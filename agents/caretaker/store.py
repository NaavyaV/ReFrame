"""Where caregiver access, settings and their history are kept.

SpacetimeStore uses three private SpacetimeDB tables (caregiver_grant,
user_preference, preference_audit). Every write is a reducer that also appends an
audit row, so a change can't be saved without a record of who made it and why.
If the tables don't exist yet, ready() says so and the caregiver agent refuses
every change. MemoryStore is the same thing in memory, for tests.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from shared.spacetime import Spacetime, SpacetimeError, check_tables, field as column, sql_literal

TABLES = ["caregiver_grant", "user_preference", "preference_audit"]
RECHECK_SECONDS = 300


class StoreError(Exception):
    pass


@dataclass
class Grant:
    grant_id: str
    user_ref: str
    caregiver: str  # the caregiver's agent address; empty until they use the invite code
    scopes: list[str]
    status: str  # "pending", "active" or "revoked"
    code_hash: str = ""
    expires_micros: int = 0
    approval: str = ""


@dataclass
class Pref:
    value: str
    set_by: str
    grant_id: str = ""


@dataclass
class AuditEntry:
    user_ref: str
    actor: str
    action: str
    setting: str
    old_value: str
    new_value: str
    approval: str
    at: float = field(default_factory=time.time)


class PreferenceStore:
    async def ready(self) -> str | None:
        """None when usable, otherwise why not."""
        raise NotImplementedError

    async def save_grant(self, grant: Grant, actor: str, action: str) -> None:
        raise NotImplementedError

    async def grant_by_code(self, code_hash: str) -> Grant | None:
        raise NotImplementedError

    async def grants_for_user(self, user_ref: str) -> list[Grant]:
        raise NotImplementedError

    async def grants_for_caregiver(self, caregiver: str) -> list[Grant]:
        raise NotImplementedError

    async def preferences(self, user_ref: str) -> dict[str, Pref]:
        raise NotImplementedError

    async def set_preference(self, user_ref: str, setting: str, value: str, actor: str, grant_id: str,
                             approval: str) -> str:
        """Save a setting and its audit row. Returns the old value ("" if it wasn't set)."""
        raise NotImplementedError

    async def clear_preference(self, user_ref: str, setting: str, actor: str, approval: str) -> str:
        raise NotImplementedError

    async def audit(self, user_ref: str, limit: int = 20) -> list[AuditEntry]:
        raise NotImplementedError


class MemoryStore(PreferenceStore):
    """For tests and for trying the flow locally. Nothing survives a restart."""

    def __init__(self) -> None:
        self.grants: dict[str, Grant] = {}
        self.prefs: dict[str, dict[str, Pref]] = {}
        self.log: list[AuditEntry] = []

    async def ready(self) -> str | None:
        return None

    async def save_grant(self, grant: Grant, actor: str, action: str) -> None:
        old = self.grants.get(grant.grant_id)
        self.grants[grant.grant_id] = Grant(**{**grant.__dict__, "scopes": list(grant.scopes)})
        self.log.append(AuditEntry(grant.user_ref, actor, action, "", old.status if old else "", grant.status,
                                   grant.approval))

    async def grant_by_code(self, code_hash: str) -> Grant | None:
        return next((g for g in self.grants.values() if code_hash and g.code_hash == code_hash), None)

    async def grants_for_user(self, user_ref: str) -> list[Grant]:
        return [g for g in self.grants.values() if g.user_ref == user_ref]

    async def grants_for_caregiver(self, caregiver: str) -> list[Grant]:
        return [g for g in self.grants.values() if caregiver and g.caregiver == caregiver]

    async def preferences(self, user_ref: str) -> dict[str, Pref]:
        return dict(self.prefs.get(user_ref, {}))

    async def set_preference(self, user_ref, setting, value, actor, grant_id, approval) -> str:
        prefs = self.prefs.setdefault(user_ref, {})
        old = prefs[setting].value if setting in prefs else ""
        prefs[setting] = Pref(value, actor, grant_id)
        self.log.append(AuditEntry(user_ref, actor, "set", setting, old, value, approval))
        return old

    async def clear_preference(self, user_ref, setting, actor, approval) -> str:
        prefs = self.prefs.setdefault(user_ref, {})
        old = prefs.pop(setting).value if setting in prefs else ""
        self.log.append(AuditEntry(user_ref, actor, "clear", setting, old, "", approval))
        return old

    async def audit(self, user_ref: str, limit: int = 20) -> list[AuditEntry]:
        return [entry for entry in self.log if entry.user_ref == user_ref][-limit:][::-1]


def _grant(row: dict[str, Any]) -> Grant:
    scopes = column(row, "scopes") or []
    if isinstance(scopes, str):
        try:
            scopes = json.loads(scopes)
        except json.JSONDecodeError:
            scopes = []
    return Grant(
        grant_id=str(column(row, "grant_id", "grantId") or ""),
        user_ref=str(column(row, "user_ref", "userRef") or ""),
        caregiver=str(column(row, "caregiver_address", "caregiverAddress") or ""),
        scopes=[str(scope) for scope in scopes] if isinstance(scopes, list) else [],
        status=str(column(row, "status") or ""),
        code_hash=str(column(row, "invite_code_hash", "inviteCodeHash") or ""),
        expires_micros=int(column(row, "invite_expires_micros", "inviteExpiresMicros") or 0),
        approval=str(column(row, "approval") or ""),
    )


class SpacetimeStore(PreferenceStore):
    def __init__(self, db: Spacetime | None = None) -> None:
        self.db = db
        self._reason: str | None = "not checked yet"
        self._checked = -RECHECK_SECONDS * 2.0

    async def ready(self) -> str | None:
        now = time.monotonic()
        if self._reason is None or now - self._checked < RECHECK_SECONDS:
            return self._reason
        self._checked = now
        if self.db is None:
            self.db, status = await check_tables(TABLES)
        else:
            status = await self.db.preflight(TABLES)
        missing = [table for table, state in status.items() if state == "missing"]
        broken = [state for state in status.values() if state not in {"ok", "missing"}]
        if missing:
            self._reason = (f"caregiver storage isn't set up yet: SpacetimeDB is missing the table(s) "
                            f"{', '.join(missing)}. Publish the updated module (README, 'Database tables for the "
                            "agents'). Until then no caregiver changes can be saved.")
        elif broken:
            self._reason = f"caregiver storage isn't reachable: {broken[0]}"
        else:
            self._reason = None
        return self._reason

    def _db(self) -> Spacetime:
        if self.db is None:
            raise StoreError("SpacetimeDB isn't configured.")
        return self.db

    async def _sql(self, query: str) -> list[dict[str, Any]]:
        try:
            return await self._db().sql(query)
        except SpacetimeError as exc:
            raise StoreError(str(exc)) from exc

    async def _call(self, reducer: str, *args: Any) -> None:
        try:
            await self._db().call(reducer, *args)
        except SpacetimeError as exc:
            raise StoreError(str(exc)) from exc

    async def save_grant(self, grant: Grant, actor: str, action: str) -> None:
        await self._call("save_caregiver_grant", grant.grant_id, grant.user_ref, grant.caregiver,
                         list(grant.scopes), grant.status, grant.code_hash, grant.expires_micros,
                         grant.approval, actor, action)

    async def grant_by_code(self, code_hash: str) -> Grant | None:
        rows = await self._sql(f"SELECT * FROM caregiver_grant WHERE invite_code_hash = {sql_literal(code_hash)}")
        return _grant(rows[0]) if rows else None

    async def grants_for_user(self, user_ref: str) -> list[Grant]:
        return [_grant(row) for row in await self._sql(
            f"SELECT * FROM caregiver_grant WHERE user_ref = {sql_literal(user_ref)}")]

    async def grants_for_caregiver(self, caregiver: str) -> list[Grant]:
        return [_grant(row) for row in await self._sql(
            f"SELECT * FROM caregiver_grant WHERE caregiver_address = {sql_literal(caregiver)}")]

    async def preferences(self, user_ref: str) -> dict[str, Pref]:
        prefs: dict[str, Pref] = {}
        for row in await self._sql(f"SELECT * FROM user_preference WHERE user_ref = {sql_literal(user_ref)}"):
            value = column(row, "value") or ""
            prefs[str(column(row, "setting"))] = Pref(str(value), str(column(row, "set_by", "setBy") or ""),
                                                      str(column(row, "grant_id", "grantId") or ""))
        return prefs

    async def set_preference(self, user_ref, setting, value, actor, grant_id, approval) -> str:
        old = (await self.preferences(user_ref)).get(setting)
        await self._call("set_user_preference", user_ref, setting, value, actor, grant_id, approval)
        return old.value if old else ""

    async def clear_preference(self, user_ref, setting, actor, approval) -> str:
        old = (await self.preferences(user_ref)).get(setting)
        await self._call("clear_user_preference", user_ref, setting, actor, approval)
        return old.value if old else ""

    async def audit(self, user_ref: str, limit: int = 20) -> list[AuditEntry]:
        rows = await self._sql(f"SELECT * FROM preference_audit WHERE user_ref = {sql_literal(user_ref)}")
        rows.sort(key=lambda row: int(column(row, "audit_id", "auditId") or 0), reverse=True)
        return [
            AuditEntry(user_ref, str(column(row, "actor") or ""), str(column(row, "action") or ""),
                       str(column(row, "setting") or ""), str(column(row, "old_value", "oldValue") or ""),
                       str(column(row, "new_value", "newValue") or ""), str(column(row, "approval") or ""), 0.0)
            for row in rows[:limit]
        ]
