"""Checking real listings against what the person asked for.

Hard constraints first. A listing that fails any of them can't be chosen:
  - it is the right kind of product (the category's main word is in the title or
    category path, and it isn't an accessory or replacement part)
  - its price is shown, in the right currency, and within the budget (when there is one)
  - it isn't marked out of stock, and wasn't suggested already
  - every feature the person said it must have appears in the listing's own words

Then a transparent score ranks the listings that pass:
  +3     for each preferred feature the listing mentions
  +2x    (rating - 3.5), when a star rating is shown        (so 4.5 stars = +2, 3 stars = -1)
  +0.5x  log10(reviews + 1), at most +2
  +0.5   if it says it's in stock
  +0.5x  the share of the budget left unspent, at most +0.5
  -0.5   if the listing was sponsored
A listing is "good enough" when it passes, mentions at least half the preferred
features, and isn't rated below 3.5 stars. Nothing here is guessed: a feature only
counts when the listing says it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from shared.features import evidence_for, label, stem, words
from shared.format import money
from shared.models import HardConstraints, ProductCandidate, SoftPreferences


ACCESSORY = re.compile(
    r"\b(?:replacement|compatible with|accessor(?:y|ies)|spare parts?|refills?|repair kit)\b|"
    r"\b(?:filters?|bags?|parts?|attachments?|brush(?:es)? rolls?|batter(?:y|ies)|chargers?|covers?|cases?|"
    r"hoses?|belts?|pads?|heads?|cuffs?|bands?|straps?|cables?|cords?|mounts?|stands?)\s+(?:for|compatible|fits?)\b",
    re.I,
)


@dataclass
class Evaluation:
    candidate: ProductCandidate
    failures: list[str] = field(default_factory=list)
    met: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    score: float = 0.0
    breakdown: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return not self.failures


def listing_text(candidate: ProductCandidate) -> str:
    parts = [candidate.title, *candidate.features, *(f"{key}: {value}" for key, value in candidate.attributes.items())]
    return " \n".join(parts)


def category_matches(category: str, candidate: ProductCandidate) -> bool:
    wanted = [word for word in re.findall(r"[a-z0-9]+", category.lower()) if word not in {"a", "an", "the", "for"}]
    if not wanted:
        return False
    stems = [stem(word) for word in wanted]
    head = stems[-1]
    have = words(candidate.title + " " + candidate.attributes.get("Category", ""))
    if head not in have:
        return False
    return sum(word in have for word in stems) / len(stems) >= 0.5


def evaluate(candidate: ProductCandidate, hard: HardConstraints, soft: SoftPreferences) -> Evaluation:
    result = Evaluation(candidate)
    text = listing_text(candidate)

    if category_matches(hard.category, candidate):
        result.met.append(f"is a {hard.category}")
    else:
        result.failures.append(f"isn't a {hard.category}")
    if ACCESSORY.search(candidate.title) and not ACCESSORY.search(hard.category):
        result.failures.append(f"looks like an accessory or part, not a {hard.category}")

    if hard.max_price is not None or hard.min_price is not None:
        if candidate.price is None:
            result.failures.append("price isn't shown")
        elif candidate.currency != hard.currency:
            result.failures.append(f"price is in {candidate.currency or 'an unknown currency'}, not {hard.currency}")
        else:
            if hard.max_price is not None and candidate.price > hard.max_price:
                result.failures.append(f"costs {money(candidate.price, candidate.currency)}, over your "
                                       f"{money(hard.max_price, hard.currency)} budget")
            elif hard.max_price is not None:
                result.met.append(f"is under your {money(hard.max_price, hard.currency)} budget")
            if hard.min_price is not None and candidate.price < hard.min_price:
                result.failures.append(f"costs less than {money(hard.min_price, hard.currency)}")

    if candidate.availability == "out_of_stock":
        result.failures.append("is out of stock")
    if candidate.url in hard.exclude_urls:
        result.failures.append("was already suggested")

    for key in hard.required_features:
        found = evidence_for(key, text)
        if found:
            result.met.append(label(key))
            result.evidence[key] = found
        else:
            result.failures.append(f"the listing doesn't say it's {label(key)}")

    if not result.passes:
        return result

    for key in soft.features:
        found = evidence_for(key, text)
        if found:
            result.met.append(label(key))
            result.evidence[key] = found
            result.score += 3
            result.breakdown.append(f"+3 {key}")
        else:
            result.unverified.append(label(key))
    if candidate.rating is not None and soft.prefer_well_reviewed:
        points = 2 * (candidate.rating - 3.5)
        result.score += points
        result.breakdown.append(f"{points:+.1f} rating {candidate.rating}")
    if candidate.review_count and soft.prefer_well_reviewed:
        points = min(0.5 * math.log10(candidate.review_count + 1), 2)
        result.score += points
        result.breakdown.append(f"+{points:.1f} {candidate.review_count} reviews")
    if candidate.availability == "in_stock" and soft.prefer_in_stock:
        result.score += 0.5
        result.breakdown.append("+0.5 in stock")
    if hard.max_price and candidate.price is not None and candidate.currency == hard.currency:
        points = min(0.5 * (1 - candidate.price / hard.max_price), 0.5)
        result.score += points
        result.breakdown.append(f"+{points:.2f} price headroom")
    if candidate.sponsored:
        result.score -= 0.5
        result.breakdown.append("-0.5 sponsored")
    result.score = round(result.score, 2)
    return result


def good_enough(result: Evaluation, soft: SoftPreferences) -> bool:
    if not result.passes:
        return False
    found = sum(key in result.evidence for key in soft.features)
    if soft.features and found < math.ceil(len(soft.features) / 2):
        return False
    rating = result.candidate.rating
    return rating is None or rating >= 3.5


def best(results: list[Evaluation]) -> Evaluation | None:
    passing = [result for result in results if result.passes]
    if not passing:
        return None
    return max(passing, key=lambda r: (r.score, -(r.candidate.price or math.inf)))
