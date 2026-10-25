"""Optional checks against the real services named in .env. Skipped unless HMH_LIVE_TESTS=1.

    $env:HMH_LIVE_TESTS = "1"; .\\.venv\\Scripts\\python.exe -m pytest tests/test_live.py -s

The Apify test runs ONE real, paid Actor run with the scraper's normal limits
(6 items, a $0.05 charge cap, 75 s). The SpacetimeDB test only reads (SELECT COUNT).
Nothing here prints a key or token.
"""

from __future__ import annotations

import os

import pytest
from dotenv import dotenv_values

from conftest import AGENTS_DIR

LIVE = os.getenv("HMH_LIVE_TESTS") == "1"
ENV = {key: (value or "").strip() for key, value in dotenv_values(AGENTS_DIR.parent / ".env").items()} if LIVE else {}
pytestmark = [pytest.mark.live, pytest.mark.skipif(not LIVE, reason="set HMH_LIVE_TESTS=1 to call real services")]


async def test_live_apify_search_returns_real_bounded_listings():
    if not ENV.get("APIFY_API_TOKEN"):
        pytest.skip("APIFY_API_TOKEN isn't set")
    from scraper.flow import Scraper
    from shared.models import ScrapeFilters, ScrapeRequest, now_iso

    response = await Scraper(token=ENV["APIFY_API_TOKEN"]).search(ScrapeRequest(
        task_id="live-1", request_id="live-1:0", sent_at=now_iso(), search_query="lightweight vacuum easy to use",
        site="amazon.com", filters=ScrapeFilters(max_price=100.0, must_include_any=["vacuum"]),
    ))
    print(f"\nApify: status={response.status} seen={response.search.items_seen} dropped={response.search.items_dropped} "
          f"run={response.search.run_id} took={response.search.duration_seconds}s warnings={response.warnings}")
    for candidate in response.candidates:
        print(f"  - {candidate.title[:70]} | {candidate.supplier} | {candidate.price} {candidate.currency} | "
              f"{candidate.availability} | {candidate.rating} ({candidate.review_count}) | {candidate.url}")
    assert response.error is None, response.error
    assert len(response.candidates) <= 3
    for candidate in response.candidates:
        assert candidate.url.startswith("https://www.amazon.com/dp/")
        assert candidate.price is None or candidate.price <= 100.0
        assert candidate.source_actor == "junglee/amazon-crawler"


async def test_live_groq_answers(monkeypatch):
    if not ENV.get("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY isn't set")
    from shared import llm

    monkeypatch.setenv("GROQ_API_KEY", ENV["GROQ_API_KEY"])
    monkeypatch.setattr(llm, "_client", None)
    text = await llm.complete([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=50)
    assert "ready" in text.lower()


async def test_live_spacetime_tables_are_reported():
    if not ENV.get("SPACETIME_TOKEN") or not ENV.get("SPACETIME_DB_NAME"):
        pytest.skip("SPACETIME_* isn't set")
    from caretaker.store import TABLES
    from shared.spacetime import Spacetime

    db = Spacetime(ENV.get("SPACETIME_HOST") or "https://maincloud.spacetimedb.com", ENV["SPACETIME_DB_NAME"],
                   ENV["SPACETIME_TOKEN"])
    status = await db.preflight(["app_user", "task_request", *TABLES])
    print(f"\nSpacetimeDB tables: {status}")
    assert status["app_user"] == "ok"
    assert all(value in {"ok", "missing"} for value in status.values())
