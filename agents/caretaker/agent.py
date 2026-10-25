"""Caretaker uAgent.

Same shape as the other local agents: chat protocol, Groq, no mailbox.
Registration stays off. Talk in this terminal, or send a chat message to the local endpoint.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from groq import Groq
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.registration import AgentRegistrationPolicy

from db import Spacetime

PORT = int(os.getenv("CARETAKER_PORT", "8020"))
SEED = os.getenv("CAREGIVER_SEED", "").strip() or "reframe-caretaker-local"
MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"


class SkipRegistration(AgentRegistrationPolicy):
    async def register(self, agent_identifier, identity, protocols, endpoints, metadata=None):
        return None


agent = Agent(
    name="caretaker",
    seed=SEED,
    port=PORT,
    endpoint=[f"http://127.0.0.1:{PORT}/submit"],
    mailbox=False,
    registration_policy=SkipRegistration(),
    publish_agent_details=False,
    report_events=False,
)

chat_proto = Protocol(spec=chat_protocol_spec)
db = Spacetime()
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])


@dataclass
class Pending:
    patient: dict[str, str]
    request: str


@dataclass
class Session:
    unlocked: set[str] = field(default_factory=set)
    pending: Pending | None = None


sessions: dict[str, Session] = {}


def create_text_chat(text: str) -> ChatMessage:
    return ChatMessage(content=[TextContent(text=text)], msg_id=uuid4(), timestamp=datetime.now(timezone.utc))


def _complete(system: str, user: str) -> str:
    kwargs = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_completion_tokens": 400,
    }
    if "gpt-oss" in MODEL:
        kwargs["reasoning_effort"] = "low"
    message = groq_client.chat.completions.create(**kwargs).choices[0].message
    return (message.content or "").strip()


def _match(text: str, patients: list[dict[str, str]]) -> list[dict[str, str]]:
    folded = text.lower()
    hits = []
    for patient in patients:
        names = [patient["full_name"].lower(), *patient["full_name"].lower().split()]
        if any(len(name) > 2 and name in folded for name in names):
            hits.append(patient)
    return hits


def _note_body(text: str) -> str | None:
    match = re.search(
        r"\bnote\b\s*(?:for\s+[^:]{0,80}?)?\s*(?:that|:|-)\s*(.+)",
        text,
        re.I | re.S,
    )
    if not match:
        match = re.search(r"\bnote\b\s+(.+)", text, re.I | re.S)
    if not match:
        return None
    body = match.group(1).strip().strip(".")
    if body.lower().startswith("for ") and ":" in body:
        body = body.split(":", 1)[1].strip()
    return body or None


def _plain(patient: dict[str, str], notes: list[dict[str, str]], saved: str | None) -> str:
    lines = [
        f"{patient['full_name']}, born {patient['date_of_birth']}, lives in {patient['city']}.",
        f"{patient['conditions']}. {patient['medications']}.",
    ]
    if notes:
        lines.append(f"Latest note: {notes[-1]['body']}")
    if saved:
        lines.append("Saved.")
    return " ".join(lines)


def _phrase(patient: dict[str, str], notes: list[dict[str, str]], request: str, saved: str | None) -> str:
    note_lines = "\n".join(f"- {note['body']}" for note in notes) or "None"
    saved_line = f"You just saved this note: {saved}" if saved else "No new note."
    system = (
        "You are a caretaker speaking with a colleague. "
        "Use only the record below. Two or three short sentences. "
        "Name the medication. Do not give new medical advice."
    )
    user = (
        f"Request: {request}\n"
        f"Name: {patient['full_name']}\n"
        f"Born: {patient['date_of_birth']}\n"
        f"City: {patient['city']}\n"
        f"Conditions: {patient['conditions']}\n"
        f"Medications: {patient['medications']}\n"
        f"Notes:\n{note_lines}\n"
        f"{saved_line}"
    )
    try:
        text = _complete(system, user)
    except Exception:
        text = ""
    if not text:
        return _plain(patient, notes, saved)
    if patient["city"].lower() not in text.lower():
        text = f"{text} Lives in {patient['city']}."
    medication = patient["medications"].split()[0]
    if medication.lower() not in text.lower():
        text = f"{text} {patient['medications']}."
    if saved and "saved" not in text.lower():
        text = f"{text} Saved."
    return text


def _open(patient: dict[str, str], request: str) -> str:
    saved = _note_body(request)
    if saved:
        db.add_note(patient["patient_id"], "caretaker", saved)
    notes = db.notes(patient["patient_id"])
    return _phrase(patient, notes, request, saved)


def answer(sender: str, text: str) -> str:
    text = text.strip()
    if not text:
        return "Say what you need."
    db.ensure_patients()
    patients = db.patients()
    session = sessions.setdefault(sender, Session())

    if session.pending is not None:
        patient = session.pending.patient
        request = session.pending.request
        session.unlocked.add(patient["patient_id"])
        session.pending = None
        return _open(patient, request)

    hits = _match(text, patients)
    if len(hits) > 1:
        return "Which person, " + " or ".join(patient["full_name"] for patient in hits) + "?"
    if len(hits) == 1:
        patient = hits[0]
        if patient["patient_id"] not in session.unlocked:
            session.pending = Pending(patient, text)
            return f"Before I open {patient['full_name']}'s record: {patient['security_question']}"
        return _open(patient, text)

    if re.search(r"\b(patients|who|list|names|people)\b", text, re.I):
        return "I look after " + ", ".join(patient["full_name"] for patient in patients) + "."

    try:
        names = ", ".join(patient["full_name"] for patient in patients)
        return _complete(
            "You are a caretaker. One or two short sentences. "
            f"You look after {names}. Do not invent their records. "
            "If they ask about a person, tell them to use that person's name.",
            text,
        )
    except Exception:
        return "Tell me the person's name."


@chat_proto.on_message(ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    ctx.logger.info(f"Message from {sender}")
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )
    for item in msg.content:
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"Session started with {sender}")
        elif isinstance(item, TextContent):
            ctx.logger.info(f"Text from {sender}: {item.text}")
            try:
                reply = answer(sender, item.text)
            except Exception as exc:
                ctx.logger.error(f"Caretaker failed: {exc}")
                reply = "I couldn't reach the records. Try again."
            await ctx.send(sender, create_text_chat(reply))
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"Session ended with {sender}")
            sessions.pop(sender, None)
        else:
            ctx.logger.info(f"Ignored content from {sender}")


@chat_proto.on_message(ChatAcknowledgement)
async def handle_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Acknowledgement from {sender} for {msg.acknowledged_msg_id}")


agent.include(chat_proto, publish_manifest=False)


@agent.on_event("startup")
async def startup(ctx: Context):
    db.ensure_patients()
    ctx.logger.info("Caretaker ready")


def _repl() -> None:
    print("Caretaker", flush=True)
    while True:
        try:
            text = input("> ")
        except EOFError:
            return
        if text.strip().lower() in {"quit", "exit"}:
            os._exit(0)
        try:
            print(answer("local", text), flush=True)
        except Exception as exc:
            print(f"I couldn't reach the records. {exc}", flush=True)


if __name__ == "__main__":
    print(f"Agent address: {agent.address}", flush=True)
    if os.getenv("CARETAKER_REPL", "1") != "0":
        threading.Thread(target=_repl, daemon=True).start()
    agent.run()
