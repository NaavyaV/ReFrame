"""Hold My Hand Caregiver uAgent.

Lets a caregiver the person has approved adjust a few response settings (simpler
wording, shorter explanations, larger-text hints, language, checking before bigger
steps, pointing out clear features). It is not an administrator: it can't buy,
read conversations, change recommendations or touch other agents. The person
approves access through the orchestrator, sees every change, and can undo it.

Settings live in SpacetimeDB (caregiver_grant, user_preference, preference_audit).
Until those tables are published, every change is refused with an explanation.

This folder is still called caretaker so paths and CARETAKER_PORT keep working;
the identity comes from CAREGIVER_SEED.

Run on its own:  python agents/caretaker/agent.py   (port 8020 unless CARETAKER_PORT is set)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.agents import AGENTS, Terminal, address, chat_protocol, deliver, make_agent, own_agent, run  # noqa: E402
from shared.models import (  # noqa: E402
    CAREGIVER_PROTOCOL,
    CaregiverInviteRequest,
    CaregiverInviteResult,
    CaregiverPreferenceProposal,
    CaregiverPreferenceResult,
    PreferenceQuery,
    PreferenceSnapshot,
    UserRevokeRequest,
)
from shared.spacetime import Spacetime, SpacetimeNotConfigured  # noqa: E402
from uagents import Agent, Context, Protocol  # noqa: E402

from caretaker.flow import CaregiverService, for_orchestrator  # noqa: E402
from caretaker.store import PreferenceStore, SpacetimeStore  # noqa: E402


def default_store() -> PreferenceStore | None:
    try:
        return SpacetimeStore(Spacetime.from_env())
    except SpacetimeNotConfigured:
        return None


def create(service: CaregiverService | None = None, **agent_options) -> tuple[Agent, Terminal]:
    agent = make_agent("caregiver", **agent_options)
    orchestrator = address("orchestrator")
    service = service or CaregiverService(default_store(), orchestrator=orchestrator)

    proto = Protocol(name=CAREGIVER_PROTOCOL[0], version=CAREGIVER_PROTOCOL[1])

    @proto.on_message(CaregiverInviteRequest, replies={CaregiverInviteResult})
    async def on_invite(ctx: Context, sender: str, msg: CaregiverInviteRequest):
        result = await service.invite(sender, msg)
        ctx.logger.info(f"Invite for {msg.user_ref}: {result.status}" + (f" ({result.error.code})" if result.error else ""))
        await ctx.send(sender, result)

    @proto.on_message(PreferenceQuery, replies={PreferenceSnapshot})
    async def on_query(ctx: Context, sender: str, msg: PreferenceQuery):
        await ctx.send(sender, await service.snapshot(sender, msg))

    @proto.on_message(UserRevokeRequest, replies={PreferenceSnapshot})
    async def on_revoke(ctx: Context, sender: str, msg: UserRevokeRequest):
        snapshot = await service.user_revoke(sender, msg)
        ctx.logger.info(f"Revoke for {msg.user_ref} (access too: {msg.revoke_access}): {snapshot.status}")
        await ctx.send(sender, snapshot)

    @proto.on_message(CaregiverPreferenceProposal, replies={CaregiverPreferenceResult})
    async def on_proposal(ctx: Context, sender: str, msg: CaregiverPreferenceProposal):
        result = await service.on_proposal(sender, msg)
        ctx.logger.info(f"Proposal {msg.setting} for {msg.user_ref} from {sender}: {result.status}")
        await ctx.send(sender, result)
        if result.status in {"applied", "cleared"}:
            await ctx.send(orchestrator, for_orchestrator(result))

    agent.include(proto, publish_manifest=True)

    async def on_chat(ctx: Context, sender: str, text: str) -> None:
        reply, change = await service.chat(sender, text)
        if change is not None:
            ctx.logger.info(f"Caregiver {sender} set {change.setting} for {change.user_ref}: {change.status}")
            await ctx.send(orchestrator, for_orchestrator(change))
        await deliver(ctx, sender, reply)

    agent.include(chat_protocol(on_chat, ignore=own_agent), publish_manifest=True)

    @agent.on_event("startup")
    async def startup(ctx: Context):
        if service.store is None:
            ctx.logger.warning("Caregiver storage: OFF (SPACETIME_DB_NAME/SPACETIME_TOKEN not set). All changes are refused.")
        else:
            reason = await service.store.ready()
            if reason:
                ctx.logger.warning(f"Caregiver storage: OFF, {reason}")
            else:
                ctx.logger.info("Caregiver storage: ready")
        ctx.logger.info(f"Caregiver ready. Trusts person requests only from the orchestrator {orchestrator}")

    terminal = Terminal(agent, "Hold My Hand Caregiver (you are a caregiver called 'local')", on_chat,
                        AGENTS["caregiver"].repl_var)
    return agent, terminal


if __name__ == "__main__":
    run(*create())
