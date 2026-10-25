"""Hold My Hand Orchestrator uAgent.

The one agent people talk to, through ASI:One or Agentverse chat (Agent Chat
Protocol), this terminal, or the backend bridge used by the browser extension.
It reads the request, delegates product research and tech help to the technical
helper, applies the person's own display settings (some of which an approved
caregiver may set), and replies in plain language. It never buys anything.

Bridge: POST /bridge/message and POST /bridge/status on this agent's port, signed
with BRIDGE_SHARED_SECRET (see shared/security.py and backend/app/agents_bridge.py).

Run on its own:  python agents/orchestrator/agent.py   (port 8040 unless ORCHESTRATOR_PORT is set)
"""

from __future__ import annotations

import os
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import llm  # noqa: E402
from shared.agents import (  # noqa: E402
    AGENTS,
    LOCAL,
    Terminal,
    address,
    chat_protocol,
    deliver,
    make_agent,
    own_agent,
    run,
)
from shared.models import (  # noqa: E402
    CAREGIVER_PROTOCOL,
    SHOPPING_PROTOCOL,
    CaregiverInviteResult,
    CaregiverPreferenceResult,
    PreferenceSnapshot,
    RecommendationResponse,
    TaskAck,
    TechHelpResponse,
)
from shared.security import NonceCache, verify_bridge  # noqa: E402
from uagents import Agent, Context, Model, Protocol  # noqa: E402

from orchestrator.flow import Action, Orchestrator, Say  # noqa: E402

BRIDGE_PREFIX = "bridge:"
USER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class BridgeMessage(Model):
    user_id: str
    message: str
    timestamp: int
    nonce: str
    signature: str


class BridgeStatusRequest(Model):
    user_id: str
    task_id: str = ""
    timestamp: int
    nonce: str
    signature: str


class BridgeReply(Model):
    ok: bool
    error: str = ""
    task_id: str = ""
    status: str = ""
    messages: list[str] = []
    result: dict[str, Any] = {}
    ui_hints: dict[str, str] = {}


def create(flow: Orchestrator | None = None, **agent_options) -> tuple[Agent, Terminal]:
    agent = make_agent("orchestrator", **agent_options)
    helper, caregiver = address("technical_helper"), address("caregiver")
    flow = flow or Orchestrator(own_address=agent.address, technical_helper=helper, caregiver=caregiver)
    outbox: dict[str, deque[str]] = {}
    nonces = NonceCache()

    async def act(ctx: Context, actions: list[Action]) -> None:
        for action in actions:
            if isinstance(action, Say):
                if action.session.startswith(BRIDGE_PREFIX):
                    outbox.setdefault(action.session, deque(maxlen=50)).append(action.text)
                else:
                    await deliver(ctx, action.session, action.text, end_session=action.end_session)
            else:
                status = await ctx.send(action.to, action.message)
                ctx.logger.info(f"Sent {type(action.message).__name__} to {action.to[:16]}...: {status.status}")

    # Results from the technical helper.
    shopping = Protocol(name=SHOPPING_PROTOCOL[0], version=SHOPPING_PROTOCOL[1])

    @shopping.on_message(TaskAck)
    async def on_ack(ctx: Context, sender: str, msg: TaskAck):
        ctx.logger.info(f"[{msg.task_id}] helper {msg.status}" + (f": {msg.error.code}" if msg.error else ""))
        await act(ctx, await flow.on_ack(sender, msg))

    @shopping.on_message(RecommendationResponse)
    async def on_recommendation(ctx: Context, sender: str, msg: RecommendationResponse):
        ctx.logger.info(f"[{msg.task_id}] recommendation {msg.status} after {msg.attempts} search(es), "
                        f"{msg.alternatives_considered_count} listing(s) checked")
        await act(ctx, await flow.on_recommendation(sender, msg))

    @shopping.on_message(TechHelpResponse)
    async def on_tech_help(ctx: Context, sender: str, msg: TechHelpResponse):
        await act(ctx, await flow.on_tech_help(sender, msg))

    agent.include(shopping)

    # Answers and notices from the caregiver agent.
    care = Protocol(name=CAREGIVER_PROTOCOL[0], version=CAREGIVER_PROTOCOL[1])

    @care.on_message(CaregiverInviteResult)
    async def on_invite(ctx: Context, sender: str, msg: CaregiverInviteResult):
        await act(ctx, await flow.on_invite_result(sender, msg))

    @care.on_message(PreferenceSnapshot)
    async def on_snapshot(ctx: Context, sender: str, msg: PreferenceSnapshot):
        await act(ctx, await flow.on_snapshot(sender, msg))

    @care.on_message(CaregiverPreferenceResult)
    async def on_preference(ctx: Context, sender: str, msg: CaregiverPreferenceResult):
        ctx.logger.info(f"Caregiver setting {msg.setting} for {msg.user_ref}: {msg.status}")
        await act(ctx, await flow.on_preference_result(sender, msg))

    agent.include(care)

    # People.
    async def on_chat(ctx: Context, sender: str, text: str) -> None:
        await act(ctx, await flow.on_text(sender, text))

    agent.include(chat_protocol(on_chat, ignore=own_agent), publish_manifest=True)

    @agent.on_interval(period=5.0)
    async def sweep(ctx: Context):
        await act(ctx, await flow.sweep())

    # The backend bridge.
    def bridge_reply(session: str, user: str, task_id: str = "") -> BridgeReply:
        messages = list(outbox.pop(session, []))
        task = flow.status(task_id, user) if task_id else None
        if task is None:
            current = flow.sessions.get(session)
            task = flow.status(current.task_id, user) if current and current.task_id else None
        result = task.result.dict() if task is not None and task.result is not None else {}
        return BridgeReply(ok=True, task_id=task.task_id if task else "", status=task.status if task else "",
                           messages=messages, result=result, ui_hints=flow.ui_hints(user))

    def refused(user_id: str, body: str, timestamp: int, nonce: str, signature: str) -> str | None:
        if not USER_ID.match(user_id):
            return "bad_user_id"
        return verify_bridge(os.getenv("BRIDGE_SHARED_SECRET", "").strip(), timestamp, nonce, user_id, body,
                             signature, nonces)

    @agent.on_rest_post("/bridge/message", BridgeMessage, BridgeReply)
    async def bridge_message(ctx: Context, req: BridgeMessage) -> BridgeReply:
        reason = refused(req.user_id, req.message, req.timestamp, req.nonce, req.signature)
        if reason:
            ctx.logger.warning(f"Bridge request refused: {reason}")
            return BridgeReply(ok=False, error=reason)
        session = BRIDGE_PREFIX + req.user_id
        await act(ctx, await flow.on_text(session, req.message[:1500]))
        return bridge_reply(session, flow.session(session).user)

    @agent.on_rest_post("/bridge/status", BridgeStatusRequest, BridgeReply)
    async def bridge_status(ctx: Context, req: BridgeStatusRequest) -> BridgeReply:
        reason = refused(req.user_id, req.task_id, req.timestamp, req.nonce, req.signature)
        if reason:
            return BridgeReply(ok=False, error=reason)
        session = BRIDGE_PREFIX + req.user_id
        return bridge_reply(session, flow.session(session).user, req.task_id)

    @agent.on_event("startup")
    async def startup(ctx: Context):
        ctx.logger.info(f"Technical helper: {helper}")
        ctx.logger.info(f"Caregiver: {caregiver}")
        ctx.logger.info(await flow.task_log.check())
        ctx.logger.info("Groq: " + ("set" if llm.configured() else "MISSING: replies use plain rules only"))
        ctx.logger.info("Bridge: " + ("on" if os.getenv("BRIDGE_SHARED_SECRET", "").strip()
                                      else "OFF (BRIDGE_SHARED_SECRET is empty)"))
        ctx.logger.info("Orchestrator ready")

    async def on_terminal(ctx: Context, sender: str, text: str) -> None:
        await act(ctx, await flow.on_text(LOCAL, text))

    terminal = Terminal(agent, "Hold My Hand Orchestrator", on_terminal, AGENTS["orchestrator"].repl_var)
    return agent, terminal


if __name__ == "__main__":
    run(*create())
