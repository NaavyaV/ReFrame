"""The caregiver agent's rules, without the uAgents plumbing.

Access works like this:
  1. The person asks the orchestrator to add a caregiver, picks which settings they
     may change, and confirms. The orchestrator (the only sender trusted to speak for
     a person) asks for an invite; this agent stores it and returns a one-time code
     that expires after INVITE_HOURS.
  2. The caregiver sends the code here. Their agent address, which uAgents verifies
     with a signature on every message, becomes the caregiver's identity.
  3. The caregiver can then change only the approved settings, to allowed values.
     Each change is saved with an audit row and passed to the orchestrator, which
     tells the person and lets them undo it.
Nothing is changed when storage isn't available, and settings that could affect what
gets bought (budgets, shops, payments) are always refused.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Callable

from shared.models import (
    CAREGIVER,
    ORCHESTRATOR,
    CaregiverInviteRequest,
    CaregiverInviteResult,
    CaregiverPreferenceProposal,
    CaregiverPreferenceResult,
    ErrorInfo,
    PreferenceQuery,
    PreferenceSnapshot,
    UserRevokeRequest,
    new_id,
    now_iso,
)
from shared.preferences import SETTINGS, Change, Refused, describe, parse_change, valid
from shared.security import short_address

from caretaker.store import Grant, PreferenceStore, StoreError

INVITE_HOURS = 24
MAX_CODE_FAILURES = 5
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE = re.compile(r"\b([A-HJ-NP-Z2-9]{4})-?([A-HJ-NP-Z2-9]{4})\b")
USER_REF = re.compile(r"\bhmh-u-[0-9a-f]{12}\b")

HELP = (
    "- \"How is <name> doing?\"\n"
    "- A code from Hold My Hand (ABCD-EFGH), then \"turn on simpler wording\"\n"
    "- \"Show settings\""
)


def code_hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").upper().encode()).hexdigest()


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _iso(micros: int) -> str:
    return datetime.fromtimestamp(micros / 1_000_000, timezone.utc).isoformat(timespec="minutes")


class CaregiverService:
    def __init__(self, store: PreferenceStore | None, *, orchestrator: str,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.orchestrator = orchestrator
        self._clock = clock
        self._failures: dict[str, list[float]] = {}

    async def _unavailable(self) -> ErrorInfo | None:
        if self.store is None:
            return ErrorInfo(code="storage_unavailable",
                             message="caregiver storage isn't configured (SPACETIME_DB_NAME and SPACETIME_TOKEN).")
        reason = await self.store.ready()
        return ErrorInfo(code="storage_unavailable", message=reason) if reason else None

    # From the orchestrator, speaking for a person

    async def invite(self, sender: str, request: CaregiverInviteRequest) -> CaregiverInviteResult:
        def result(status: str, error: ErrorInfo | None = None, **extra) -> CaregiverInviteResult:
            return CaregiverInviteResult(request_id=request.request_id, sent_at=now_iso(), user_ref=request.user_ref,
                                         status=status, error=error, **extra)

        if sender != self.orchestrator:
            return result("denied", ErrorInfo(code="unauthorized_sender",
                                              message="Only the Hold My Hand Orchestrator can create invites."))
        scopes = [scope for scope in dict.fromkeys(request.scopes) if scope in SETTINGS]
        if not scopes or not request.approval.strip():
            return result("denied", ErrorInfo(code="bad_request",
                                              message="An invite needs the person's approval and at least one setting."))
        problem = await self._unavailable()
        if problem:
            return result("error", problem)
        code = new_code()
        expires = int((self._clock() + INVITE_HOURS * 3600) * 1_000_000)
        grant = Grant(new_id("grant"), request.user_ref, "", scopes, "pending", code_hash(code), expires,
                      request.approval[:200])
        try:
            await self.store.save_grant(grant, f"user:{request.user_ref}", "invite_created")
        except StoreError as exc:
            return result("error", ErrorInfo(code="storage_error", message=str(exc)))
        return result("created", invite_code=code, expires_at=_iso(expires), scopes=scopes)

    async def snapshot(self, sender: str, request: PreferenceQuery | UserRevokeRequest) -> PreferenceSnapshot:
        def result(status: str, error: ErrorInfo | None = None, **extra) -> PreferenceSnapshot:
            return PreferenceSnapshot(request_id=request.request_id, sent_at=now_iso(), user_ref=request.user_ref,
                                      status=status, error=error, **extra)

        if sender != self.orchestrator:
            return result("error", ErrorInfo(code="unauthorized_sender",
                                             message="Only the Hold My Hand Orchestrator can read a person's settings."))
        problem = await self._unavailable()
        if problem:
            return result("unavailable", problem)
        try:
            prefs = await self.store.preferences(request.user_ref)
            grants = await self.store.grants_for_user(request.user_ref)
        except StoreError as exc:
            return result("error", ErrorInfo(code="storage_error", message=str(exc)))
        values = {key: pref.value for key, pref in prefs.items() if valid(key, pref.value)}
        set_by = {key: short_address(pref.set_by) for key, pref in prefs.items()
                  if key in values and not pref.set_by.startswith("user:")}
        caregivers = [short_address(grant.caregiver) for grant in grants if grant.status == "active"]
        return result("ok", preferences=values, set_by=set_by, caregivers=caregivers)

    async def user_revoke(self, sender: str, request: UserRevokeRequest) -> PreferenceSnapshot:
        if sender != self.orchestrator:
            return await self.snapshot(sender, request)
        problem = await self._unavailable()
        if problem:
            return await self.snapshot(sender, request)
        actor = f"user:{request.user_ref}"
        try:
            for key, pref in (await self.store.preferences(request.user_ref)).items():
                if pref.set_by.startswith("user:") or (request.setting and key != request.setting):
                    continue
                await self.store.clear_preference(request.user_ref, key, actor, request.approval[:200])
            if request.revoke_access:
                for grant in await self.store.grants_for_user(request.user_ref):
                    if grant.status in {"pending", "active"}:
                        grant.status = "revoked"
                        grant.code_hash = ""
                        await self.store.save_grant(grant, actor, "access_revoked")
        except StoreError as exc:
            return PreferenceSnapshot(request_id=request.request_id, sent_at=now_iso(), user_ref=request.user_ref,
                                      status="error", error=ErrorInfo(code="storage_error", message=str(exc)))
        return await self.snapshot(sender, request)

    # From caregivers. `sender` is their verified agent address.

    def _locked(self, sender: str) -> bool:
        now = self._clock()
        recent = [at for at in self._failures.get(sender, []) if now - at < 3600]
        self._failures[sender] = recent
        return len(recent) >= MAX_CODE_FAILURES

    async def accept_code(self, sender: str, code: str) -> str:
        if self._locked(sender):
            return "Too many codes that didn't work. Please wait an hour and try again."
        problem = await self._unavailable()
        if problem:
            return f"I can't link you right now: {problem.message}"
        try:
            grant = await self.store.grant_by_code(code_hash(code))
        except StoreError as exc:
            return f"I can't check that code right now: {exc}"
        if grant is None or grant.status != "pending" or grant.expires_micros < self._clock() * 1_000_000:
            self._failures.setdefault(sender, []).append(self._clock())
            return "That code didn't work. It may be mistyped, used already or expired. Ask the person for a new one."
        if sender == self.orchestrator:
            return "That code is for a caregiver."
        grant.caregiver = sender
        grant.status = "active"
        grant.code_hash = ""
        try:
            await self.store.save_grant(grant, sender, "invite_accepted")
        except StoreError as exc:
            return f"I couldn't save that: {exc}"
        names = ", ".join(SETTINGS[key].label for key in grant.scopes)
        return (f"You're now linked as a caregiver for person {grant.user_ref}. You may change: {names}. "
                "Tell me what to change, for example \"turn on simpler wording\".")

    async def _grant_for(self, sender: str, user_ref: str | None) -> tuple[Grant | None, ErrorInfo | None]:
        try:
            grants = [g for g in await self.store.grants_for_caregiver(sender) if g.status == "active"]
        except StoreError as exc:
            return None, ErrorInfo(code="storage_error", message=str(exc))
        if user_ref:
            grants = [g for g in grants if g.user_ref == user_ref]
        if not grants:
            return None, ErrorInfo(code="no_access", message="You don't have caregiver access for that person. Ask "
                                                             "them for a code from Hold My Hand.")
        if len({g.user_ref for g in grants}) > 1:
            people = ", ".join(sorted({g.user_ref for g in grants}))
            return None, ErrorInfo(code="which_person", message=f"You help more than one person ({people}). Add "
                                                                "\"for hmh-u-...\" to say which.")
        return grants[0], None

    async def propose(self, sender: str, user_ref: str | None, setting: str, value: str,
                      proposal_id: str = "") -> CaregiverPreferenceResult:
        proposal_id = proposal_id or new_id("change")

        def result(status: str, error: ErrorInfo | None = None, **extra) -> CaregiverPreferenceResult:
            return CaregiverPreferenceResult(proposal_id=proposal_id, recipient_role=CAREGIVER, sent_at=now_iso(),
                                             caregiver_id=sender, user_ref=extra.pop("user", user_ref or ""),
                                             setting=setting, status=status, error=error, **extra)

        problem = await self._unavailable()
        if problem:
            return result("error", problem)
        if sender == self.orchestrator:
            return result("rejected", ErrorInfo(code="unauthorized_sender", message="Caregivers only."))
        grant, error = await self._grant_for(sender, user_ref)
        if grant is None:
            return result("rejected", error)
        if setting not in SETTINGS:
            return result("rejected", ErrorInfo(code="not_allowed", message="That isn't a setting a caregiver can change."),
                          user=grant.user_ref)
        if setting not in grant.scopes:
            return result("rejected", ErrorInfo(
                code="out_of_scope", message=f"The person didn't allow you to change {SETTINGS[setting].label}."),
                user=grant.user_ref)
        if not valid(setting, value):
            return result("rejected", ErrorInfo(code="invalid_value", message="That value isn't allowed."),
                          user=grant.user_ref)
        approval = f"grant {grant.grant_id}; person approved: {', '.join(grant.scopes)}; {grant.approval}"[:200]
        try:
            if value == SETTINGS[setting].default:
                old = await self.store.clear_preference(grant.user_ref, setting, sender, approval)
                status = "cleared"
            else:
                old = await self.store.set_preference(grant.user_ref, setting, value, sender, grant.grant_id, approval)
                status = "applied"
        except StoreError as exc:
            return result("error", ErrorInfo(code="storage_error", message=str(exc)), user=grant.user_ref)
        return result(status, user=grant.user_ref, old_value=old, new_value=value, scope=list(grant.scopes),
                      approval=approval)

    async def on_proposal(self, sender: str, proposal: CaregiverPreferenceProposal) -> CaregiverPreferenceResult:
        if proposal.caregiver_id != sender:
            return CaregiverPreferenceResult(
                proposal_id=proposal.proposal_id, recipient_role=CAREGIVER, sent_at=now_iso(), caregiver_id=sender,
                user_ref=proposal.user_ref, setting=proposal.setting, status="rejected",
                error=ErrorInfo(code="unauthorized_sender", message="caregiver_id must be the sender's own address."))
        return await self.propose(sender, proposal.user_ref, proposal.setting, proposal.proposed_value,
                                  proposal.proposal_id)

    async def view(self, sender: str) -> str:
        problem = await self._unavailable()
        if problem:
            return f"I can't show settings right now: {problem.message}"
        try:
            grants = [g for g in await self.store.grants_for_caregiver(sender) if g.status == "active"]
            if not grants:
                return "You aren't linked to anyone yet. Ask the person for a code from Hold My Hand."
            lines = []
            for grant in grants:
                prefs = await self.store.preferences(grant.user_ref)
                lines.append(f"Person {grant.user_ref}:")
                for key in grant.scopes:
                    value = prefs[key].value if key in prefs else SETTINGS[key].default
                    lines.append(f"- {describe(key, value)}")
        except StoreError as exc:
            return f"I can't show settings right now: {exc}"
        return "\n".join(lines)

    async def chat(self, sender: str, text: str) -> tuple[str, CaregiverPreferenceResult | None]:
        """A caregiver's message: (reply, the change to report to the orchestrator, if any)."""
        code = CODE.search(text.upper())
        if code and re.fullmatch(r"\s*(?:code\s*:?\s*)?[A-Za-z2-9]{4}-?[A-Za-z2-9]{4}\s*\.?\s*", text, re.I):
            return await self.accept_code(sender, code.group(1) + code.group(2)), None
        lowered = text.lower()
        if re.search(r"\b(?:help|what can (?:i|you) do|how does this work)\b", lowered) or lowered.strip() in {"hi", "hello"}:
            return HELP, None
        if re.search(r"\b(?:show|view|list|see|what are)\b.*\bsettings?\b|^settings$", lowered.strip()):
            return await self.view(sender), None
        user = USER_REF.search(lowered)
        change = parse_change(text)
        if isinstance(change, Refused):
            return change.reason, None
        if isinstance(change, Change):
            result = await self.propose(sender, user.group(0) if user else None, change.setting, change.value)
            if result.status in {"applied", "cleared"}:
                return (f"Done: {describe(change.setting, change.value)} for person {result.user_ref}. They'll see "
                        "a note about this change and can undo it."), result
            return f"I didn't change anything: {result.error.message if result.error else 'it was refused'}", None
        if code and "-" in code.group(0):
            return await self.accept_code(sender, code.group(1) + code.group(2)), None
        return HELP, None


def for_orchestrator(result: CaregiverPreferenceResult) -> CaregiverPreferenceResult:
    """The same result, addressed to the orchestrator so it can tell the person."""
    return result.copy(update={"recipient_role": ORCHESTRATOR, "sent_at": now_iso()})
