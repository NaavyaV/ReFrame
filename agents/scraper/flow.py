"""Product search for the scraper: run the site's Apify Actor, normalise, filter, return facts.

Limits on every run: at most FETCH_ITEMS dataset items, a MAX_CHARGE_USD spending cap,
RUN_TIMEOUT_SECONDS of Actor time, MAX_RUNS_PER_HOUR across the process, and a short
cache so a repeated search costs nothing. Results are facts from the listing only;
the technical helper decides which, if any, is good enough.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable

from shared.features import words
from shared.models import (
    ErrorInfo,
    ProductCandidate,
    ScrapeFilters,
    ScrapeRequest,
    ScrapeResponse,
    SearchMetadata,
    now_iso,
)
from shared.security import clean_text

from scraper.actors import SITES, ActorConfig

MAX_CANDIDATES = 3
FETCH_ITEMS = 6
RUN_TIMEOUT_SECONDS = 75
WAIT_SECONDS = 90
MAX_CHARGE_USD = Decimal("0.05")
MAX_RUNS_PER_HOUR = 20
MAX_CONCURRENT_RUNS = 2
CACHE_SECONDS = 600

# Errors the technical helper may retry; anything else ends the search.
RETRYABLE = {"timeout", "rate_limited", "actor_failed"}


class RunBudget:
    """At most `limit` Actor runs in any rolling hour."""

    def __init__(self, limit: int = MAX_RUNS_PER_HOUR, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self._clock = clock
        self._runs: deque[float] = deque()

    def take(self) -> bool:
        now = self._clock()
        while self._runs and now - self._runs[0] > 3600:
            self._runs.popleft()
        if len(self._runs) >= self.limit:
            return False
        self._runs.append(now)
        return True


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:60]


def apply_filters(
    candidates: list[ProductCandidate], filters: ScrapeFilters
) -> tuple[list[ProductCandidate], list[str]]:
    """Drop duplicates and listings that plainly don't fit the request's factual filters."""
    kept: list[ProductCandidate] = []
    dropped: list[str] = []
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    terms = [words(term) for term in filters.must_include_any if term.strip()]
    excluded = set(filters.exclude_urls)
    for candidate in candidates:
        key = _title_key(candidate.title)
        if candidate.id in seen_ids or key in seen_titles:
            dropped.append(f"duplicate: {candidate.title[:60]}")
            continue
        seen_ids.add(candidate.id)
        seen_titles.add(key)
        if candidate.url in excluded:
            dropped.append(f"already suggested: {candidate.title[:60]}")
            continue
        haystack = words(candidate.title + " " + candidate.attributes.get("Category", ""))
        if terms and not any(term <= haystack for term in terms):
            dropped.append(f"not relevant: {candidate.title[:60]}")
            continue
        if candidate.price is not None and candidate.currency == filters.currency:
            if filters.max_price is not None and candidate.price > filters.max_price:
                dropped.append(f"over {filters.max_price:g} {filters.currency}: {candidate.title[:60]}")
                continue
            if filters.min_price is not None and candidate.price < filters.min_price:
                dropped.append(f"under {filters.min_price:g} {filters.currency}: {candidate.title[:60]}")
                continue
        kept.append(candidate)
    return kept, dropped


def _status(run: Any) -> str:
    status = getattr(run, "status", None)
    if isinstance(run, dict):
        status = run.get("status")
    return str(getattr(status, "value", status) or "").upper()


def _attr(run: Any, name: str, camel: str) -> str:
    if isinstance(run, dict):
        return str(run.get(camel) or run.get(name) or "")
    return str(getattr(run, name, "") or "")


def default_client_factory(token: str) -> Any:
    from apify_client import ApifyClientAsync

    return ApifyClientAsync(token=token, max_retries=2)


def _api_error(exc: Exception) -> ErrorInfo:
    """Map an Apify client error to a structured error without echoing anything secret."""
    status = getattr(exc, "status_code", None)
    kind = str(getattr(exc, "type", "") or "")
    if status in (401, 403):
        return ErrorInfo(code="unauthorized", message="Apify rejected APIFY_API_TOKEN, or it can't run this Actor.")
    if status == 429 or "rate-limit" in kind:
        return ErrorInfo(code="rate_limited", message="Apify is rate limiting requests. Try again shortly.", retryable=True)
    if status == 402 or re.search(r"usage|credit|limit-exceeded|not-enough", kind):
        return ErrorInfo(code="budget_exceeded", message="The Apify account has run out of credit or hit a usage limit.")
    if status == 404:
        return ErrorInfo(code="actor_not_found", message="The configured Apify Actor wasn't found.")
    return ErrorInfo(code="actor_failed", message=f"Apify request failed ({exc.__class__.__name__}).", retryable=True)


class Scraper:
    def __init__(
        self,
        client_factory: Callable[[str], Any] = default_client_factory,
        *,
        budget: RunBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
        sites: dict[str, ActorConfig] | None = None,
        token: str | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._budget = budget or RunBudget(clock=clock)
        self._clock = clock
        self._sites = sites or SITES
        self._token = token
        self._cache: dict[tuple, tuple[float, list[ProductCandidate], SearchMetadata, list[str]]] = {}
        self._runs = asyncio.Semaphore(MAX_CONCURRENT_RUNS)

    def token(self) -> str:
        return self._token if self._token is not None else os.getenv("APIFY_API_TOKEN", "").strip()

    def _response(self, request: ScrapeRequest, search: SearchMetadata, *, candidates=None,
                  warnings=None, error: ErrorInfo | None = None) -> ScrapeResponse:
        candidates = candidates or []
        status = "error" if error else "ok" if candidates else "empty"
        return ScrapeResponse(
            task_id=request.task_id,
            request_id=request.request_id,
            sent_at=now_iso(),
            status=status,
            candidates=candidates,
            search=search,
            warnings=warnings or [],
            error=error,
        )

    async def search(self, request: ScrapeRequest) -> ScrapeResponse:
        query = clean_text(request.search_query, 120)
        site = request.site.strip().lower().removeprefix("www.")
        config = self._sites.get(site)
        search = SearchMetadata(query=query, site=site, actor_id=config.actor_id if config else "")
        if config is None:
            return self._response(request, search, error=ErrorInfo(
                code="site_not_allowed", message=f"{site or 'That site'} isn't an approved shopping site."))
        if not query:
            return self._response(request, search, error=ErrorInfo(code="bad_request", message="The search was empty."))
        token = self.token()
        if not token:
            return self._response(request, search, error=ErrorInfo(
                code="integration_unavailable",
                message="Product search isn't set up: APIFY_API_TOKEN is missing in .env."))
        limit = max(1, min(request.max_items, MAX_CANDIDATES))
        filters = request.filters

        key = (site, query.lower(), filters.max_price, filters.min_price, filters.currency)
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < CACHE_SECONDS:
            _, items, meta, warnings = cached
            kept, dropped = apply_filters(items, filters)
            meta = meta.copy(update={"cached": True, "items_dropped": len(dropped)})
            return self._response(request, meta, candidates=kept[:limit], warnings=warnings)

        if not self._budget.take():
            return self._response(request, search, error=ErrorInfo(
                code="budget_exceeded",
                message=f"Hold My Hand has used its limit of {self._budget.limit} product searches this hour."))

        started = self._clock()
        async with self._runs:
            outcome = await self._run_actor(config, token, query, filters)
        if isinstance(outcome, ErrorInfo):
            search.duration_seconds = round(self._clock() - started, 1)
            return self._response(request, search, error=outcome)
        run, raw_items, warnings = outcome
        search.run_id = _attr(run, "id", "id")
        search.dataset_id = _attr(run, "default_dataset_id", "defaultDatasetId")
        search.items_seen = len(raw_items)
        search.duration_seconds = round(self._clock() - started, 1)

        collected_at = now_iso()
        normalized: list[ProductCandidate] = []
        malformed = 0
        for item in raw_items:
            candidate, _reason = config.normalize(item, config, collected_at)
            if candidate is None:
                malformed += 1
            else:
                normalized.append(candidate)
        if malformed:
            warnings.append(f"{malformed} listing(s) were missing a title or valid link and were skipped.")
        if raw_items and not normalized:
            search.items_dropped = len(raw_items)
            return self._response(request, search, warnings=warnings, error=ErrorInfo(
                code="malformed_output", message="The Actor returned listings, but none had a title and valid link.",
                retryable=True))
        self._cache[key] = (self._clock(), normalized, search.copy(), list(warnings))
        kept, dropped = apply_filters(normalized, filters)
        search.items_dropped = malformed + len(dropped)
        return self._response(request, search, candidates=kept[:limit], warnings=warnings)

    async def _run_actor(self, config: ActorConfig, token: str, query: str, filters: ScrapeFilters):
        client = self._client_factory(token)
        run_input = config.build_input(query, filters, FETCH_ITEMS, config)
        warnings: list[str] = []
        try:
            run = await asyncio.wait_for(
                client.actor(config.actor_id).call(
                    run_input=run_input,
                    max_items=FETCH_ITEMS,
                    max_total_charge_usd=MAX_CHARGE_USD,
                    memory_mbytes=config.memory_mbytes,
                    run_timeout=timedelta(seconds=RUN_TIMEOUT_SECONDS),
                    wait_duration=timedelta(seconds=WAIT_SECONDS),
                    logger=None,
                ),
                timeout=WAIT_SECONDS + 20,
            )
        except asyncio.TimeoutError:
            return ErrorInfo(code="timeout", message="The product search took too long.", retryable=True)
        except Exception as exc:
            if exc.__class__.__module__.startswith("apify_client"):
                return _api_error(exc)
            return ErrorInfo(code="actor_failed", message=f"The product search failed ({exc.__class__.__name__}).",
                             retryable=True)
        if run is None:
            return ErrorInfo(code="actor_failed", message="Apify didn't return the run.", retryable=True)
        status = _status(run)
        if status in {"RUNNING", "READY"}:
            run_id = _attr(run, "id", "id")
            try:
                await client.run(run_id).abort()
            except Exception:
                pass
            return ErrorInfo(code="timeout", message="The product search took too long and was stopped.", retryable=True)
        if status in {"TIMED-OUT", "TIMING-OUT"}:
            warnings.append("The Actor hit its time limit; these are the listings it found before stopping.")
        elif status in {"FAILED", "ABORTED", "ABORTING"}:
            return ErrorInfo(code="actor_failed", message=f"The Apify Actor run ended with status {status}.",
                             retryable=True)
        dataset_id = _attr(run, "default_dataset_id", "defaultDatasetId")
        try:
            page = await client.dataset(dataset_id).list_items(limit=FETCH_ITEMS * 2, clean=True)
        except Exception as exc:
            if exc.__class__.__module__.startswith("apify_client"):
                return _api_error(exc)
            return ErrorInfo(code="actor_failed", message="Couldn't read the Actor's results.", retryable=True)
        items = getattr(page, "items", None)
        if items is None and isinstance(page, dict):
            items = page.get("items")
        if not isinstance(items, list):
            return ErrorInfo(code="malformed_output", message="The Actor's results weren't a list.", retryable=True)
        if status in {"TIMED-OUT", "TIMING-OUT"} and not items:
            return ErrorInfo(code="timeout", message="The product search timed out before finding anything.",
                             retryable=True)
        return run, items[: FETCH_ITEMS * 2], warnings
