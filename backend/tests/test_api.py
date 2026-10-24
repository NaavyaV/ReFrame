import os
import uuid

import httpx
import pytest

BASE = os.getenv("REFRAME_API", "http://localhost:8000")

SNAPSHOT = {
    "url": "https://power.example.com/account",
    "title": "My Account - Example Power",
    "headings": ["Welcome back", "Latest news"],
    "elements": [
        {"ref": "r1", "kind": "control", "tag": "a", "role": "link", "text": "Billing & Payments", "href": "/billing", "region": "nav"},
        {"ref": "r2", "kind": "control", "tag": "a", "role": "link", "text": "Outage map", "href": "/outages", "region": "nav"},
        {"ref": "r3", "kind": "control", "tag": "button", "role": "button", "text": "Pay now", "region": "main"},
        {"ref": "r4", "kind": "control", "tag": "input", "input_type": "password", "label": "Password", "sensitive": True},
        {"ref": "r5", "kind": "block", "tag": "aside", "text": "Save 20% on smart thermostats!", "region": "aside"},
        {"ref": "r6", "kind": "block", "tag": "section", "text": "Amount due: $84.12 by Nov 3", "region": "main"},
    ],
}


@pytest.fixture(scope="module")
def token() -> str:
    email = f"test-{uuid.uuid4().hex[:8]}@example.com"
    r = httpx.post(f"{BASE}/auth/register", json={"email": email, "password": "correct horse", "display_name": "Test"})
    assert r.status_code == 200, r.text
    r = httpx.post(f"{BASE}/auth/login", json={"email": email, "password": "wrong password"})
    assert r.status_code == 401
    r = httpx.post(f"{BASE}/auth/login", json={"email": email, "password": "correct horse"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_reframe_requires_auth():
    r = httpx.post(f"{BASE}/reframe", json={"message": "hi", "snapshot": SNAPSHOT})
    assert r.status_code == 401


def test_reframe_pay_bill(token):
    r = httpx.post(
        f"{BASE}/reframe",
        json={"message": "I want to pay my electricity bill", "snapshot": SNAPSHOT},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    print(body)
    refs = {"r1", "r2", "r3", "r4", "r5", "r6"}
    for op in body["operations"]:
        for item in op.get("items", []):
            assert item.get("ref") in refs | {None}
            if item.get("ref") == "r3":
                assert item["confirm"] is True


def test_reframe_bigger_text(token):
    r = httpx.post(
        f"{BASE}/reframe",
        json={"message": "make the text much bigger and hide the ad", "snapshot": SNAPSHOT},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    print(body)
    assert any(op["op"] in ("page_style", "style") for op in body["operations"])
