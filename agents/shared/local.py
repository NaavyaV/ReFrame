"""What the local agents share: seeds, ports, addresses and how they find each other.

Registration is off, so the Almanac can't resolve them. The resolver maps every
local agent's address to its endpoint and falls back to the global resolver for
anyone else (an ASI:One or Agentverse user).

The orchestrator relays chats for many users, so it tags each message with a
MetadataContent {"session": <user>}. Agents trust that tag only from the
orchestrator's address and key their sessions by it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass

from uagents.resolver import GlobalResolver, Resolver
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent
from uagents_core.identity import Identity
from uagents_core.registration import AgentRegistrationPolicy

# name: (seed variable, fallback seed, port variable, default port)
AGENTS = {
    "orchestrator": ("ORCHESTRATOR_SEED", "reframe-orchestrator-local", "ORCHESTRATOR_PORT", 8040),
    "technical_helper": ("TECHNICAL_HELPER_SEED", "reframe-technical-helper-local", "TECHNICAL_HELPER_PORT", 8010),
    "caretaker": ("CAREGIVER_SEED", "reframe-caretaker-local", "CARETAKER_PORT", 8020),
    "scraper": ("SCRAPER_SEED", "reframe-scraper-local", "SCRAPER_PORT", 8030),
}


class SkipRegistration(AgentRegistrationPolicy):
    async def register(self, agent_identifier, identity, protocols, endpoints, metadata=None):
        return None


def seed(name: str) -> str:
    variable, fallback, _, _ = AGENTS[name]
    return os.getenv(variable, "").strip() or fallback


def port(name: str) -> int:
    _, _, variable, default = AGENTS[name]
    return int(os.getenv(variable, "").strip() or default)


def endpoint(name: str) -> str:
    return f"http://127.0.0.1:{port(name)}/submit"


def address(name: str) -> str:
    return Identity.from_seed(seed(name), 0).address


class LocalResolver(Resolver):
    def __init__(self) -> None:
        self._rules = {address(name): endpoint(name) for name in AGENTS}
        self._fallback = GlobalResolver()

    async def resolve(self, destination: str) -> tuple[str | None, list[str]]:
        if destination in self._rules:
            return destination, [self._rules[destination]]
        return await self._fallback.resolve(destination)


def agent_options(name: str) -> dict:
    """Keyword arguments for uagents.Agent shared by every local agent."""
    return {
        "name": name,
        "seed": seed(name),
        "port": port(name),
        "endpoint": [endpoint(name)],
        "mailbox": False,
        "registration_policy": SkipRegistration(),
        "publish_agent_details": False,
        "report_events": False,
        "resolve": LocalResolver(),
    }


def relayed_session(sender: str, msg: ChatMessage) -> str | None:
    """The user a message was relayed for, if it came from the orchestrator."""
    if sender != address("orchestrator"):
        return None
    for item in msg.content:
        if isinstance(item, MetadataContent) and item.metadata.get("session"):
            return item.metadata["session"]
    return None


def create_text_chat(text: str, session: str | None = None) -> ChatMessage:
    content: list = [TextContent(text=text)]
    if session:
        content.append(MetadataContent(metadata={"session": session}))
    return ChatMessage(content=content, msg_id=uuid4(), timestamp=datetime.now(timezone.utc))
