"""Scraper uAgent.

Same shape as the other local agents: chat protocol, Groq, no mailbox.
Registration stays off. Talk in this terminal, or send a chat message to the local endpoint.
Send a public URL and a goal; it reads the page and says where to click.
Follow-up questions use the last page read in that chat.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass

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

from page import Page, ScrapeError, describe, fetch, find_url

PORT = int(os.getenv("SCRAPER_PORT", "8030"))
SEED = os.getenv("SCRAPER_SEED", "").strip() or "reframe-scraper-local"
MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"

SYSTEM = (
    "You help an older or less technical person find their way around a website. "
    "Use only the page below. Never claim you clicked anything. "
    "Word it as guidance: 'Look for...', 'Select...'. "
    "Name links and buttons exactly as they appear. "
    "Give at most four short numbered steps. "
    "If the page doesn't have what they want, say so and name the closest link."
)


class SkipRegistration(AgentRegistrationPolicy):
    async def register(self, agent_identifier, identity, protocols, endpoints, metadata=None):
        return None


agent = Agent(
    name="scraper",
    seed=SEED,
    port=PORT,
    endpoint=[f"http://127.0.0.1:{PORT}/submit"],
    mailbox=False,
    registration_policy=SkipRegistration(),
    publish_agent_details=False,
    report_events=False,
)

chat_proto = Protocol(spec=chat_protocol_spec)
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])


@dataclass
class Session:
    page: Page | None = None


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
        "temperature": 0.2,
        "max_completion_tokens": 500,
    }
    if "gpt-oss" in MODEL:
        kwargs["reasoning_effort"] = "low"
    message = groq_client.chat.completions.create(**kwargs).choices[0].message
    return (message.content or "").strip()


def _plain(page: Page) -> str:
    lines = [f"{page.title or page.url}."]
    if page.links:
        lines.append("Links on the page: " + ", ".join(label for label, _ in page.links[:8]) + ".")
    return " ".join(lines)


def answer(sender: str, text: str) -> str:
    text = text.strip()
    if not text:
        return "Send me a web address and what you want to do there."
    session = sessions.setdefault(sender, Session())

    url = find_url(text)
    if url:
        try:
            session.page = fetch(url)
        except ScrapeError as exc:
            return str(exc)
    if session.page is None:
        return "Which website? Send me its address."

    goal = text
    if url:
        goal = goal.replace(url, "").replace(url.removeprefix("https://"), "").strip(" ,.-")
    if not goal:
        goal = "What is this page for and what are the main things I can do here?"
    try:
        reply = _complete(SYSTEM, f"Goal: {goal}\n\n{describe(session.page)}")
    except Exception:
        reply = ""
    return reply or _plain(session.page)


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
                ctx.logger.error(f"Scraper failed: {exc}")
                reply = "I couldn't read that page. Try again."
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
    ctx.logger.info("Scraper ready")


def _repl() -> None:
    print("Scraper", flush=True)
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
            print(f"I couldn't read that page. {exc}", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    print(f"Agent address: {agent.address}", flush=True)
    if os.getenv("SCRAPER_REPL", "1") != "0":
        threading.Thread(target=_repl, daemon=True).start()
    agent.run()
