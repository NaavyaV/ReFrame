"""In-process rate limiting: per-user/per-IP request windows and a shared Groq token budget.

State lives in memory, so limits apply per server process (run a single worker).
"""

import asyncio
import math
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager


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
    async def slot(self, user_id: str):
        if user_id in self._active:
            raise RateLimited(2, "Still working on your last request.")
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

    def __init__(self, rpm: int, tpm: int, rpd: int, tpd: int, max_wait: float) -> None:
        # (window seconds, max requests, max tokens)
        self._limits = [(60.0, rpm, tpm), (86400.0, rpd, tpd)]
        self._log: deque[list] = deque()  # [timestamp, tokens]
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0
        self.max_wait = max_wait

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
        now = time.monotonic()
        if now < self._cooldown_until:
            return self._cooldown_until - now
        try:
            return self._wait_needed(now, tokens)
        except RateLimited:
            return math.inf

    async def acquire(self, tokens: int) -> list:
        async with self._lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                raise RateLimited(self._cooldown_until - now)
            while self._log and now - self._log[0][0] >= self._limits[-1][0]:
                self._log.popleft()
            wait = self._wait_needed(now, tokens)
            if wait > self.max_wait:
                raise RateLimited(wait)
            if wait > 0:
                await asyncio.sleep(wait)
            entry = [time.monotonic(), tokens]
            self._log.append(entry)
            return entry

    @staticmethod
    def settle(entry: list, actual_tokens: int) -> None:
        entry[1] = actual_tokens

    def cooldown(self, seconds: float) -> None:
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)
