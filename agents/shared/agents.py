"""Setup shared by the Hold My Hand agents.

All four are Agentverse mailbox agents. Their addresses come from the seeds in .env,
so the seeds must never change: a new seed is a new agent. A missing seed is filled
once with a cryptographically secure value; an existing one is never replaced.

The agents reach each other directly on 127.0.0.1 first and fall back to the
Agentverse mailbox, so a local run works even before the mailboxes are registered.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import secrets
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

try:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)
except Exception:
    pass

from shared.security import install_redaction  # noqa: E402

install_redaction()

import httpx  # noqa: E402
from uagents import Agent, Context, Protocol  # noqa: E402
from uagents.resolver import GlobalResolver, Resolver  # noqa: E402
from uagents_core.config import AgentverseConfig  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.identity import Identity, parse_identifier  # noqa: E402

LOCAL = "local"
RESET_WORDS = {"reset", "start over", "new problem", "cancel"}


@dataclass(frozen=True)
class Spec:
    key: str
    display_name: str
    seed_var: str
    port_var: str
    port: int
    folder: str
    repl_var: str
    description: str
    avatar_url: str | None = None


# Names, descriptions and avatar are what the Agentverse profiles show.
# Ports are the ones this repo has always used; 8000 belongs to the backend.
AGENTS: dict[str, Spec] = {
    "orchestrator": Spec(
        "orchestrator",
        "Hold My Hand Orchestrator",
        "ORCHESTRATOR_SEED",
        "ORCHESTRATOR_PORT",
        8040,
        "orchestrator",
        "ORCHESTRATOR_REPL",
        "Helps older adults shop online: tell it what you need and your budget, and it returns one "
        "product with its price, link and why it fits. It never buys anything for you.",
    ),
    "technical_helper": Spec(
        "technical_helper",
        "Hold My Hand Technical Helper",
        "TECHNICAL_HELPER_SEED",
        "TECHNICAL_HELPER_PORT",
        8010,
        "technical_helper",
        "TECHNICAL_HELPER_REPL",
        "Checks real product listings against a shopper's budget and needs, retries a few times if "
        "nothing fits, and picks one with reasons. Also explains device and website problems step by step.",
    ),
    "scraper": Spec(
        "scraper",
        "Hold My Hand Scraper",
        "SCRAPER_SEED",
        "SCRAPER_PORT",
        8030,
        "scraper",
        "SCRAPER_REPL",
        "Runs an Apify Actor on an approved shopping site and returns up to three real product "
        "listings with prices, brands, links and where each fact came from. Facts only, no verdicts.",
        avatar_url="https://storage.googleapis.com/agentverse-prod-assets/agent-avatars-pub/706244553.jpg",
    ),
    "caregiver": Spec(
        "caregiver",
        "Hold My Hand Caregiver",
        "CAREGIVER_SEED",
        "CARETAKER_PORT",
        8020,
        "caretaker",
        "CARETAKER_REPL",
        "Lets a caregiver the person has approved adjust a few response settings, such as simpler "
        "wording or larger text hints. Every change is logged and the person can undo it.",
    ),
}


class MissingSeed(SystemExit):
    pass


def seed(key: str) -> str:
    spec = AGENTS[key]
    value = os.getenv(spec.seed_var, "").strip()
    if not value:
        raise MissingSeed(
            f"{spec.seed_var} is empty in .env. Run `python run_all.py` once to generate the missing "
            "seed, or set it yourself. The agent's address comes from it, so never change it later."
        )
    return value


def port(key: str) -> int:
    spec = AGENTS[key]
    return int(os.getenv(spec.port_var, "").strip() or spec.port)


def address(key: str) -> str:
    """An agent's address: <KEY>_ADDRESS if set, otherwise derived from its seed."""
    override = os.getenv(f"{key.upper()}_ADDRESS", "").strip()
    return override or Identity.from_seed(seed(key), 0).address


def local_endpoint(key: str) -> str:
    return f"http://127.0.0.1:{port(key)}/submit"


def fill_missing_seeds(path: Path = ENV_PATH) -> list[str]:
    """Give every agent without a seed a new random one, written into .env.

    Only empty values are filled. Existing seeds, other variables, comments and line
    order are left exactly as they are. Returns the names that were filled, never values.
    """
    missing = [spec.seed_var for spec in AGENTS.values() if not os.getenv(spec.seed_var, "").strip()]
    if not missing:
        return []
    lines: list[str] = []
    if path.exists():
        with open(path, encoding="utf-8", newline="") as file:  # keep the file's own line endings
            lines = file.read().splitlines(keepends=True)
    filled = []
    for name in missing:
        value = secrets.token_hex(32)
        pattern = re.compile(rf"^\s*{re.escape(name)}\s*=(.*?)(\r?\n)?$")
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if not match:
                continue
            current = match.group(1).strip()
            if current and not current.startswith("#"):
                value = ""  # set in the file but not loaded; leave it for a person to look at
            else:
                lines[index] = f"{name}={value}{match.group(2) or os.linesep}"
            break
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(f"{name}={value}\n")
        if value:
            os.environ[name] = value
            filled.append(name)
    if filled:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines), encoding="utf-8", newline="")
        os.replace(tmp, path)
    return filled


MAILBOX_SUBMIT = AgentverseConfig().mailbox_endpoint


class LocalFirstResolver(Resolver):
    """Our own agents: their local endpoint, then their Agentverse mailbox. Anyone else: the Almanac."""

    def __init__(self) -> None:
        self._known: dict[str, list[str]] = {}
        for key in AGENTS:
            try:
                self._known[address(key)] = [local_endpoint(key), MAILBOX_SUBMIT]
            except MissingSeed:
                continue
        self._global = GlobalResolver()

    async def resolve(self, destination: str) -> tuple[str | None, list[str]]:
        _, _, parsed = parse_identifier(destination)
        if parsed in self._known:
            return parsed, list(self._known[parsed])
        return await self._global.resolve(destination)


def make_agent(key: str, **options) -> Agent:
    spec = AGENTS[key]
    agent = Agent(
        name=spec.display_name,
        seed=seed(key),
        port=port(key),
        mailbox=True,
        resolve=LocalFirstResolver(),
        readme_path=str(AGENTS_DIR / spec.folder / "README.md"),
        description=spec.description,
        avatar_url=spec.avatar_url,
        publish_agent_details=True,
        handle_messages_concurrently=True,
        shutdown_timeout=10.0,
        **options,
    )

    @agent.on_event("startup")
    async def connect_to_agentverse(ctx: Context):
        asyncio.get_running_loop().create_task(_connect_mailbox(ctx, spec))

    return agent


OWNERSHIP_HINT = re.compile(r"owned by|another user|not the owner|belongs to|already registered", re.I)
KEY_HINT = re.compile(r"unauthori[sz]ed|invalid token|expired|not authenticated|401", re.I)


async def _connect_mailbox(ctx: Context, spec: Spec) -> None:
    """Register the mailbox and profile with AGENTVERSE_API_KEY, the same call Inspector > Connect makes."""
    key = os.getenv("AGENTVERSE_API_KEY", "").strip()
    if not key:
        ctx.logger.warning(
            "Agentverse: AGENTVERSE_API_KEY isn't set, so the mailbox wasn't registered automatically. "
            "Open the Inspector link above and choose Connect > Mailbox."
        )
        return
    base = f"http://127.0.0.1:{port(spec.key)}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(40):
                try:
                    if (await client.get(f"{base}/agent_info")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            response = await client.post(f"{base}/connect", json={"user_token": key, "agent_type": "mailbox"})
        result = response.json() if response.status_code == 200 else {"success": False, "detail": response.text}
    except Exception as exc:
        ctx.logger.error(f"Agentverse: couldn't register the mailbox ({exc.__class__.__name__}). Messages from "
                         "ASI:One won't arrive until it is registered; local agent-to-agent messages still work.")
        return
    detail = str(result.get("detail") or "")[:300]
    if result.get("success"):
        ctx.logger.info(
            f"Agentverse: mailbox registered and profile published for '{spec.display_name}'. Keep this "
            "process running to receive messages. (Almanac contract or funding warnings are separate.)"
        )
    elif OWNERSHIP_HINT.search(detail):
        ctx.logger.error(
            f"Agentverse: this address is registered to a different Agentverse account ({detail}). The seed "
            "was NOT changed. Use that account's API key, or migrate deliberately (see README, Troubleshooting)."
        )
    elif KEY_HINT.search(detail):
        ctx.logger.error(
            f"Agentverse: AGENTVERSE_API_KEY was rejected ({detail}). Create a new key in Agentverse > "
            "Profile > API Keys, or connect by hand with Inspector > Connect > Mailbox."
        )
    else:
        ctx.logger.error(f"Agentverse: mailbox registration failed ({detail or 'no detail'}). "
                         "You can connect by hand with Inspector > Connect > Mailbox.")


def text_message(text: str, *, end_session: bool = False) -> ChatMessage:
    content: list = [TextContent(text=text)]
    if end_session:
        content.append(EndSessionContent())
    return ChatMessage(content=content, msg_id=uuid4(), timestamp=datetime.now(timezone.utc))


async def deliver(ctx: Context, session: str, text: str, *, end_session: bool = False) -> None:
    """Send a chat reply, or print it when the session is this terminal."""
    if session == LOCAL:
        print(f"\n{text}\n", flush=True)
        print("> ", end="", flush=True)
    else:
        await ctx.send(session, text_message(text, end_session=end_session))


TextHandler = Callable[[Context, str, str], Awaitable[None]]


def chat_protocol(
    on_text: TextHandler,
    *,
    on_end: Callable[[str], None] | None = None,
    ignore: Callable[[str], bool] = lambda sender: False,
) -> Protocol:
    """The Agent Chat Protocol: acknowledge every message, then hand its text to on_text(ctx, sender, text).

    Messages from senders where ignore(sender) is true are acknowledged and dropped, so two
    of our agents can never chat each other into a loop. Acknowledgements are never answered.
    """
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(
            sender,
            ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id),
        )
        if ignore(sender):
            ctx.logger.debug(f"Ignored chat from one of our own agents: {sender}")
            return
        if any(isinstance(item, StartSessionContent) for item in msg.content):
            ctx.logger.info(f"Chat session started by {sender}")
        text = msg.text().strip()
        if text:
            try:
                await on_text(ctx, sender, text)
            except Exception as exc:
                ctx.logger.exception(f"Failed to answer {sender}: {exc.__class__.__name__}")
                await ctx.send(sender, text_message("Sorry, something went wrong on my side. Please try again."))
        if on_end and any(isinstance(item, EndSessionContent) for item in msg.content):
            on_end(sender)

    @proto.on_message(ChatAcknowledgement)
    async def handle_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
        ctx.logger.debug(f"Acknowledgement from {sender} for {msg.acknowledged_msg_id}")

    return proto


def own_agent(sender: str) -> bool:
    try:
        return sender in {address(key) for key in AGENTS}
    except MissingSeed:
        return False


class Terminal:
    """A chat in this terminal. Typed lines reach on_text with the LOCAL session."""

    def __init__(self, agent: Agent, title: str, on_text: TextHandler, env_var: str) -> None:
        self.title = title
        self.on_text = on_text
        self.enabled = os.getenv(env_var, "1") != "0"
        self.lines: queue.Queue[str] = queue.Queue()

        @agent.on_interval(period=0.5)
        async def drain(ctx: Context):
            while True:
                try:
                    text = self.lines.get_nowait()
                except queue.Empty:
                    return
                try:
                    await self.on_text(ctx, LOCAL, text)
                except Exception as exc:
                    ctx.logger.exception(f"Failed to answer the terminal: {exc.__class__.__name__}")
                    await deliver(ctx, LOCAL, "Sorry, something went wrong on my side. Please try again.")

    def _read(self) -> None:
        print(f"{self.title} (type quit to stop)", flush=True)
        print("> ", end="", flush=True)
        while True:
            try:
                text = input()
            except (EOFError, OSError):
                return
            if text.strip().lower() in {"quit", "exit"}:
                signal.raise_signal(signal.SIGINT)
                return
            if text.strip():
                self.lines.put(text.strip())
            else:
                print("> ", end="", flush=True)

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._read, daemon=True).start()


def run(agent: Agent, terminal: Terminal | None = None) -> None:
    """Run one agent until Ctrl+C, then shut it down cleanly (a second Ctrl+C forces it)."""
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except AttributeError:
        pass
    print(f"Agent address: {agent.address}", flush=True)
    loop = asyncio.get_event_loop_policy().get_event_loop()
    main = loop.create_task(agent.run_async())

    def stop(*_args) -> None:
        loop.call_soon_threadsafe(main.cancel)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    if terminal is not None:
        terminal.start()
    try:
        loop.run_until_complete(main)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        loop.close()
