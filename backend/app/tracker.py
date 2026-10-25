"""Live agent tracker: writes each run's progress to SpacetimeDB under the ReFrame user's id.

Writes are queued in order and run in the background, so a slow or unreachable database never
delays the page. Only labels and counts are sent, never form field values.
"""

import asyncio
import logging
import time
import uuid
from urllib.parse import urlsplit

from .auth import PublicUser
from .schemas import ReframeRequest, ReframeResponse, SnapElement
from .stdb import SpacetimeClient

log = logging.getLogger("reframe.tracker")
FLUSH_TIMEOUT_S = 0.4
_pending: set[asyncio.Task] = set()
# When each user last cleared the tracker; writes from runs started before that are dropped.
_cleared_at: dict[str, float] = {}


async def clear_user_runs(stdb: SpacetimeClient, user_id: str) -> None:
    _cleared_at[user_id] = time.monotonic()
    await stdb.call("clear_runs", user_id)


class Tracker:
    def __init__(self, stdb: SpacetimeClient, user: PublicUser, req: ReframeRequest) -> None:
        self._stdb = stdb
        self._user = user
        self._req = req
        self._chain: asyncio.Task | None = None
        self.run_id = uuid.uuid4().hex[:16]
        self._created = time.monotonic()

    def _queue(self, reducer: str, *args) -> None:
        previous = self._chain

        async def send() -> None:
            if previous:
                await asyncio.gather(previous, return_exceptions=True)
            if _cleared_at.get(self._user.user_id, 0) >= self._created:
                return
            try:
                await self._stdb.call(reducer, *args)
            except Exception as err:  # the tracker must never break a reframe
                log.warning("tracker %s failed: %s", reducer, err)

        self._chain = asyncio.create_task(send())
        _pending.add(self._chain)
        self._chain.add_done_callback(_pending.discard)

    def start(self) -> None:
        url = self._req.snapshot.url
        self._queue(
            "start_run",
            self.run_id,
            self._user.user_id,
            self._user.display_name or "ReFrame user",
            urlsplit(url).netloc,
            url,
            self._req.message,
            "auto" if self._req.continuation else "typed",
        )

    def event(self, kind: str, target: str, detail: str = "", step: str = "", progress: int = 0) -> None:
        self._queue("log_event", self.run_id, kind, target, detail, step, max(0, min(progress, 100)))

    def alert(self, severity: str, title: str, detail: str = "") -> None:
        self._queue("raise_alert", self.run_id, severity, title, detail)

    def finish(self, status: str, reply: str) -> None:
        self._queue("finish_run", self.run_id, status, reply)

    async def flush(self) -> None:
        if self._chain:
            await asyncio.wait({self._chain}, timeout=FLUSH_TIMEOUT_S)

    def record_result(self, res: ReframeResponse) -> None:
        """Summarize a model or quick-command result as tracker events."""
        by_ref: dict[str, SnapElement] = {e.ref: e for e in self._req.snapshot.elements}

        def name(ref: str) -> str:
            el = by_ref.get(ref)
            return (el and (el.label or el.text or el.role)) or ref

        ops = res.operations or []
        total = max(len(ops), 1)
        for i, op in enumerate(ops, 1):
            progress = 30 + int(60 * i / total)
            kind = op.op
            if kind == "hide":
                for ref in (op.refs or [])[:12]:
                    self.event("remove", name(ref), "hidden", "Removing clutter", progress)
            elif kind == "render":
                for ref in (op.keep_refs or [])[:6]:
                    self.event("keep", name(ref), "kept from the page", "Rebuilding the layout", progress)
                self.event("change", "Page layout", "rebuilt as a simple panel", "Rebuilding the layout", progress)
            elif kind == "page_style":
                changed = [k for k, v in op.model_dump(exclude_none=True).items() if k != "op" and v not in (False, None)]
                self.event("change", "Page style", ", ".join(changed) or "reset", "Adjusting the look", progress)
            elif kind == "restore":
                self.event("change", "Whole page", "restored to the original", "Restoring", progress)
            else:
                self.event("change", kind.replace("_", " ").capitalize(), "", "Updating the page", progress)
