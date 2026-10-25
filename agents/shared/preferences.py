"""The settings a caregiver may change, and how to read a request to change one.

Only these low-risk settings exist. Each has a fixed set of values, so a caregiver
can never put free text into the orchestrator's instructions. Anything that could
affect money or choices (budgets, blocked shops, orders, payments) is refused: it
would need the person's own separate approval and isn't part of this version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    values: tuple[str, ...]
    default: str


LANGUAGES = {
    "english": "en", "spanish": "es", "espanol": "es", "french": "fr", "german": "de", "italian": "it",
    "portuguese": "pt", "chinese": "zh", "mandarin": "zh", "hindi": "hi", "arabic": "ar", "korean": "ko",
    "japanese": "ja", "vietnamese": "vi", "tagalog": "tl", "russian": "ru", "polish": "pl",
}
LANGUAGE_NAMES = {code: name.title() for name, code in LANGUAGES.items() if name != "espanol"}
LANGUAGE_NAMES["zh"] = "Chinese"

SETTINGS: dict[str, Setting] = {
    setting.key: setting
    for setting in (
        Setting("wording", "simpler wording", ("simple", "standard"), "standard"),
        Setting("explanation_length", "shorter explanations", ("short", "standard"), "standard"),
        Setting("text_size", "larger text (a display hint for the browser extension)", ("standard", "large", "extra_large"), "standard"),
        Setting("language", "preferred language", tuple(sorted(set(LANGUAGES.values()))), "en"),
        Setting("confirm_complex_steps", "checking with you before bigger steps", ("on", "off"), "off"),
        Setting("highlight_clear_features", "pointing out clear product features", ("on", "off"), "off"),
    )
}

# Settings that could change outcomes. Never changeable by a caregiver here.
OUTCOME_WORDS = re.compile(
    r"\b(?:budget|spend(?:ing)?|price limit|max(?:imum)? price|block(?:ed)?|ban|merchants?|sellers?|stores?|shops?|"
    r"brands? only|categor(?:y|ies)|pay(?:ment|ments)?|card|checkout|order|purchase|buy (?:it|for|on|from)|"
    r"only buy|shop (?:at|on|from))\b",
    re.I,
)


def defaults() -> dict[str, str]:
    return {key: setting.default for key, setting in SETTINGS.items()}


def valid(setting: str, value: str) -> bool:
    return setting in SETTINGS and value in SETTINGS[setting].values


def sanitize(preferences: dict[str, str]) -> dict[str, str]:
    """Drop anything that isn't a known setting with an allowed value."""
    return {key: value for key, value in preferences.items() if valid(key, value)}


def describe(setting: str, value: str) -> str:
    if setting == "language":
        return f"preferred language: {LANGUAGE_NAMES.get(value, value)}"
    if setting == "text_size":
        return {"large": "larger text", "extra_large": "extra-large text"}.get(value, "standard text size")
    label = SETTINGS[setting].label if setting in SETTINGS else setting
    on = value in {"on", "simple", "short"}
    return f"{label}: {'on' if on else 'off'}"


_OFF = re.compile(r"\b(?:off|stop|disable|no longer|don't|do not|normal|standard|regular|default|undo|reset|remove)\b", re.I)


@dataclass(frozen=True)
class Change:
    setting: str
    value: str


@dataclass(frozen=True)
class Refused:
    reason: str


def parse_change(text: str) -> Change | Refused | None:
    """Read a caregiver's request into one allowed change, a refusal, or None if it isn't a change."""
    lowered = text.lower()
    off = bool(_OFF.search(lowered))
    language = re.search(r"\b(?:in|to|speak|use|language(?: to| is)?)\s+([a-z]+)\b", lowered)
    if language and language.group(1) in LANGUAGES:
        return Change("language", LANGUAGES[language.group(1)])
    for name, code in LANGUAGES.items():
        if re.search(rf"\b{name}\b", lowered) and re.search(r"\blanguage\b|\bspeak\b|\breply\b|\banswer", lowered):
            return Change("language", code)
    if re.search(r"simpl(?:e|er)\s+(?:words|wording|language)|plain (?:words|language|english)|easier words", lowered):
        return Change("wording", "standard" if off else "simple")
    if re.search(r"short(?:er)?\s+(?:explanations?|answers?|replies|messages)|\bbrief(?:er)?\b|less detail", lowered):
        return Change("explanation_length", "standard" if off else "short")
    if re.search(r"(?:extra[- ]large|huge|very (?:large|big))\s+(?:text|font|print|letters)", lowered):
        return Change("text_size", "standard" if off else "extra_large")
    if re.search(r"(?:large|larger|big|bigger)\s+(?:text|font|print|letters)|text size", lowered):
        return Change("text_size", "standard" if off else "large")
    if re.search(r"(?:ask|check|confirm)(?:s|ing)?\s+(?:(?:with\s+)?(?:them|her|him|me)\s+)?(?:first\s+)?before|"
                 r"confirm(?:ation)? (?:before|for)", lowered):
        return Change("confirm_complex_steps", "off" if off else "on")
    if re.search(r"(?:highlight|point out|show|call out)\s+(?:the\s+)?(?:clear\s+|key\s+|main\s+)?features|clear features", lowered):
        return Change("highlight_clear_features", "off" if off else "on")
    if OUTCOME_WORDS.search(lowered):
        return Refused(
            "That could change what gets bought or where, so Hold My Hand doesn't let a caregiver set it. "
            "The person can say it themselves when they ask for help."
        )
    return None


def parse_scopes(text: str) -> list[str]:
    """Which settings a person agrees to let a caregiver change."""
    lowered = text.lower()
    if re.search(r"\b(?:all|everything|any of them|all of them|every one)\b", lowered):
        return list(SETTINGS)
    picked = []
    patterns = {
        "wording": r"simpl|plain|wording|words",
        "explanation_length": r"short|brief|length|explanation",
        "text_size": r"text|font|size|bigger|larger",
        "language": r"language|spanish|french|translat|" + "|".join(LANGUAGES),
        "confirm_complex_steps": r"ask|check|confirm|before",
        "highlight_clear_features": r"highlight|feature|point out",
    }
    for key, pattern in patterns.items():
        if re.search(pattern, lowered):
            picked.append(key)
    numbers = [int(n) for n in re.findall(r"\b([1-6])\b", lowered)]
    for number in numbers:
        key = list(SETTINGS)[number - 1]
        if key not in picked:
            picked.append(key)
    return picked
