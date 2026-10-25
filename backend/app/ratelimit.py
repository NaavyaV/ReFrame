"""In-process rate limiting: per-user/per-IP request windows and a shared Groq token budget.

Request windows live in memory, so limits apply per server process (run a single worker).
Groq usage is also saved to disk so restarts (including --reload) don't forget the daily spend.
"""

import asyncio
import json
import logging
import math
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("reframe")


class RateLimited(Exception):
    def __init__(self, retry_after: float, message: str = "Too many requests. Wait a moment and try again."):
        super().__init__(message)
        self.retry_after = max(1, math.ceil(retry_after))
        self.message = message


class SlidingWindow:
    """At most `limit` hits per `window` seconds for each key."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise RateLimited(self.window - (now - hits[0]))
        hits.append(now)
        if len(self._hits) > 10_000:
            self._prune(now)

    def _prune(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window]:
            del self._hits[key]


class UserLimiter:
    """Per-user request windows plus one in-flight request per user."""

    def __init__(self, per_minute: int, per_hour: int) -> None:
        self._windows = [SlidingWindow(per_minute, 60), SlidingWindow(per_hour, 3600)]
        self._active: set[str] = set()

    @asynccontextmanager
    async def slot(self, user_id: str, count: bool = True):
        if user_id in self._active:
            raise RateLimited(2, "Still working on your last request.")
        if count:
            for window in self._windows:
                window.hit(user_id)
        self._active.add(user_id)
        try:
            yield
        finally:
            self._active.discard(user_id)


class GroqBudget:
    """Keeps our own Groq calls under the account's request and token limits.

    Each call reserves an estimated token count up front; `settle` swaps it for the real usage.
    After Groq returns a 429 every call fails fast until the retry-after has passed.
    """

    def __init__(
        self, rpm: int, tpm: int, rpd: int, tpd: int, max_wait: float, store: Path | None = None, name: str = ""
    ) -> None:
        # (window seconds, max requests, max tokens)
        self._limits = [(60.0, rpm, tpm), (86400.0, rpd, tpd)]
        self._log: deque[list] = deque()  # [unix time, tokens]
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0
        self.max_wait = max_wait
        self._store = store
        self._name = name
        if store:
            saved = _read_store(store).get(name) or {}
            now = time.time()
            self._log.extend([float(ts), int(tok)] for ts, tok in saved.get("log", []) if now - ts < 86400)
            self._cooldown_until = float(saved.get("cooldown_until", 0))

    def _save(self) -> None:
        if not self._store:
            return
        data = _read_store(self._store)
        data[self._name] = {"log": list(self._log), "cooldown_until": self._cooldown_until}
        tmp = self._store.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._store)
        except OSError as err:
            log.warning("couldn't save Groq usage: %s", err)

    def _wait_needed(self, now: float, tokens: int) -> float:
        wait = 0.0
        for window, max_req, max_tok in self._limits:
            entries = [e for e in self._log if now - e[0] < window]
            if len(entries) >= max_req:
                wait = max(wait, window - (now - entries[0][0]))
            used = sum(e[1] for e in entries)
            if used + tokens > max_tok:
                # Wait until enough old usage leaves the window.
                need = used + tokens - max_tok
                for ts, tok in entries:
                    need -= tok
                    if need <= 0:
                        wait = max(wait, window - (now - ts))
                        break
                else:
                    raise RateLimited(60, "That request is too large for the AI service right now.")
        return wait

    def wait_for(self, tokens: int) -> float:
        """Seconds until a call of this size fits; infinite if it never will."""
        now = time.time()
        if now < self._cooldown_until:
            return self._cooldown_until - now
        try:
            return self._wait_needed(now, tokens)
        except RateLimited:
            return math.inf

    async def acquire(self, tokens: int) -> list:
        async with self._lock:
            now = time.time()
            if now < self._cooldown_until:
                raise RateLimited(self._cooldown_until - now)
            while self._log and now - self._log[0][0] >= self._limits[-1][0]:
                self._log.popleft()
            wait = self._wait_needed(now, tokens)
            if wait > self.max_wait:
                raise RateLimited(wait)
            if wait > 0:
                await asyncio.sleep(wait)
            entry = [time.time(), tokens]
            self._log.append(entry)
            self._save()
            return entry

    def settle(self, entry: list, actual_tokens: int) -> None:
        entry[1] = actual_tokens
        self._save()

    def cooldown(self, seconds: float) -> None:
        self._cooldown_until = max(self._cooldown_until, time.time() + seconds)
        self._save()


def _read_store(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
