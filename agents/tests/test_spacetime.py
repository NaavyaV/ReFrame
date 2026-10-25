"""7. A missing SpacetimeDB table gives an honest, actionable message and nothing crashes."""

from __future__ import annotations

import httpx

from orchestrator.flow import Orchestrator
from orchestrator.tasklog import TaskLog
from shared.models import ProductSearchTask
from shared.spacetime import MissingTable, Spacetime, SpacetimeError, check_tables

TOKEN = "stdb-test-token-should-never-appear"


def database(handler) -> Spacetime:
    return Spacetime("https://stdb.test", "reframe-test", TOKEN, transport=httpx.MockTransport(handler))


def missing(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/sql"):
        table = request.content.decode().split("FROM ")[-1]
        return httpx.Response(400, text=f"no such table: `{table}`. If the table exists, it may be marked private.")
    return httpx.Response(404, text="no such reducer")


async def test_missing_table_is_reported_by_name():
    db = database(missing)
    try:
        await db.sql("SELECT * FROM task_request")
    except MissingTable as exc:
        assert exc.table == "task_request"
        assert "reframe-test" in str(exc) and "Publish the updated module" in str(exc)
        assert TOKEN not in str(exc)
    else:
        raise AssertionError("expected MissingTable")
    assert await db.preflight(["task_request", "caregiver_grant"]) == {
        "task_request": "missing", "caregiver_grant": "missing"}


async def test_task_history_switches_off_and_shopping_still_works():
    log = TaskLog(database(missing))
    line = await log.check()
    assert "Task history is OFF" in line and "task_request" in line and not log.enabled
    await log.record("task-1", "hmh-u-x", "product", "delegated", "a vacuum")  # no error, no call
    orchestrator = Orchestrator(own_address="me", technical_helper="helper", caregiver="care", task_log=log)
    actions = await orchestrator.on_text("agent1qperson", "Find a vacuum under $100")
    assert any(isinstance(getattr(a, "message", None), ProductSearchTask) for a in actions)


async def test_reducer_failure_turns_history_off_without_raising():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/sql"):
            return httpx.Response(200, json=[{"schema": {"elements": [{"name": {"some": "n"}}]}, "rows": [[0]]}])
        return httpx.Response(530, text="maincloud unavailable")

    log = TaskLog(database(handler))
    assert "saving" in await log.check() and log.enabled
    await log.record("task-1", "hmh-u-x", "product", "delegated", "a vacuum")
    assert not log.enabled and "530" in log.reason
    await log.record("task-1", "hmh-u-x", "product", "selected", "a vacuum")
    assert sum(path.endswith("/call/upsert_task_request") for path in calls) == 1


async def test_bad_token_and_unreachable_server_are_explained():
    denied = database(lambda request: httpx.Response(401, text=f"bad token {TOKEN}"))
    status = await denied.preflight(["task_request"])
    assert "refused SPACETIME_TOKEN" in status["task_request"] and TOKEN not in status["task_request"]

    def offline(request):
        raise httpx.ConnectError("connection refused")

    try:
        await database(offline).sql("SELECT 1")
    except SpacetimeError as exc:
        assert "Couldn't reach SpacetimeDB" in str(exc)


async def test_not_configured_is_reported_without_network(monkeypatch):
    monkeypatch.setenv("SPACETIME_TOKEN", "")
    db, status = await check_tables(["task_request"])
    assert db is None and "SPACETIME_TOKEN" in status["task_request"]
