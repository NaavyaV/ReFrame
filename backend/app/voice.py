"""Voice in and out through Cartesia: speech to text for requests, text to speech for replies."""

import base64
import binascii
import io
import json
import logging
import math
import struct
import uuid
import wave

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import PublicUser, current_user
from .config import get_settings

log = logging.getLogger("reframe.voice")
router = APIRouter(prefix="/voice", tags=["voice"])

API = "https://api.cartesia.ai"
TTS_WS = "wss://api.cartesia.ai/tts/websocket"
VERSION = "2025-04-16"
SAMPLE_RATE = 24000
MAX_AUDIO_BYTES = 5 * 1024 * 1024
ENVELOPE_STEP_S = 0.05


class TranscribeRequest(BaseModel):
    audio: str = Field(min_length=1, max_length=MAX_AUDIO_BYTES * 4 // 3 + 4)
    mime: str = Field(default="audio/webm", max_length=60)


class TranscribeResponse(BaseModel):
    text: str


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=600)


class SpokenWord(BaseModel):
    word: str
    start: float
    end: float


class SpeakResponse(BaseModel):
    audio: str
    duration: float
    words: list[SpokenWord]
    envelope: list[float]
    envelope_step: float = ENVELOPE_STEP_S


def _key() -> str:
    key = get_settings().cartesia_api_key
    if not key:
        raise HTTPException(503, "Voice isn't set up.")
    return key


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(body: TranscribeRequest, _user: PublicUser = Depends(current_user)) -> TranscribeResponse:
    key = _key()
    try:
        audio = base64.b64decode(body.audio, validate=True)
    except binascii.Error:
        raise HTTPException(400, "Bad audio.")
    ext = "webm" if "webm" in body.mime else "ogg" if "ogg" in body.mime else "wav" if "wav" in body.mime else "mp4"
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API}/stt",
                headers={"Authorization": f"Bearer {key}", "Cartesia-Version": VERSION},
                files={"file": (f"speech.{ext}", audio, body.mime.split(";")[0])},
                data={"model": s.cartesia_stt_model, "language": "en"},
            )
    except httpx.HTTPError as err:
        log.warning("stt request failed: %s", err)
        raise HTTPException(502, "Couldn't reach the voice service.")
    if resp.status_code != 200:
        log.warning("stt %s: %s", resp.status_code, resp.text[:300])
        raise HTTPException(502, "Couldn't understand the audio.")
    return TranscribeResponse(text=(resp.json().get("text") or "").strip())


def _envelope(pcm: bytes) -> list[float]:
    """Loudness per 50 ms, scaled 0..1, for the speaking animation."""
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    step = int(SAMPLE_RATE * ENVELOPE_STEP_S)
    levels = []
    for i in range(0, len(samples), step):
        frame = samples[i : i + step]
        levels.append(math.sqrt(sum(x * x for x in frame) / max(len(frame), 1)) / 32768)
    peak = max(levels, default=0) or 1
    return [round(min(1.0, (v / peak) ** 0.7), 3) for v in levels]


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


@router.post("/speak", response_model=SpeakResponse)
async def speak(body: SpeakRequest, _user: PublicUser = Depends(current_user)) -> SpeakResponse:
    key = _key()
    s = get_settings()
    request = {
        "model_id": s.cartesia_tts_model,
        "transcript": body.text,
        "voice": {"mode": "id", "id": s.cartesia_voice_id},
        "language": "en",
        "context_id": uuid.uuid4().hex,
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
        "add_timestamps": True,
    }
    pcm = bytearray()
    words: list[SpokenWord] = []
    try:
        async with websockets.connect(
            f"{TTS_WS}?cartesia_version={VERSION}", additional_headers={"X-API-Key": key}, open_timeout=10
        ) as ws:
            await ws.send(json.dumps(request))
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "chunk":
                    pcm += base64.b64decode(msg["data"])
                elif kind == "timestamps":
                    ts = msg["word_timestamps"]
                    words += [SpokenWord(word=w, start=a, end=b) for w, a, b in zip(ts["words"], ts["start"], ts["end"])]
                elif kind == "error":
                    log.warning("tts error: %s", msg.get("message") or msg.get("error"))
                    raise HTTPException(502, "Couldn't create the voice reply.")
                if msg.get("done"):
                    break
    except (OSError, websockets.WebSocketException) as err:
        log.warning("tts connection failed: %s", err)
        raise HTTPException(502, "Couldn't reach the voice service.")
    if not pcm:
        raise HTTPException(502, "Couldn't create the voice reply.")
    return SpeakResponse(
        audio=base64.b64encode(_wav(bytes(pcm))).decode(),
        duration=len(pcm) / 2 / SAMPLE_RATE,
        words=words,
        envelope=_envelope(bytes(pcm)),
    )
