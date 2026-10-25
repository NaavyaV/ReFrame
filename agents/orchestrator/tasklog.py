"""Task history in SpacetimeDB's task_request table, when that table exists.

Startup checks the table once. If it's missing (the module with it hasn't been
published yet) history is switched off with one clear message and shopping keeps
working. Only a task id, a pseudonymous user ref, the kind, status and a short
summary such as "vacuum under $100" are stored, never what the person typed.
"""

from __future__ import annotations

import logging

from shared.spacetime import Spacetime, SpacetimeError, check_tables

TABLE = "task_request"
log = logging.getLogger("hmh.tasklog")


class TaskLog:
    def __init__(self, db: Spacetime | None = None) -> None:
        self.db = db
        self.enabled = False
        self.reason = "not checked yet"

    async def check(self) -> str:
        """Look for the table. Returns a line for the startup log."""
        if self.db is None:
            self.db, status = await check_tables([TABLE])
        else:
            status = await self.db.preflight([TABLE])
        state = status[TABLE]
        self.enabled = state == "ok"
        if self.enabled:
            self.reason = ""
            return f"Task history: saving to SpacetimeDB table {TABLE}."
        if state == "missing":
            self.reason = (f"table '{TABLE}' doesn't exist in this database yet. Publish the updated module "
                           "(README, 'Database tables for the agents'); shopping still works without it.")
        else:
            self.reason = state
        return f"Task history is OFF: {self.reason}"

    async def record(self, task_id: str, user: str, kind: str, status: str, detail: str) -> None:
        if not self.enabled or self.db is None:
            return
        try:
            await self.db.call("upsert_task_request", task_id, user, kind, status, detail[:200])
        except SpacetimeError as exc:
            self.enabled = False
            self.reason = str(exc)
            log.warning(f"Task history switched off: {exc}")
