"""4. The technical helper enforces hard constraints, retries within its limit and says no_match honestly."""

from __future__ import annotations

from conftest import amazon_item
from scraper.actors import AMAZON_US, normalize_amazon
from shared import llm
from shared.models import (
    FAILED,
    NO_MATCH,
    SELECTED,
    ErrorInfo,
    HardConstraints,
    ProductSearchTask,
    RecommendationResponse,
    ScrapeRequest,
    ScrapeResponse,
    SearchMetadata,
    SoftPreferences,
    TaskAck,
    now_iso,
)
from technical_helper import flow
from technical_helper.evaluate import evaluate, good_enough
from technical_helper.flow import MAX_REFINEMENTS, ProductSearches, groq_explainer

ORCH, SCRAPER = "agent1qorchestrator", "agent1qscraper"


def candidate(n, title, price=59.99, **kwargs):
    return normalize_amazon(amazon_item(n, title, price, **kwargs), AMAZON_US, now_iso())[0]


def task(task_id="task-1", max_price=100.0, required=(), preferred=("lightweight", "easy_to_use"), category="vacuum"):
    return ProductSearchTask(
        task_id=task_id, sent_at=now_iso(), query="a vacuum under $100",
        hard_constraints=HardConstraints(category=category, max_price=max_price, required_features=list(required)),
        soft_preferences=SoftPreferences(features=list(preferred)), reply_to=ORCH,
    )


def listings(request: ScrapeRequest, candidates, *, error=None):
    return ScrapeResponse(
        task_id=request.task_id, request_id=request.request_id, sent_at=now_iso(),
        status="error" if error else "ok" if candidates else "empty", candidates=candidates,
        search=SearchMetadata(query=request.search_query, site=request.site), error=error,
    )


GOOD = candidate(1, "Lightweight Stick Vacuum, Easy to Use", 79.99)
OVER_BUDGET = candidate(2, "Lightweight Easy to Use Upright Vacuum", 149.0)
ACCESSORY = candidate(3, "Replacement Filters for Stick Vacuum, 4 Pack", 12.99)
WRONG_KIND = candidate(4, "Kitchen Blender with Glass Jar", 39.0, breadcrumbs="Home & Kitchen > Small Appliances")
NO_PRICE = candidate(5, "Lightweight Vacuum Cleaner", None)
OUT_OF_STOCK = candidate(6, "Lightweight Vacuum", 49.0, in_stock=False)


def helper():
    return ProductSearches(scraper=SCRAPER, orchestrator=ORCH, explain=None)


def test_hard_constraints():
    hard = HardConstraints(category="vacuum", max_price=100.0)
    soft = SoftPreferences(features=["lightweight"])
    assert evaluate(GOOD, hard, soft).passes
    assert "over your $100 budget" in evaluate(OVER_BUDGET, hard, soft).failures[0]
    assert any("accessory" in f for f in evaluate(ACCESSORY, hard, soft).failures)
    assert evaluate(WRONG_KIND, hard, soft).failures == ["isn't a vacuum"]
    assert evaluate(NO_PRICE, hard, soft).failures == ["price isn't shown"]
    assert evaluate(OUT_OF_STOCK, hard, soft).failures == ["is out of stock"]
    other_currency = GOOD.copy(update={"currency": "GBP"})
    assert "not USD" in evaluate(other_currency, hard, soft).failures[0]
    required = HardConstraints(category="vacuum", max_price=100.0, required_features=["cordless"])
    assert "doesn't say it's cordless" in evaluate(GOOD, required, soft).failures[0]


def test_soft_score_uses_only_listing_evidence():
    soft = SoftPreferences(features=["lightweight", "quiet"])
    result = evaluate(GOOD, HardConstraints(category="vacuum", max_price=100.0), soft)
    assert result.evidence == {"lightweight": "Lightweight"}
    assert result.unverified == ["quiet"]
    assert good_enough(result, soft)  # one of two features: half is enough
    assert "+3 lightweight" in result.breakdown


async def test_rejects_tasks_not_from_the_orchestrator():
    sends = await helper().on_task("agent1qstranger", task())
    assert len(sends) == 1 and isinstance(sends[0].message, TaskAck)
    assert sends[0].message.status == "rejected" and sends[0].message.error.code == "unauthorized_sender"
    bad_reply_to = task().copy(update={"reply_to": "agent1qelsewhere"})
    sends = await helper().on_task(ORCH, bad_reply_to)
    assert sends[0].message.status == "rejected"


async def test_selects_one_real_candidate():
    searches = helper()
    ack, scrape = await searches.on_task(ORCH, task())
    assert ack.message.status == "accepted" and ack.to == ORCH
    assert scrape.to == SCRAPER and scrape.message.task_id == "task-1" and scrape.message.request_id == "task-1:0"
    assert scrape.message.filters.max_price == 100.0 and scrape.message.filters.must_include_any == ["vacuum"]
    [result] = await searches.on_scrape_response(SCRAPER, listings(scrape.message, [OVER_BUDGET, GOOD, ACCESSORY]))
    rec = result.message
    assert result.to == ORCH and isinstance(rec, RecommendationResponse)
    assert rec.task_id == "task-1" and rec.status == SELECTED
    assert rec.selected_candidate == GOOD  # exactly the scraped listing, nothing invented
    assert "$79.99" in rec.rationale and "$100 budget" in rec.rationale
    assert rec.alternatives_considered_count == 3 and rec.attempts == 1
    assert any("change" in caveat for caveat in rec.caveats)


async def test_retries_within_limit_then_no_match():
    searches = helper()
    _, scrape = await searches.on_task(ORCH, task())
    requests = [scrape.message]
    for _ in range(MAX_REFINEMENTS):
        [send] = await searches.on_scrape_response(SCRAPER, listings(requests[-1], [OVER_BUDGET, ACCESSORY]))
        assert isinstance(send.message, ScrapeRequest)
        requests.append(send.message)
    [final] = await searches.on_scrape_response(SCRAPER, listings(requests[-1], [WRONG_KIND]))
    assert isinstance(final.message, RecommendationResponse) and final.message.status == NO_MATCH
    assert final.message.selected_candidate is None and final.message.attempts == MAX_REFINEMENTS + 1
    assert "cost more than your budget" in final.message.rationale
    assert [r.request_id for r in requests] == ["task-1:0", "task-1:1", "task-1:2"]
    assert len({r.search_query for r in requests}) == 3  # each retry is a different, tighter search
    assert requests[0].search_query == "lightweight easy to use vacuum"
    assert requests[-1].search_query.endswith(("for seniors", "for elderly"))
    # Nothing more happens for this task.
    assert await searches.on_scrape_response(SCRAPER, listings(requests[-1], [GOOD])) == []


async def test_stale_and_foreign_responses_are_ignored():
    searches = helper()
    _, scrape = await searches.on_task(ORCH, task())
    stale = scrape.message.copy(update={"request_id": "task-1:9"})
    assert await searches.on_scrape_response(SCRAPER, listings(stale, [GOOD])) == []
    assert await searches.on_scrape_response("agent1qstranger", listings(scrape.message, [GOOD])) == []
    [result] = await searches.on_scrape_response(SCRAPER, listings(scrape.message, [GOOD]))
    assert result.message.status == SELECTED


async def test_duplicate_task_resends_the_same_result():
    searches = helper()
    _, scrape = await searches.on_task(ORCH, task())
    [first] = await searches.on_scrape_response(SCRAPER, listings(scrape.message, [GOOD]))
    ack, again = await searches.on_task(ORCH, task())
    assert ack.message.status == "duplicate" and again.message == first.message


async def test_configuration_errors_end_the_search_without_retrying():
    searches = helper()
    _, scrape = await searches.on_task(ORCH, task())
    error = ErrorInfo(code="integration_unavailable", message="APIFY_API_TOKEN is missing")
    [final] = await searches.on_scrape_response(SCRAPER, listings(scrape.message, [], error=error))
    assert final.message.status == FAILED and final.message.error.code == "integration_unavailable"
    assert final.message.attempts == 1


async def test_transient_errors_retry_then_fail():
    searches = helper()
    _, send = await searches.on_task(ORCH, task())
    error = ErrorInfo(code="timeout", message="slow", retryable=True)
    for _ in range(MAX_REFINEMENTS):
        [send] = await searches.on_scrape_response(SCRAPER, listings(send.message, [], error=error))
        assert isinstance(send.message, ScrapeRequest)
    [final] = await searches.on_scrape_response(SCRAPER, listings(send.message, [], error=error))
    assert final.message.status == FAILED and final.message.attempts == MAX_REFINEMENTS + 1


async def test_scraper_silence_times_out():
    now = [0.0]
    searches = ProductSearches(scraper=SCRAPER, orchestrator=ORCH, explain=None, clock=lambda: now[0])
    await searches.on_task(ORCH, task())
    assert await searches.sweep() == []
    now[0] = flow.SCRAPE_TIMEOUT_SECONDS + 1
    [retry] = await searches.sweep()
    assert isinstance(retry.message, ScrapeRequest) and retry.message.retry_number == 1


async def test_model_wording_with_invented_facts_is_discarded(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    chosen = evaluate(GOOD, HardConstraints(category="vacuum", max_price=100.0), SoftPreferences(features=["lightweight"]))

    async def invented(*args, **kwargs):
        return "A great deal at just $49 with 5,000 five-star reviews!"

    monkeypatch.setattr(llm, "complete", invented)
    assert await groq_explainer(task(), chosen, "fallback text") == "fallback text"

    async def faithful(*args, **kwargs):
        return "It's a lightweight vacuum at $79.99, under your $100 budget."

    monkeypatch.setattr(llm, "complete", faithful)
    assert await groq_explainer(task(), chosen, "fallback text") == "It's a lightweight vacuum at $79.99, under your $100 budget."
