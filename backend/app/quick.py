"""Handle simple visual commands locally so they don't spend LLM tokens."""

import re

from .schemas import PageStyleOp, ReframeResponse, RestoreOp

_FILLER = {
    "please", "can", "could", "you", "make", "the", "a", "an", "it", "this", "page", "site", "text", "font",
    "words", "letters", "is", "too", "so", "very", "much", "more", "bit", "little", "i", "me", "my", "for",
    "to", "turn", "on", "off", "use", "switch", "put", "set", "and", "a", "lot", "way", "really", "just",
    "everything", "all", "of", "some", "be", "want", "need", "would", "like", "hard", "read", "see",
}

_INTENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(undo|restore|reset|original|normal|go back to normal|start over)\b"), "restore"),
    (re.compile(r"\b(bigger|larger|increase|enlarge|zoom in|too small|tiny|can'?t read)\b"), "bigger"),
    (re.compile(r"\b(smaller|decrease|shrink|zoom out|too big|too large|huge)\b"), "smaller"),
    (re.compile(r"\b(dark mode|dark|too bright|night mode|darker)\b"), "dark"),
    (re.compile(r"\b(light mode|lighter|white background)\b"), "light"),
    (re.compile(r"\b(high contrast|contrast|faded|washed out)\b"), "contrast"),
    (re.compile(r"\b(hide|remove|no) (the )?(images|pictures|photos|imagery)\b"), "hide_images"),
    (re.compile(r"\b(show|bring back) (the )?(images|pictures|photos)\b"), "show_images"),
    (re.compile(r"\b(stop|no|reduce|less) (the )?(animations?|motion|moving|movement)\b"), "reduce_motion"),
    (re.compile(r"\b(readable|easier|simpler|clearer|plain) font\b"), "readable_font"),
    (re.compile(r"\bunderline (the )?links\b"), "underline_links"),
    (re.compile(r"\b(more space|spacing|line spacing|spread out|cramped)\b"), "spacing"),
]


def _current_scale(applied: list[str]) -> float:
    for entry in applied:
        m = re.search(r"font_scale=([\d.]+)", entry)
        if m:
            return float(m.group(1))
    return 1.0


def quick_reframe(message: str, applied: list[str]) -> ReframeResponse | None:
    text = message.lower().strip().rstrip(".!?")
    if len(text) > 80:
        return None

    found: list[str] = []
    leftover = text
    for pattern, intent in _INTENTS:
        if pattern.search(leftover):
            found.append(intent)
            leftover = pattern.sub(" ", leftover)
    if not found:
        return None
    # Anything meaningful left over (ads, bills, buttons...) needs the model.
    if any(w not in _FILLER for w in re.findall(r"[a-z']+", leftover)):
        return None

    if "restore" in found:
        return ReframeResponse(reply="Back to the original page.", operations=[RestoreOp(op="restore")])

    style: dict = {}
    scale = _current_scale(applied)
    if "bigger" in found:
        style["font_scale"] = round(min(2.5, max(scale, 1.0) + 0.25 if scale > 1 else 1.3), 2)
    if "smaller" in found:
        style["font_scale"] = round(max(1.0, scale - 0.25), 2)
    if "dark" in found:
        style["dark_mode"] = True
    if "light" in found:
        style["dark_mode"] = False
    if "contrast" in found:
        style["high_contrast"] = True
    if "hide_images" in found:
        style["hide_images"] = True
    if "show_images" in found:
        style["hide_images"] = False
    if "reduce_motion" in found:
        style["reduce_motion"] = True
    if "readable_font" in found:
        style["readable_font"] = True
    if "underline_links" in found:
        style["underline_links"] = True
    if "spacing" in found:
        style["line_height"] = 1.8
    if not style:
        return None
    return ReframeResponse(reply="Done.", operations=[PageStyleOp(op="page_style", **style)])
