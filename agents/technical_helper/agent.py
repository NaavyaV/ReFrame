"""Technical helper uAgent.

Same shape as the other local agents: chat protocol, Groq, no mailbox.
Registration stays off. Talk in this terminal, or send a chat message to the local endpoint.
Walks an older or less technical person through a device or website problem, one step at a time.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.local import agent_options, create_text_chat, relayed_session  # noqa: E402

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

MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"
HISTORY_TURNS = 12

SYSTEM = (
    "You are a patient technical helper for older or less technical people. "
    "Help with phones, computers, email, passwords, Wi-Fi, printers and websites. "
    "Give one or two steps at a time in plain words, then ask if it worked. "
    "Say exactly what the button or menu is called and where it is on the screen. "
    "No jargon. Short sentences. "
    "Never ask for passwords, card numbers, codes sent by text, or other secrets. "
    "If someone asks them to pay, send gift cards, install remote-control software "
    "or read out a code, say it is probably a scam and to hang up or close the page."
)


agent = Agent(**agent_options("technical_helper"))

chat_proto = Protocol(spec=chat_protocol_spec)
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])


@dataclass
class Session:
    history: list[dict[str, str]] = field(default_factory=list)


sessions: dict[str, Session] = {}


def _complete(messages: list[dict[str, str]]) -> str:
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_completion_tokens": 500,
    }
    if "gpt-oss" in MODEL:
        kwargs["reasoning_effort"] = "low"
    message = groq_client.chat.completions.create(**kwargs).choices[0].message
    return (message.content or "").strip()


def answer(sender: str, text: str) -> str:
    text = text.strip()
    if not text:
        return "Tell me what isn't working."
    session = sessions.setdefault(sender, Session())
    if text.lower() in {"start over", "reset", "new problem"}:
        session.history.clear()
        return "Okay. What would you like help with?"

    session.history.append({"role": "user", "content": text})
    session.history = session.history[-HISTORY_TURNS:]
    try:
        reply = _complete([{"role": "system", "content": SYSTEM}, *session.history])
    except Exception:
        reply = ""
    if not reply:
        session.history.pop()
        return "I couldn't think that through just now. Try again in a moment."
    session.history.append({"role": "assistant", "content": reply})
    return reply


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
    session = relayed_session(sender, msg)
    key = session or sender
    for item in msg.content:
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"Session started with {sender}")
        elif isinstance(item, TextContent):
            ctx.logger.info(f"Text from {sender}: {item.text}")
            try:
                reply = answer(key, item.text)
            except Exception as exc:
                ctx.logger.error(f"Technical helper failed: {exc}")
                reply = "Something went wrong on my side. Try again."
            await ctx.send(sender, create_text_chat(reply, session))
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"Session ended with {sender}")
            sessions.pop(key, None)
        elif isinstance(item, MetadataContent):
            pass
        else:
            ctx.logger.info(f"Ignored content from {sender}")


@chat_proto.on_message(ChatAcknowledgement)
async def handle_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Acknowledgement from {sender} for {msg.acknowledged_msg_id}")


agent.include(chat_proto, publish_manifest=False)


@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info("Technical helper ready")


def _repl() -> None:
    print("Technical helper", flush=True)
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
            print(f"Something went wrong. {exc}", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    print(f"Agent address: {agent.address}", flush=True)
    if os.getenv("TECHNICAL_HELPER_REPL", "1") != "0":
        threading.Thread(target=_repl, daemon=True).start()
    agent.run()
