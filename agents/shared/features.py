"""Product features people ask for, and how to recognise them.

The orchestrator uses `asked` patterns to read a request; the technical helper uses
`evidence` patterns to check a real listing. A feature only counts as met when the
listing's own title, bullet points or attributes say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    search_terms: str
    asked: tuple[str, ...]
    evidence: tuple[str, ...]


FEATURES: dict[str, Feature] = {
    feature.key: feature
    for feature in (
        Feature(
            "lightweight",
            "lightweight",
            "lightweight",
            (r"light ?weight", r"\blight\b", r"easy to (?:lift|carry)", r"not (?:too )?heavy"),
            (r"light ?weight", r"\blightest\b", r"\bultra ?light", r"\bfeather ?light"),
        ),
        Feature(
            "easy_to_use",
            "easy to use",
            "easy to use",
            (r"easy[- ]to[- ]use", r"simple to use", r"easy to operate", r"user[- ]friendly", r"\bsimple\b", r"\beasy\b"),
            (r"easy[- ]to[- ](?:use|operate|clean|empty|read|set ?up)", r"user[- ]friendly", r"one[- ](?:touch|button|click)",
             r"\bsimple\b", r"\bintuitive\b", r"\bhassle[- ]free\b"),
        ),
        Feature(
            "large_display",
            "a large, easy-to-read display",
            "large display",
            (r"(?:large|big|easy[- ]to[- ]read|readable|clear) (?:display|screen|numbers|digits|text|print|font)",
             r"\breadable\b", r"easy to read"),
            (r"(?:large|big|extra[- ]large|jumbo|oversized|easy[- ]to[- ]read|backlit)\s+(?:lcd\s+)?(?:display|screen|digits|numbers|font|print)",),
        ),
        Feature(
            "large_buttons",
            "large, simple buttons",
            "big buttons",
            (r"(?:large|big) (?:buttons|keys|controls)", r"simple controls", r"easy (?:buttons|controls)"),
            (r"(?:large|big|oversized|jumbo) (?:buttons?|keys|keypad)", r"one[- ](?:touch|button)", r"simple controls"),
        ),
        Feature(
            "cordless",
            "cordless",
            "cordless",
            (r"\bcordless\b", r"\bwireless\b", r"no cords?", r"battery[- ](?:powered|operated)"),
            (r"\bcordless\b", r"\bwireless\b", r"battery[- ](?:powered|operated)", r"rechargeable"),
        ),
        Feature(
            "quiet",
            "quiet",
            "quiet",
            (r"\bquiet\b", r"\bsilent\b", r"low noise", r"not (?:too )?loud"),
            (r"\bquiet\b", r"\bsilent\b", r"low[- ]noise", r"noise[- ]reduc"),
        ),
        Feature(
            "comfortable",
            "comfortable to hold or wear",
            "comfortable",
            (r"\bcomfortable\b", r"\bcomfort\b", r"\bergonomic\b", r"easy grip", r"non[- ]slip"),
            (r"\bcomfort", r"\bergonomic", r"easy[- ]grip", r"non[- ]slip", r"\bcushion"),
        ),
        Feature(
            "loud",
            "loud, clear sound",
            "loud",
            (r"\bloud\b", r"hard of hearing", r"hearing aid", r"\bamplified\b"),
            (r"\bloud\b", r"\bamplif", r"hearing aid compatible", r"\bhac\b"),
        ),
    )
}

MUST_MARKERS = re.compile(r"\b(?:must|has to|have to|needs to|need it to|required|essential|only if|non[- ]negotiable)\b", re.I)


def asked_features(text: str) -> tuple[list[str], list[str]]:
    """(required, preferred) feature keys mentioned in a request.

    A feature is required when its clause says must/has to/needs to; otherwise it is a preference.
    "loud" is skipped when it only appears in "not too loud".
    """
    required: list[str] = []
    preferred: list[str] = []
    for clause in re.split(r"[.;,!?]|\band\b|\bbut\b", text, flags=re.I):
        must = bool(MUST_MARKERS.search(clause))
        for feature in FEATURES.values():
            if feature.key == "loud" and re.search(r"not (?:too )?loud", clause, re.I):
                continue
            if feature.key == "easy_to_use" and re.search(r"easy[- ]to[- ]read", clause, re.I):
                if not re.search(r"easy[- ]to[- ]use|simple|operate|user", clause, re.I):
                    continue
            if any(re.search(pattern, clause, re.I) for pattern in feature.asked):
                target = required if must else preferred
                if feature.key not in required and feature.key not in target:
                    target.append(feature.key)
    preferred = [key for key in preferred if key not in required]
    return required, preferred


def evidence_for(feature_key: str, text: str) -> str | None:
    """The words in a listing that show the feature, or None."""
    feature = FEATURES.get(feature_key)
    if feature is None:
        return None
    for pattern in feature.evidence:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0)
    return None


def stem(word: str) -> str:
    """A rough singular: vacuums -> vacuum, batteries -> battery, boxes -> box."""
    word = word.lower()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text: str) -> set[str]:
    return {stem(word) for word in re.findall(r"[a-z0-9]+", text.lower())}


def label(feature_key: str) -> str:
    feature = FEATURES.get(feature_key)
    return feature.label if feature else feature_key.replace("_", " ")


def search_terms(feature_key: str) -> str:
    feature = FEATURES.get(feature_key)
    return feature.search_terms if feature else feature_key.replace("_", " ")
