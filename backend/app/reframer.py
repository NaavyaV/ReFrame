import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from groq import AsyncGroq, BadRequestError, RateLimitError
from pydantic import TypeAdapter, ValidationError

from .config import get_settings
from .ratelimit import GroqBudget, RateLimited
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
_TAG_WITH_EACH = re.compile(r"<[a-zA-Z][^<>]*?\bdata-rf-each\s*=\s*[\"'](r\d{1,5})-(r\d{1,5})[\"'][^<>]*>")
_TAG_WITH_REF = re.compile(r"<[a-zA-Z][^<>]*?\bdata-rf-(target|bind)\s*=\s*[\"'](r\d{1,5})[\"'][^<>]*>")

SYSTEM_PROMPT = """You are ReFrame, a front-end accessibility layer for older adults. You only change how the \
CURRENT page looks. Reply with ONE valid JSON object (plain string values, no code or concatenation):
{"reply":"...","done":false,"operations":[...]}
"reply": 1-2 warm, everyday sentences to an older adult: what this page now shows and the exact label of the
  data-rf-primary button to press next. Only describe what is really on the page. Never just a button name, no jargon.
"done": true only when this page shows the goal is finished (e.g. a confirmation) and no more help is needed.

OPS
render {"op":"render","html":"...","css":"...","isolate":true,"keep_refs":["rN"]}
  HTML is placed in the page under the site header, in a centered 880px column; the rest is hidden (popups too).
  keep_refs = the "ref" value of THEME header. Build ONE full-width panel: heading, optional short info copied from the page, then full-width stacked buttons
  (>=60px tall, 20-24px text, left-aligned), primary first. Match the site: copy the literal "cls" values from THEME
  (card, button, heading) and PAGE into class="..." attributes, use THEME colours/fonts; no new visual style. Keep css short (auto-scoped).
  <button data-rf-target="rN">label</button> clicks real element rN; <input|select data-rf-bind="rN"> mirrors
  real field rN. data-rf-primary="1" on the single next step; data-rf-confirm="1" on pay/submit/delete/buy/send.
  Labels say what the button really does; a link leading toward the goal is "Go to <place>".
  No script/form/iframe/href/url()/event handlers. Never invent amounts, dates or names; use "context".
  Long lists (LISTS below: events, results, rows) -> <div data-rf-each="rA-rB" data-rf-class="site classes"></div>;
  the extension fills in one button per item with its real text. Never retype list items.
  isolate=false + "anchor_ref" + "position"(before|after|prepend|append): small addition, nothing hidden.
hide|unhide|highlight {"op":"hide","refs":["rN"]}
relabel {"op":"relabel","ref":"rN","text":"plain words"}
style {"op":"style","ref":"rN","styles":{"font-size":"22px"}}
page_style {"op":"page_style","font_scale":1-2.5,"line_height":1-2.5,"high_contrast":b,"dark_mode":b,"hide_images":b,"reduce_motion":b,"readable_font":b,"underline_links":b}
scroll_to {"op":"scroll_to","ref":"rN"}   restore {"op":"restore"}

RULES
Task goals -> one render with the few controls toward the goal (if it is on another page, the best link is primary).
Small visual wishes -> page_style/style/hide/relabel, no render. Only refs from PAGE. Never output SENSITIVE values.
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


MAX_CONTROLS_IN_PROMPT = 45
MAX_BLOCKS_IN_PROMPT = 8
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
    lists = _find_lists(s)
    if lists:
        lines.append("LISTS:")
        lines.extend(lists)
    return "\n".join(lines)


MIN_LIST_ITEMS = 5


def _ref_num(ref: str) -> int:
    return int(ref[1:])


def _find_lists(s: Snapshot) -> list[str]:
    """Runs of consecutive, similar controls (same tag and region), e.g. schedule rows or search results."""
    controls = sorted((e for e in s.elements if e.kind == "control"), key=lambda e: _ref_num(e.ref))
    runs: list[list[SnapElement]] = []
    for el in controls:
        prev = runs[-1][-1] if runs else None
        if (
            prev
            and _ref_num(el.ref) == _ref_num(prev.ref) + 1
            and el.tag == prev.tag
            and el.region == prev.region
            and el.input_type is None
        ):
            runs[-1].append(el)
        else:
            runs.append([el])
    out = []
    for run in runs:
        if len(run) < MIN_LIST_ITEMS:
            continue
        first, last = run[0], run[-1]
        out.append(
            f'{first.ref}-{last.ref}: {len(run)} {first.tag}s in {first.region or "page"}, '
            f'e.g. "{(first.text or first.label or "")[:60]}" ... "{(last.text or last.label or "")[:60]}"'
        )
    return out


def _build_messages(req: ReframeRequest) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if not req.continuation:
        # Only the user's own recent requests; assistant replies add tokens without helping.
        recent = [t for t in req.history if t.role == "user"][-3:]
        if recent:
            messages.append({"role": "user", "content": "Earlier requests: " + " | ".join(t.content[:200] for t in recent)})
            messages.append({"role": "assistant", "content": "Noted."})
    applied = ", ".join(req.applied) if req.applied else "nothing"
    intro = (
        "The user just moved to this page while working on the request below. Rebuild this page for it; "
        "the reply says where they are now and what to press next."
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

    def fix_each(m: re.Match) -> str:
        nonlocal bound
        tag, start, end = m.group(0), m.group(1), m.group(2)
        items = [by_ref.get(f"r{i}") for i in range(_ref_num(start), _ref_num(end) + 1)]
        if _ref_num(end) < _ref_num(start) or not items[0] or not items[-1]:
            return re.sub(r"\sdata-rf-each\s*=\s*[\"'][^\"']*[\"']", "", tag)
        bound += len(items)
        if any(el and el.kind == "control" and _needs_confirm(el, snapshot) for el in items):
            tag = tag[:-1].rstrip("/") + ' data-rf-confirm="1">'
        return tag

    op.html = _TAG_WITH_EACH.sub(fix_each, op.html)
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
        if isinstance(raw, dict) and isinstance(raw.get("keep_refs"), list):
            raw["keep_refs"] = [r for r in raw["keep_refs"] if isinstance(r, str) and r in by_ref]
        try:
            op = _op_adapter.validate_python(raw)
        except ValidationError as err:
            log.warning("dropped op %s: %s", str(raw)[:300], str(err).splitlines()[:4])
            dropped += 1
            continue
        if isinstance(op, RenderOp):
            if op.isolate:
                # Isolation already hides the rest of the page; only the site header stays visible.
                header = (snapshot.theme.get("header") or {}).get("ref") if snapshot.theme else None
                op.keep_refs = [header] if header in by_ref else []
                op.anchor_ref = None
                op.position = "prepend"
            checked = _check_render(op, by_ref, snapshot)
            if checked is None:
                log.warning("dropped render: %s", op.html[:300])
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
    if any(isinstance(o, RenderOp) and o.isolate for o in valid):
        valid = [o for o in valid if o.op not in ("hide", "highlight", "scroll_to")]
    return valid, dropped


# Groq reserves max tokens against the per-minute token budget, so keep this near real output size.
MAX_COMPLETION_TOKENS = 1500
# Typical reply size; the reservation is replaced by real usage once the call returns.
EXPECTED_OUTPUT_TOKENS = 400
USAGE_FILE = Path(__file__).resolve().parents[1] / ".groq-usage.json"
_client: AsyncGroq | None = None
_budgets: dict[str, GroqBudget] = {}


def _groq() -> AsyncGroq:
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=get_settings().groq_api_key, max_retries=0, timeout=20.0)
    return _client


def _groq_budget(model: str) -> GroqBudget:
    # Groq limits each model separately.
    if model not in _budgets:
        s = get_settings()
        _budgets[model] = GroqBudget(
            s.groq_rpm, s.groq_tpm, s.groq_rpd, s.groq_tpd, s.groq_max_wait, store=USAGE_FILE, name=model
        )
    return _budgets[model]


def _estimate_tokens(messages: list[dict]) -> int:
    # ~4 chars per token plus per-message overhead; settled with real usage after the call.
    return sum(len(m["content"]) // 4 + 8 for m in messages) + EXPECTED_OUTPUT_TOKENS


def _retry_after(err: RateLimitError) -> float:
    try:
        return float(err.response.headers.get("retry-after", "10"))
    except (TypeError, ValueError):
        return 10.0


def _pick_model(models: list[str], tokens: int) -> str:
    """First model with budget now, otherwise the one that frees up soonest."""
    waits = [(_groq_budget(m).wait_for(tokens), i, m) for i, m in enumerate(models)]
    ready = [m for w, _, m in waits if w == 0]
    return ready[0] if ready else min(waits)[2]


async def _complete(model: str, messages: list[dict], json_mode: bool = True):
    """One Groq call, throttled per model; overflows to GROQ_FAST_MODEL when the main model is out of budget."""
    tokens = _estimate_tokens(messages)
    models = list(dict.fromkeys([model, get_settings().groq_fast_model]))
    chosen = _pick_model(models, tokens)
    if chosen != model:
        log.info("Groq budget low on %s; using %s", model, chosen)
    try:
        return await _complete_on(chosen, messages, tokens, json_mode)
    except RateLimited:
        others = [m for m in models if m != chosen and _groq_budget(m).wait_for(tokens) == 0]
        if not others:
            raise
        log.info("Groq limit on %s; using %s", chosen, others[0])
        return await _complete_on(others[0], messages, tokens, json_mode)


async def _complete_on(model: str, messages: list[dict], tokens: int, json_mode: bool = True):
    """A 429 pauses calls to this model; it is never retried on the same model."""
    budget = _groq_budget(model)
    entry = await budget.acquire(tokens)
    try:
        completion = await _groq().chat.completions.create(
            model=model,
            messages=messages,
            **({"response_format": {"type": "json_object"}} if json_mode else {}),
            temperature=0.2,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            **({"reasoning_effort": "low"} if "gpt-oss" in model else {}),
        )
    except RateLimitError as err:
        wait = _retry_after(err)
        log.warning("Groq rate limit on %s; pausing model calls for %.1fs", model, wait)
        budget.cooldown(wait)
        raise RateLimited(wait) from err
    if completion.usage is not None:
        budget.settle(entry, completion.usage.total_tokens)
    return completion


def _salvage(err: BadRequestError) -> dict | None:
    body = err.body if isinstance(err.body, dict) else {}
    return _extract_json((body.get("error") or body).get("failed_generation") or "")


def _extract_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def reframe(req: ReframeRequest) -> ReframeResponse:
    s = get_settings()
    model = s.groq_fast_model if req.continuation else s.groq_model
    messages = _build_messages(req)
    started = time.perf_counter()

    data: dict | None = None
    used_model = model
    for attempt in range(2):
        try:
            # Groq's JSON mode rejects small slips outright; the retry parses the text itself.
            completion = await _complete(model, messages, json_mode=attempt == 0)
            used_model = completion.model or model
        except BadRequestError as err:
            if "json_validate_failed" not in str(err):
                raise
            data = _salvage(err)
            if data is not None:
                log.warning("Groq rejected JSON (attempt %d); salvaged it", attempt + 1)
                break
            body = err.body if isinstance(err.body, dict) else {}
            failed = str((body.get("error") or body).get("failed_generation") or "")
            log.warning("Groq rejected malformed JSON (attempt %d): %r", attempt + 1, failed[:200])
            continue
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
            data = _extract_json(content)
            if data is not None:
                break
            log.warning("Groq returned invalid JSON (attempt %d)", attempt + 1)
            if completion.choices[0].finish_reason == "length":
                # The same prompt would be cut off again; don't spend another call on it.
                log.warning("output hit max_completion_tokens=%d; not retrying", MAX_COMPLETION_TOKENS)
                break

    model_ms = int((time.perf_counter() - started) * 1000)
    if not isinstance(data, dict):
        return ReframeResponse(reply="Something went wrong. Please try again.", operations=[], model_ms=model_ms)

    ops, dropped = _validate_ops(data.get("operations", []), req.snapshot)
    reply = str(data.get("reply") or "").strip()[:600] or ("Done." if ops else "I couldn't change this page.")
    log.info("reframe model=%s ms=%d ops=%d dropped=%d", used_model, model_ms, len(ops), dropped)
    log.info("reframe url=%s msg=%r reply=%r ops=%s", req.snapshot.url[:80], req.message[:80], reply[:120],
             [o.model_dump(exclude_none=True) | ({"html": o.html[:160]} if isinstance(o, RenderOp) else {}) for o in ops])
    return ReframeResponse(
        reply=reply, operations=ops, dropped=dropped, model_ms=model_ms, done=bool(data.get("done"))
    )
