"""Apify Actors the scraper may run, and how to talk to each one.

Actors don't share an input or output schema, so each site maps to an ActorConfig
with its own input builder and item normaliser. To support another approved site,
add an entry to SITES with an Actor whose schema you have checked.

amazon.com -> junglee/amazon-crawler ("Amazon Product Scraper", pay per result).
  Input used: categoryOrProductUrls [{url}], maxItemsPerStartUrl, maxSearchPagesPerStartUrl,
  scrapeProductDetails, useCaptchaSolver (always false), scrapeSellers, maxOffers,
  maxProductVariantsAsSeparateResults, scrapeProductVariantPrices.
  Output fields read: title, url, asin, brand, price {value, currency}, inStock, inStockText,
  stars, reviewsCount, breadCrumbs, features[], attributes[] / attributesMapped {},
  productOverview[], thumbnailImage, seller {name}, categoryPageData {isSponsored}.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from shared.models import APPROVED_SITES, ProductCandidate, ScrapeFilters
from shared.security import clean_text, host_matches


@dataclass(frozen=True)
class ActorConfig:
    actor_id: str
    site: str
    currency: str
    build_input: Callable[[str, ScrapeFilters, int, "ActorConfig"], dict[str, Any]]
    normalize: Callable[[Any, "ActorConfig", str], tuple[ProductCandidate | None, str]]
    memory_mbytes: int = 2048


CURRENCY_SYMBOLS = {"$": "USD", "US$": "USD", "USD": "USD", "£": "GBP", "GBP": "GBP", "€": "EUR", "EUR": "EUR",
                    "C$": "CAD", "CAD": "CAD", "A$": "AUD", "AUD": "AUD"}
ASIN = re.compile(r"^[A-Z0-9]{10}$")
ATTRIBUTE_KEYS = re.compile(
    r"weight|dimension|size|power|battery|cord|special feature|feature|display|screen|noise|material|"
    r"capacity|recommended|surface|controls?|voltage|wattage|color|colour|included",
    re.I,
)


def amazon_search_url(query: str, filters: ScrapeFilters, site: str) -> str:
    params: dict[str, str] = {"k": query}
    if filters.max_price or filters.min_price:
        low = int(filters.min_price * 100) if filters.min_price else ""
        high = int(math.ceil(filters.max_price * 100)) if filters.max_price else ""
        params["rh"] = f"p_36:{low}-{high}"
    return f"https://www.{site}/s?{urlencode(params)}"


def amazon_input(query: str, filters: ScrapeFilters, fetch_items: int, config: ActorConfig) -> dict[str, Any]:
    return {
        "categoryOrProductUrls": [{"url": amazon_search_url(query, filters, config.site)}],
        "maxItemsPerStartUrl": fetch_items,
        "maxSearchPagesPerStartUrl": 1,
        "scrapeProductDetails": True,
        "useCaptchaSolver": False,
        "scrapeSellers": False,
        "maxOffers": 0,
        "maxProductVariantsAsSeparateResults": 0,
        "scrapeProductVariantPrices": False,
    }


def parse_price(value: Any, default_currency: str) -> tuple[float | None, str]:
    currency = ""
    amount: Any = value
    if isinstance(value, dict):
        amount = value.get("value")
        currency = str(value.get("currency") or "").strip()
    if isinstance(amount, str):
        symbol = re.match(r"^\s*([A-Z]{3}|[A-Z]?\$|£|€)", amount)
        if symbol and not currency:
            currency = symbol.group(1)
        digits = re.search(r"\d[\d,]*(?:\.\d+)?", amount)
        amount = float(digits.group(0).replace(",", "")) if digits else None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None, ""
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0 or amount > 100_000:
        return None, ""
    code = CURRENCY_SYMBOLS.get(currency, currency if re.fullmatch(r"[A-Z]{3}", currency) else "")
    if currency in {"$", ""}:
        code = default_currency
    return round(amount, 2), code


def _attributes(item: dict[str, Any]) -> dict[str, str]:
    pairs: list[tuple[Any, Any]] = []
    mapped = item.get("attributesMapped")
    if isinstance(mapped, dict):
        pairs.extend(mapped.items())
    for key in ("productOverview", "attributes", "manufacturerAttributes"):
        rows = item.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    pairs.append((row.get("key") or row.get("name"), row.get("value")))
    out: dict[str, str] = {}
    for raw_key, raw_value in pairs:
        key = clean_text(raw_key, 40)
        value = clean_text(raw_value, 120)
        if not key or not value or key in out or not ATTRIBUTE_KEYS.search(key):
            continue
        out[key] = value
        if len(out) >= 8:
            break
    return out


def _brand(raw: Any) -> str:
    brand = clean_text(raw, 80)
    brand = re.sub(r"^(?:visit the|brand:)\s*", "", brand, flags=re.I)
    return re.sub(r"\s+store$", "", brand, flags=re.I).strip()


def normalize_amazon(item: Any, config: ActorConfig, collected_at: str) -> tuple[ProductCandidate | None, str]:
    """One dataset item as a ProductCandidate, or (None, why it was dropped)."""
    if not isinstance(item, dict):
        return None, "not an object"
    title = clean_text(item.get("title"), 200)
    if not title:
        return None, "no title"
    asin = str(item.get("asin") or "").strip().upper()
    raw_url = str(item.get("url") or "").strip()
    if ASIN.match(asin):
        url = f"https://www.{config.site}/dp/{asin}"
    elif raw_url.startswith("https://") and host_matches(raw_url, {config.site}):
        parts = urlsplit(raw_url)
        url = f"https://{parts.hostname}{parts.path}"
    else:
        return None, "no valid product link"
    price, currency = parse_price(item.get("price"), config.currency)
    seller = item.get("seller")
    image = str(item.get("thumbnailImage") or "")
    category_data = item.get("categoryPageData") if isinstance(item.get("categoryPageData"), dict) else {}
    in_stock = item.get("inStock")
    rating = item.get("stars")
    reviews = item.get("reviewsCount")
    attributes = _attributes(item)
    breadcrumbs = clean_text(item.get("breadCrumbs"), 150)
    if breadcrumbs:
        attributes["Category"] = breadcrumbs
    features = [clean_text(text, 160) for text in (item.get("features") or []) if isinstance(text, str)]
    candidate = ProductCandidate(
        id=f"{config.site}:{asin or hashlib.sha1(url.encode()).hexdigest()[:10]}",
        title=title,
        supplier=_brand(item.get("brand")),
        seller=clean_text(seller.get("name"), 80) if isinstance(seller, dict) else "",
        price=price,
        currency=currency if price is not None else "",
        url=url,
        availability="in_stock" if in_stock is True else "out_of_stock" if in_stock is False else "",
        availability_text=clean_text(item.get("inStockText"), 80),
        rating=float(rating) if isinstance(rating, (int, float)) and not isinstance(rating, bool) and 0 < rating <= 5 else None,
        review_count=int(reviews) if isinstance(reviews, (int, float)) and not isinstance(reviews, bool) and reviews >= 0 else None,
        features=[text for text in features if text][:5],
        attributes=attributes,
        image_url=image if image.startswith("https://") else "",
        sponsored=bool(category_data.get("isSponsored")),
        source=config.site,
        source_actor=config.actor_id,
        collected_at=collected_at,
    )
    candidate.missing_data = [
        name
        for name, missing in (
            ("price", candidate.price is None),
            ("brand", not candidate.supplier),
            ("availability", not candidate.availability),
            ("rating", candidate.rating is None),
        )
        if missing
    ]
    return candidate, ""


AMAZON_US = ActorConfig(
    actor_id="junglee/amazon-crawler",
    site="amazon.com",
    currency="USD",
    build_input=amazon_input,
    normalize=normalize_amazon,
)

# Approved sites. Anything else is refused before an Actor runs.
SITES: dict[str, ActorConfig] = {config.site: config for config in (AMAZON_US,) if config.site in APPROVED_SITES}
