import asyncio
import json
import logging
import re
import time
from urllib.parse import urlsplit

from groq import AsyncGroq, BadRequestError, NotFoundError, RateLimitError
from pydantic import TypeAdapter, ValidationError

from .config import get_settings
from .schemas import (
    Operation,
    ReframeRequest,
    ReframeResponse,
    RenderOp,
    SnapElement,
    Snapshot,
)

log = logging.getLogger("reframe")
_op_adapter = TypeAdapter(Operation)

CONSEQUENTIAL_BUTTON = re.compile(
    r"\b(pay|submit|delete|remove|purchase|buy|order|checkout|confirm|transfer|send|"
    r"sign ?out|log ?out|cancel|unsubscribe|close account|place)\b",
    re.I,
)
CONSEQUENTIAL_LINK = re.compile(
    r"\b(delete|sign ?out|log ?out|unsubscribe|close account|cancel (my )?(account|service|plan))\b",
    re.I,
)
_TAG_WITH_REF = re.compile(r"<[a-zA-Z][^<>]*?\bdata-rf-(target|bind)\s*=\s*[\"'](r\d{1,5})[\"'][^<>]*>")

SYSTEM_PROMPT = """You are ReFrame, a front-end accessibility layer inside a Chrome extension. \
You only change how the CURRENT page looks and is laid out, for older adults and people with low \
digital literacy. Back-end behaviour, accounts and data are not your concern.

Return ONE JSON object: {"reply": "<one short plain sentence>", "operations": [ ... ]}

OPERATIONS
1. {"op":"render","html":"...","css":"...","isolate":true,"keep_refs":["rN"]}
   Inserts your HTML into the page itself, so it inherits the site's CSS.
   - Layout: with isolate=true your HTML sits in a centered full-width column (max 880px) directly under the
     site's header, on the site's page background; the rest of the page is hidden. Build ONE panel that fills
     that column (width:100%): a heading, optional short info lines copied from the page, then full-width
     stacked buttons with the primary one first.
   - Make it look like it belongs to the site: reuse the site's own class names (shown as .cls in PAGE, and
     THEME.card / THEME.button / THEME.heading) and the THEME fonts/colours. Never introduce a new visual style.
   - Bigger and calmer than the original: buttons width:100%, at least 60px tall, text 20-24px, generous
     spacing, left-aligned text; the primary action styled like the site's main button, others as outlined
     versions in the same colours.
   - Clickable things: <button data-rf-target="rN">label</button> clicks the real element rN.
     Form fields: <input data-rf-bind="rN"> / <select data-rf-bind="rN"></select> mirror the real field rN
     (select options are copied automatically).
     Add data-rf-confirm="1" on anything that pays, submits, deletes, buys, sends or signs out.
     Put data-rf-primary="1" on the single most likely next step.
   - Button labels say what the button really does. A link that only leads toward the goal is labelled by
     where it goes ("Go to Account Center"), never as the goal itself ("Download bill") unless it does that.
   - No <script>, <form>, <iframe>, event handlers, href or url(). Text must come from the page; never invent
     amounts, dates or names. Use "context" to label similar controls (e.g. "October 2026 bill - $84.12").
   - css: plain rules for elements inside your HTML (it is scoped automatically). Keep it short.
   - keep_refs: page parts to keep visible, normally just the site header block (THEME.header.ref).
   - isolate=false + "anchor_ref"/"position" (before|after|prepend|append) instead adds your HTML next to an
     existing element without hiding anything; use that only for small additions.
2. {"op":"hide","refs":["rN",...]} / {"op":"unhide","refs":[...]} / {"op":"highlight","refs":[...]}
3. {"op":"relabel","ref":"rN","text":"plain words"}
4. {"op":"style","ref":"rN","styles":{"font-size":"22px"}}  (no url())
5. {"op":"page_style","font_scale":1.0-2.5,"line_height":1.0-2.5,"high_contrast":bool,"dark_mode":bool,
    "hide_images":bool,"reduce_motion":bool,"readable_font":bool,"underline_links":bool}
6. {"op":"scroll_to","ref":"rN"}
7. {"op":"restore"}  undo everything

WHEN TO USE WHAT
- Task goals ("pay my bill", "find statements") -> one "render" with the few controls that lead to the goal.
  If the goal is on another page, show the link that most likely leads there as the primary button.
  A render with isolate=true already hides popups, so don't add buttons to close them. Only when you are
  not rendering and a popup blocks the page, hide it (or offer its close button).
- Small visual wishes ("bigger text", "hide ads", "too bright") -> page_style / style / hide / relabel, no render.
- Use ONLY refs from PAGE. Never put values of SENSITIVE fields anywhere.
- "reply": short, kind, no jargon; name the button to press next.
"""


def _describe(el: SnapElement) -> str:
    parts = [el.ref]
    if el.kind == "block":
        parts.append(f"block<{el.tag}>")
    else:
        kind = el.role or el.tag
        if el.input_type:
            kind += f"[{el.input_type}]"
        parts.append(kind)
    if el.cls:
        parts.append(f".{el.cls}")
    if el.label:
        parts.append(f'label="{el.label}"')
    if el.text:
        parts.append(f'"{el.text}"')
    if el.context:
        parts.append(f'context="{el.context}"')
    if el.href:
        parts.append(f"-> {el.href}")
    if el.region:
        parts.append(f"in:{el.region}")
    if el.sensitive:
        parts.append("SENSITIVE")
    if el.hidden:
        parts.append("(hidden)")
    return " | ".join(parts)


MAX_CONTROLS_IN_PROMPT = 70
MAX_BLOCKS_IN_PROMPT = 14
_STOPWORDS = {
    "the", "and", "for", "you", "your", "want", "need", "help", "please", "can", "how", "what", "with",
    "this", "that", "page", "make", "show", "find", "get", "into", "from", "have", "are", "was",
}
_REGION_SCORE = {"dialog": 6, "main": 3, "form": 3, "nav": 1, "header": 1, "aside": -1, "footer": -3}


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w.rstrip("s") for w in words if len(w) > 2 and w not in _STOPWORDS}


def _select_elements(s: Snapshot, message: str) -> list[SnapElement]:
    """Keep the prompt small: the most goal-relevant controls plus a few layout blocks."""
    goal = _keywords(message)
    header_ref = (s.theme.get("header") or {}).get("ref") if isinstance(s.theme, dict) else None
    seen: set[tuple] = set()
    controls: list[tuple[float, int, SnapElement]] = []
    blocks: list[tuple[float, int, SnapElement]] = []
    for i, el in enumerate(s.elements):
        hay = _keywords(f"{el.text} {el.label or ''} {el.context or ''} {el.href or ''}")
        score = 5 * len(goal & hay) + _REGION_SCORE.get(el.region or "", 0)
        if el.kind == "control":
            key = (el.text.lower(), el.href, el.label)
            if key in seen and not el.context:
                continue
            seen.add(key)
            if el.tag in ("input", "select", "textarea"):
                score += 2
            if el.href == "#" and not el.text:
                score -= 3
            controls.append((score - i * 0.01, i, el))
        else:
            if el.ref == header_ref:
                score += 10
            if el.text.startswith("["):
                score += 2
            blocks.append((score - i * 0.01, i, el))
    keep = sorted(controls, reverse=True)[:MAX_CONTROLS_IN_PROMPT] + sorted(blocks, reverse=True)[:MAX_BLOCKS_IN_PROMPT]
    return [el for _, _, el in sorted(keep, key=lambda t: t[1])]


def _format_snapshot(s: Snapshot, message: str) -> str:
    lines = [f"URL: {s.url}", f"TITLE: {s.title}"]
    if s.theme:
        lines.append("THEME: " + json.dumps(s.theme, separators=(",", ":")))
    if s.headings:
        lines.append("HEADINGS: " + " / ".join(s.headings[:12]))
    if s.has_dialog:
        lines.append("NOTE: a popup is open")
    lines.append("PAGE:")
    lines.extend(_describe(e) for e in _select_elements(s, message))
    return "\n".join(lines)


def _build_messages(req: ReframeRequest) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in req.history[-6:]:
        messages.append({"role": turn.role, "content": turn.content})
    applied = ", ".join(req.applied) if req.applied else "nothing"
    intro = (
        "The user just moved to this page while working on the request below. Rebuild this page for it."
        if req.continuation
        else "User request."
    )
    messages.append(
        {
            "role": "user",
            "content": (
                f"{intro}\nREQUEST: {req.message}\nCURRENTLY APPLIED: {applied}\n\n"
                f"{_format_snapshot(req.snapshot, req.message)}\n\nJSON only."
            ),
        }
    )
    return messages


def _is_cross_origin(href: str | None, page_url: str) -> bool:
    if not href or href.startswith("/") or href == "#":
        return False
    target = urlsplit(href)
    return bool(target.netloc) and target.netloc != urlsplit(page_url).netloc


def _needs_confirm(el: SnapElement, snapshot: Snapshot) -> bool:
    words = f"{el.text} {el.label or ''}"
    is_button = el.tag in ("button", "input") or el.role == "button"
    pattern = CONSEQUENTIAL_BUTTON if is_button else CONSEQUENTIAL_LINK
    return bool(pattern.search(words)) or _is_cross_origin(el.href, snapshot.url)


def _check_render(op: RenderOp, by_ref: dict[str, SnapElement], snapshot: Snapshot) -> RenderOp | None:
    bound = 0

    def fix_tag(m: re.Match) -> str:
        nonlocal bound
        tag, kind, ref = m.group(0), m.group(1), m.group(2)
        el = by_ref.get(ref)
        if el is None or el.kind != "control":
            return re.sub(r"\sdata-rf-(target|bind)\s*=\s*[\"']r\d{1,5}[\"']", "", tag)
        bound += 1
        if kind != "target":
            return tag
        model_wants = "data-rf-confirm" in tag
        tag = re.sub(r"\sdata-rf-confirm(\s*=\s*[\"'][^\"']*[\"'])?", "", tag)
        is_button = el.tag in ("button", "input") or el.role == "button"
        if _needs_confirm(el, snapshot) or (model_wants and is_button):
            tag = tag[:-1].rstrip("/") + ' data-rf-confirm="1">'
        return tag

    op.html = _TAG_WITH_REF.sub(fix_tag, op.html)
    if bound == 0 and op.isolate:
        return None
    op.keep_refs = [r for r in op.keep_refs if r in by_ref]
    if op.anchor_ref and op.anchor_ref not in by_ref:
        op.anchor_ref = None
    return op


def _refs_in(op) -> list[str]:
    refs = getattr(op, "refs", None)
    if refs:
        return list(refs)
    ref = getattr(op, "ref", None)
    return [ref] if ref else []


def _validate_ops(raw_ops: list, snapshot: Snapshot) -> tuple[list, int]:
    by_ref = {e.ref: e for e in snapshot.elements}
    valid, dropped = [], 0
    for raw in raw_ops if isinstance(raw_ops, list) else []:
        try:
            op = _op_adapter.validate_python(raw)
        except ValidationError:
            dropped += 1
            continue
        if isinstance(op, RenderOp):
            checked = _check_render(op, by_ref, snapshot)
            if checked is None:
                dropped += 1
            else:
                valid.append(checked)
            continue
        refs = _refs_in(op)
        if any(r not in by_ref for r in refs):
            known = [r for r in refs if r in by_ref]
            if hasattr(op, "refs") and known:
                op.refs = known
            else:
                dropped += 1
                continue
        valid.append(op)
    return valid, dropped


FALLBACK_MODEL = "openai/gpt-oss-120b"
RATE_LIMIT_MODEL = "openai/gpt-oss-20b"
_client: AsyncGroq | None = None


def _groq() -> AsyncGroq:
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=get_settings().groq_api_key, max_retries=0, timeout=20.0)
    return _client


async def _complete_once(model: str, messages: list[dict]):
    return await _groq().chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.2,
        # Groq reserves max tokens against the per-minute token budget, so keep this near real output size.
        max_completion_tokens=1400,
        **({"reasoning_effort": "low"} if "gpt-oss" in model else {}),
    )


def _retry_after(err: RateLimitError) -> float:
    try:
        return float(err.response.headers.get("retry-after", "2"))
    except (TypeError, ValueError):
        return 2.0


async def _complete(model: str, messages: list[dict]):
    try:
        return await _complete_once(model, messages)
    except RateLimitError as err:
        wait = _retry_after(err)
        if model != RATE_LIMIT_MODEL:
            log.warning("Groq rate limit on %s (retry in %.1fs); using %s", model, wait, RATE_LIMIT_MODEL)
            try:
                return await _complete_once(RATE_LIMIT_MODEL, messages)
            except RateLimitError as err2:
                wait = min(wait, _retry_after(err2))
        if wait > 6:
            raise
        await asyncio.sleep(wait)
        return await _complete_once(model, messages)


async def reframe(req: ReframeRequest) -> ReframeResponse:
    s = get_settings()
    model = s.groq_fast_model if req.continuation else s.groq_model
    messages = _build_messages(req)
    started = time.perf_counter()

    data: dict | None = None
    for attempt in range(2):
        try:
            completion = await _complete(model, messages)
        except BadRequestError as err:
            if "json_validate_failed" not in str(err) or attempt == 1:
                raise
            log.warning("Groq rejected malformed JSON (attempt %d); retrying", attempt + 1)
            continue
        except NotFoundError:
            if model == FALLBACK_MODEL:
                raise
            log.warning("Groq model %s not found, using %s", model, FALLBACK_MODEL)
            model = FALLBACK_MODEL
            completion = await _complete(model, messages)
        usage = completion.usage
        if usage is not None:
            log.info(
                "groq usage prompt=%s completion=%s queue=%.2fs completion_time=%.2fs total=%.2fs",
                usage.prompt_tokens, usage.completion_tokens,
                getattr(usage, "queue_time", 0) or 0, getattr(usage, "completion_time", 0) or 0,
                getattr(usage, "total_time", 0) or 0,
            )
        content = completion.choices[0].message.content or ""
        try:
            data = json.loads(content)
            break
        except json.JSONDecodeError:
            log.warning("Groq returned invalid JSON (attempt %d)", attempt + 1)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": "That was not valid JSON. Return only the JSON object."})

    model_ms = int((time.perf_counter() - started) * 1000)
    if not isinstance(data, dict):
        return ReframeResponse(reply="Something went wrong. Please try again.", operations=[], model_ms=model_ms)

    ops, dropped = _validate_ops(data.get("operations", []), req.snapshot)
    reply = str(data.get("reply") or "").strip()[:600] or ("Done." if ops else "I couldn't change this page.")
    log.info("reframe model=%s ms=%d ops=%d dropped=%d", model, model_ms, len(ops), dropped)
    return ReframeResponse(reply=reply, operations=ops, dropped=dropped, model_ms=model_ms)
