"""Test setup: throwaway seeds and no external services.

Every external key is blanked before any agent code loads .env, so no test can
call Groq, Apify, Agentverse or SpacetimeDB with real credentials. The product
listings below are made-up fixtures for tests only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

TEST_ENV = {
    "ORCHESTRATOR_SEED": "hmh-test-orchestrator-seed-0001",
    "TECHNICAL_HELPER_SEED": "hmh-test-technical-helper-seed-0001",
    "SCRAPER_SEED": "hmh-test-scraper-seed-0001",
    "CAREGIVER_SEED": "hmh-test-caregiver-seed-0001",
    "GROQ_API_KEY": "",
    "AGENTVERSE_API_KEY": "",
    "APIFY_API_TOKEN": "",
    "STRIPE_SECRET_KEY": "",
    "SPACETIME_DB_NAME": "",
    "SPACETIME_TOKEN": "",
    "BRIDGE_SHARED_SECRET": "hmh-test-bridge-secret-0001",
    "ORCHESTRATOR_ADDRESS": "",
    "TECHNICAL_HELPER_ADDRESS": "",
    "SCRAPER_ADDRESS": "",
    "CAREGIVER_ADDRESS": "",
}
os.environ.update(TEST_ENV)
AGENTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENTS_DIR))

import pytest  # noqa: E402


def amazon_item(n: int, title: str, price=49.99, *, brand="ExampleBrand", features=None, in_stock=True,
                stars=4.5, reviews=1200, sponsored=False, asin=None,
                breadcrumbs="Home & Kitchen > Vacuums & Floor Care > Vacuums", **extra) -> dict:
    """A dataset item shaped like junglee/amazon-crawler output. Test fixture only."""
    item = {
        "title": title,
        "asin": asin or f"B0TEST{n:04d}",
        "url": f"https://www.amazon.com/Example-Product/dp/{asin or f'B0TEST{n:04d}'}?ref=sr_1_{n}",
        "brand": brand,
        "price": {"value": price, "currency": "$"} if price is not None else None,
        "inStock": in_stock,
        "inStockText": "In Stock" if in_stock else "Currently unavailable",
        "stars": stars,
        "reviewsCount": reviews,
        "breadCrumbs": breadcrumbs,
        "features": features or [],
        "attributesMapped": {"Item Weight": "4.5 Pounds", "Power Source": "Corded Electric"},
        "thumbnailImage": "https://m.media-amazon.com/images/I/example.jpg",
        "categoryPageData": {"isSponsored": sponsored},
    }
    item.update(extra)
    return item


class FakeApify:
    """Stands in for ApifyClientAsync. Records every call; never touches the network."""

    def __init__(self, items=None, *, status="SUCCEEDED", error: Exception | None = None, delay: float = 0.0):
        self.items = items if items is not None else []
        self.status = status
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []
        self.actor_ids: list[str] = []
        self.aborted = False
        self.tokens: list[str] = []

    def factory(self, token: str):
        self.tokens.append(token)
        return self

    def actor(self, actor_id: str):
        self.actor_ids.append(actor_id)
        return self

    async def call(self, **kwargs):
        import asyncio

        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id="run-test-1", status=self.status, default_dataset_id="dataset-test-1")

    def dataset(self, dataset_id: str):
        return self

    async def list_items(self, **kwargs):
        return SimpleNamespace(items=list(self.items))

    def run(self, run_id: str):
        return self

    async def abort(self):
        self.aborted = True


def apify_error(status: int, kind: str = ""):
    """An exception that looks like apify_client's ApifyApiError to the scraper."""
    cls = type("ApifyApiError", (Exception,), {"__module__": "apify_client.errors"})
    error = cls(f"status {status}")
    error.status_code = status
    error.type = kind
    return error


@pytest.fixture
def fake_apify():
    return FakeApify


@pytest.fixture
def item():
    return amazon_item


@pytest.fixture
def addresses():
    from shared.agents import address

    return {key: address(key) for key in ("orchestrator", "technical_helper", "scraper", "caregiver")}


@pytest.fixture
def unregister():
    """Remove agents built by a test from uAgents' process-wide dispatcher afterwards."""
    from uagents.dispatch import dispatcher

    built = []
    yield built
    for agent in built:
        dispatcher.unregister(agent.address, agent)
