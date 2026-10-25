"""Orchestrator uAgent.

Same shape as the other local agents: chat protocol, Groq, no mailbox.
Registration stays off. Talk in this terminal, or send a chat message to the local endpoint.
It is the one agent a person talks to. Each message goes to the technical helper,
the scraper or the caretaker as a real uAgents chat message, and the reply comes back here.
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.local import address, agent_options, create_text_chat  # noqa: E402

from groq import Groq
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from scraper.page import find_url

MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"
REPLY_TIMEOUT = 60.0
LOCAL = "local"

SPECIALISTS = {
    "technical_helper": "Fixes device, app, email, Wi-Fi, printer, password or general how-do-I problems.",
    "scraper": "Reads a specific public website and says where to click on it.",
    "caretaker": "Patient records, health, medications and notes about the people a caretaker looks after.",
}
LABELS = {
    "technical_helper": "technical helper",
    "scraper": "scraper",
    "caretaker": "caretaker",
}
CARE_WORDS = re.compile(
    r"\b(patients?|records?|medications?|medicines?|meds|caretaker|caregiver|resident)\b", re.I
)
RESET_WORDS = {"start over", "reset", "new problem"}


@dataclass
class Route:
    last: str | None = None
    last_reply: str = ""
    waiting_on: str | None = None
    since: float = 0.0


agent = Agent(**agent_options("orchestrator"))

chat_proto = Protocol(spec=chat_protocol_spec)
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
routes: dict[str, Route] = {}
by_address = {address(name): name for name in SPECIALISTS}
typed: queue.Queue[str] = queue.Queue()


def _complete(system: str, user: str) -> str:
    kwargs = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_completion_tokens": 20,
    }
    if "gpt-oss" in MODEL:
        kwargs["reasoning_effort"] = "low"
        kwargs["max_completion_tokens"] = 200
    message = groq_client.chat.completions.create(**kwargs).choices[0].message
    return (message.content or "").strip()


def choose(route: Route, text: str) -> str:
    if find_url(text):
        return "scraper"
    # The caretaker is waiting on an answer to its security question.
    if route.last == "caretaker" and route.last_reply.rstrip().endswith("?"):
        return "caretaker"
    if CARE_WORDS.search(text):
        return "caretaker"

    options = "\n".join(f"- {name}: {about}" for name, about in SPECIALISTS.items())
    context = ""
    if route.last:
        context = f"\nThe {route.last} answered last and said: {route.last_reply[-300:]}"
    try:
        picked = _complete(
            "Pick which helper should answer the person's message. "
            "If it continues the last conversation, keep the same helper. "
            "Reply with only the helper's name.\n" + options,
            f"Message: {text}{context}",
        ).lower()
    except Exception:
        picked = ""
    for name in SPECIALISTS:
        if name in picked:
            return name
    return route.last or "technical_helper"


async def relay(ctx: Context, session: str, text: str) -> None:
    route = routes.setdefault(session, Route())
    if text.strip().lower() in RESET_WORDS:
        await end_sessions(ctx, session)
        routes[session] = Route()
        await deliver(ctx, session, "Okay. What do you need?")
        return
    if route.waiting_on:
        await deliver(ctx, session, f"One moment, the {LABELS[route.waiting_on]} is still answering.")
        return
    name = choose(route, text)
    route.waiting_on = name
    route.since = time.monotonic()
    ctx.logger.info(f"Routing {session} to {name}")
    await ctx.send(address(name), create_text_chat(text, session))


async def end_sessions(ctx: Context, session: str) -> None:
    for name in SPECIALISTS:
        await ctx.send(
            address(name),
            ChatMessage(content=[EndSessionContent(), MetadataContent(metadata={"session": session})]),
        )


async def deliver(ctx: Context, session: str, text: str) -> None:
    if session == LOCAL:
        print(text, flush=True)
        print("> ", end="", flush=True)
    else:
        await ctx.send(session, create_text_chat(text))


def _session_of(msg: ChatMessage) -> str | None:
    for item in msg.content:
        if isinstance(item, MetadataContent) and item.metadata.get("session"):
            return item.metadata["session"]
    return None


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

    # A reply from one of the local agents, for the user named in its metadata.
    if sender in by_address:
        session = _session_of(msg)
        text = msg.text().strip()
        if not session or not text:
            return
        route = routes.setdefault(session, Route())
        route.last = by_address[sender]
        route.last_reply = text
        route.waiting_on = None
        await deliver(ctx, session, text)
        return

    for item in msg.content:
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"Session started with {sender}")
        elif isinstance(item, TextContent):
            ctx.logger.info(f"Text from {sender}: {item.text}")
            await relay(ctx, sender, item.text)
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"Session ended with {sender}")
            await end_sessions(ctx, sender)
            routes.pop(sender, None)
        else:
            ctx.logger.info(f"Ignored content from {sender}")


@chat_proto.on_message(ChatAcknowledgement)
async def handle_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Acknowledgement from {sender} for {msg.acknowledged_msg_id}")


agent.include(chat_proto, publish_manifest=False)


@agent.on_interval(period=0.5)
async def tick(ctx: Context):
    while True:
        try:
            text = typed.get_nowait()
        except queue.Empty:
            break
        await relay(ctx, LOCAL, text)
    now = time.monotonic()
    for session, route in routes.items():
        if route.waiting_on and now - route.since > REPLY_TIMEOUT:
            name = route.waiting_on
            route.waiting_on = None
            await deliver(ctx, session, f"The {LABELS[name]} isn't answering. Is it running?")


@agent.on_event("startup")
async def startup(ctx: Context):
    for name in SPECIALISTS:
        ctx.logger.info(f"{LABELS[name]}: {address(name)}")
    ctx.logger.info("Orchestrator ready")


def _repl() -> None:
    print("Orchestrator", flush=True)
    print("> ", end="", flush=True)
    while True:
        try:
            text = input()
        except EOFError:
            return
        if text.strip().lower() in {"quit", "exit"}:
            os._exit(0)
        if text.strip():
            typed.put(text)
        else:
            print("> ", end="", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    print(f"Agent address: {agent.address}", flush=True)
    if os.getenv("ORCHESTRATOR_REPL", "1") != "0":
        threading.Thread(target=_repl, daemon=True).start()
    agent.run()
