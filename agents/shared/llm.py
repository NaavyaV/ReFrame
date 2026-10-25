"""Groq calls shared by the agents.

Async, so a slow model call never stalls an agent. Every call has a timeout, and
callers always have a plain fallback, so the agents keep working without Groq.
GROQ_MODEL writes what people read; GROQ_FAST_MODEL extracts and takes over when
GROQ_MODEL is rate limited.
"""

from __future__ import annotations

import json
import os
import re

import groq

MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"
FAST_MODEL = os.getenv("GROQ_FAST_MODEL", "").strip() or "openai/gpt-oss-20b"
TIMEOUT_SECONDS = 20.0

_client: groq.AsyncGroq | None = None


class LLMError(Exception):
    pass


def configured() -> bool:
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def _groq() -> groq.AsyncGroq:
    global _client
    if not configured():
        raise LLMError("GROQ_API_KEY isn't set.")
    if _client is None:
        _client = groq.AsyncGroq(api_key=os.environ["GROQ_API_KEY"].strip(), max_retries=2, timeout=TIMEOUT_SECONDS)
    return _client


async def complete(
    messages: list[dict[str, str]],
    *,
    model: str = MODEL,
    max_tokens: int = 900,
    temperature: float = 0.3,
    json_mode: bool = False,
) -> str:
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if "gpt-oss" in model:
        kwargs["reasoning_effort"] = "low"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        response = await _groq().chat.completions.create(**kwargs)
    except groq.RateLimitError:
        if model == FAST_MODEL:
            raise LLMError("Groq is rate limited.") from None
        return await complete(
            messages, model=FAST_MODEL, max_tokens=max_tokens, temperature=temperature, json_mode=json_mode
        )
    except groq.APIError as exc:
        raise LLMError(f"Groq request failed ({exc.__class__.__name__}).") from exc
    return (response.choices[0].message.content or "").strip()


def parse_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def complete_json(system: str, user: str, *, model: str = FAST_MODEL, max_tokens: int = 1500) -> dict:
    """Ask for a JSON object. Retries once without JSON mode, which rejects some valid answers."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        data = parse_json(await complete(messages, model=model, max_tokens=max_tokens, temperature=0.2, json_mode=True))
    except LLMError:
        data = None
    if data is None:
        messages[0]["content"] += "\nReply with one JSON object and nothing else."
        data = parse_json(await complete(messages, model=model, max_tokens=max_tokens, temperature=0.2))
    if data is None:
        raise LLMError("The model did not return JSON.")
    return data


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_URL = re.compile(r"https?://\S+")


def grounded(text: str, facts: str) -> bool:
    """True if every number and link in text also appears in facts.

    Used to throw away model wording that invents a price, rating or URL.
    """
    plain_facts = facts.replace(",", "")
    for number in _NUMBER.findall(text):
        if number.replace(",", "") not in plain_facts:
            return False
    return all(url.rstrip(").,") in facts for url in _URL.findall(text))
