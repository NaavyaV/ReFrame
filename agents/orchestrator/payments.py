"""Stripe payment links for an amount due, in test mode only.

A live key (sk_live_ / rk_live_) is refused, so no real card can be charged.
"""

from __future__ import annotations

import os

import httpx

STRIPE_API = "https://api.stripe.com/v1"
TEST_PREFIXES = ("sk_test_", "rk_test_")
MIN_AMOUNT = 0.50


class PaymentError(Exception):
    pass


def key() -> str:
    return os.getenv("STRIPE_SECRET_KEY", "").strip()


def status() -> str:
    value = key()
    if not value:
        return "missing"
    return "test" if value.startswith(TEST_PREFIXES) else "not a test key"


async def payment_link(amount: float, currency: str, name: str) -> str:
    """A Stripe Payment Link charging exactly this amount."""
    if status() != "test":
        raise PaymentError("No Stripe test key." if not key() else "STRIPE_SECRET_KEY isn't a test key.")
    if amount < MIN_AMOUNT:
        raise PaymentError(f"Stripe can't charge less than {MIN_AMOUNT:.2f}.")
    try:
        async with httpx.AsyncClient(base_url=STRIPE_API, auth=(key(), ""), timeout=20.0) as client:
            price = await client.post("/prices", data={
                "unit_amount": round(amount * 100),
                "currency": currency.lower(),
                "product_data[name]": name[:250],
            })
            price.raise_for_status()
            link = await client.post("/payment_links", data={
                "line_items[0][price]": price.json()["id"],
                "line_items[0][quantity]": 1,
            })
            link.raise_for_status()
            return link.json()["url"]
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()["error"]["message"]
        except Exception:
            detail = f"HTTP {exc.response.status_code}"
        raise PaymentError(f"Stripe refused: {detail}") from exc
    except httpx.HTTPError as exc:
        raise PaymentError(f"Couldn't reach Stripe ({exc.__class__.__name__}).") from exc
