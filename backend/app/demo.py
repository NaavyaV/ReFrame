"""Scripted walkthrough for the BrightGrid demo site, so demos are fast and identical every time.

Only applies to the demo hosts below; every other site goes through the model.
"""

import asyncio
import random
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .schemas import ReframeRequest, ReframeResponse, RenderOp, SnapElement

if TYPE_CHECKING:
    from .tracker import Tracker

DEMO_HOSTS = {"127.0.0.1:5173", "localhost:5173", "reframe-demo.pages.dev"}
DEMO_HOST_SUFFIX = ".reframe-demo.pages.dev"

CSS = """
.rf-demo{background:#fff;border:1px solid #d5dbe1;border-top:8px solid #0b4f8a;border-radius:18px;padding:40px;
  font-family:Arial,Helvetica,sans-serif;color:#1d2a36;box-shadow:0 10px 32px rgba(11,79,138,.14)}
.rf-kicker{font-size:18px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;color:#b07a00;margin:0 0 10px}
.rf-demo h2{font-size:38px;line-height:1.2;color:#0b4f8a;margin:0 0 14px}
.rf-demo p{font-size:24px;line-height:1.55;margin:0 0 26px}
.rf-facts{display:flex;gap:16px;margin:0 0 30px}
.rf-fact{flex:1;background:#f3f7fb;border:1px solid #d5dbe1;border-radius:14px;padding:20px 22px}
.rf-fact span{display:block;font-size:20px;color:#5a6b7b;margin-bottom:6px}
.rf-fact b{font-size:34px;color:#0b4f8a}
.rf-big{display:block;width:100%;font-size:28px;font-weight:bold;padding:26px 28px;border:0;border-radius:14px;
  background:#0b4f8a;color:#fff;text-align:center;cursor:pointer;box-shadow:0 6px 0 #073a66}
.rf-big:hover{background:#0d5ea3}
.rf-big:active{box-shadow:none}
.rf-big:focus-visible{outline:4px solid #f2a900;outline-offset:4px}
.rf-pay{background:#1e7a46;box-shadow:0 6px 0 #145732}
.rf-pay:hover{background:#228c50}
.rf-demo input[aria-invalid="true"]{border:3px solid #b3261e;background:#fff5f4;outline-color:#b3261e}
.rf-hint{display:block;font-size:18px;font-weight:normal;color:#5a6b7b;margin-top:2px}
.rf-demo label{display:block;font-size:22px;font-weight:bold;margin:0 0 8px}
.rf-demo input{display:block;width:100%;font-size:28px;padding:16px 18px;border:2px solid #8a99a8;border-radius:12px;
  margin:0 0 24px;color:#1d2a36;background:#fff}
.rf-demo input:focus{border-color:#0b4f8a;outline:4px solid #f2a900}
.rf-two{display:flex;gap:16px}
.rf-two>div{flex:1}
.rf-check{width:110px;height:110px;border-radius:50%;background:#1f8a4c;color:#fff;font-size:64px;font-weight:bold;
  display:flex;align-items:center;justify-content:center;margin:0 auto 22px}
.rf-center{text-align:center}
.rf-note{font-size:20px;color:#5a6b7b}
"""


def is_demo(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host in DEMO_HOSTS or host.endswith(DEMO_HOST_SUFFIX)


def _page(url: str) -> str:
    """'/billing.html', '/billing' (Cloudflare Pages pretty URLs) and '/' map to 'billing' / 'index'."""
    name = urlsplit(url).path.rsplit("/", 1)[-1].removesuffix(".html")
    return name or "index"


def _link(elements: list[SnapElement], page: str, text: str | None = None) -> SnapElement | None:
    links = [e for e in elements if e.kind == "control" and e.href and _page(e.href) == page]
    if text:
        links = [e for e in links if text.lower() in (e.text or "").lower()] or links
    return links[0] if links else None


def _field(elements: list[SnapElement], label: str) -> SnapElement | None:
    return next((e for e in elements if e.kind == "control" and label.lower() in (e.label or "").lower()), None)


def _button(elements: list[SnapElement], text: str) -> SnapElement | None:
    return next((e for e in elements if e.kind == "control" and text.lower() in (e.text or "").lower()), None)


def _render(html: str) -> RenderOp:
    return RenderOp(op="render", html=html, css=CSS, isolate=True)


def _step(req: ReframeRequest) -> ReframeResponse | None:
    s = req.snapshot
    els = s.elements
    page = _page(s.url)
    page_text = " ".join(e.text or "" for e in els)

    if page == "confirmation":
        confirmation = (re.search(r"BG-\d+", page_text) or ["BG-20491"])[0]
        home = _link(els, "index")
        if not home:
            return None
        return ReframeResponse(
            reply=f"I found your receipt under all the ads — confirmation {confirmation}. You're good to go, nothing else to do!",
            operations=[_render(
                '<div class="rf-demo rf-center"><div class="rf-check">✓</div>'
                "<h2>Payment submitted!</h2>"
                f"<p>You paid <b>$84.12</b>. Your confirmation number is <b>{confirmation}</b>.</p>"
                '<p class="rf-note">You\'re all set. There is nothing else you need to do.</p>'
                f'<button class="rf-big" data-rf-target="{home.ref}">Back to home</button></div>'
            )],
            done=True,
        )

    if page == "pay":
        card, exp, cvv = _field(els, "card number"), _field(els, "expiration"), _field(els, "security code")
        submit = _button(els, "submit payment")
        if not (card and exp and cvv and submit):
            return None
        return ReframeResponse(
            reply="I put the payment form in one place with big, easy boxes. Now type your card details and press \"Submit payment\".",
            operations=[_render(
                '<div class="rf-demo"><p class="rf-kicker">Step 3 of 3</p><h2>Enter your card</h2>'
                "<p>You are paying <b>$84.12</b> to BrightGrid.</p>"
                f'<label for="card">Card number<span class="rf-hint">16 digits</span></label>'
                f'<input id="card" data-rf-bind="{card.ref}" data-rf-format="card" placeholder="1234 5678 9012 3456">'
                '<div class="rf-two">'
                f'<div><label for="exp">Expiration<span class="rf-hint">MM/YY</span></label>'
                f'<input id="exp" data-rf-bind="{exp.ref}" data-rf-format="exp" placeholder="MM/YY"></div>'
                f'<div><label for="cvv">Security code<span class="rf-hint">3 digits on the back</span></label>'
                f'<input id="cvv" data-rf-bind="{cvv.ref}" data-rf-format="cvv" placeholder="123"></div>'
                "</div>"
                f'<button class="rf-big rf-pay" data-rf-target="{submit.ref}" data-rf-primary="1">Submit payment</button></div>'
            )],
        )

    if page == "billing":
        pay = _link(els, "pay")
        if not pay:
            return None
        return ReframeResponse(
            reply="I closed the survey pop-up and kept just your bill. Now press \"Make a one-time payment\".",
            operations=[_render(
                '<div class="rf-demo"><p class="rf-kicker">Step 2 of 3 · Account Center</p><h2>Pay your bill</h2>'
                '<div class="rf-facts"><div class="rf-fact"><span>Amount due</span><b>$84.12</b></div>'
                '<div class="rf-fact"><span>Due date</span><b>Nov 3, 2026</b></div></div>'
                f'<button class="rf-big" data-rf-target="{pay.ref}" data-rf-primary="1">Make a one-time payment</button></div>'
            )],
        )

    if page == "index":
        billing = _link(els, "billing", "payment")
        if not billing:
            return None
        return ReframeResponse(
            reply="I cleared away the clutter and kept only what you need. Now press \"Check payment options\".",
            operations=[_render(
                '<div class="rf-demo"><p class="rf-kicker">Step 1 of 3 · Welcome back, Margaret</p>'
                "<h2>Your bill is $84.12</h2>"
                "<p>It is due on <b>November 3</b>. When you're ready, press the big button below.</p>"
                f'<button class="rf-big" data-rf-target="{billing.ref}" data-rf-primary="1">Check payment options</button></div>'
            )],
        )
    return None


# What the tracker shows while each step "works": (kind, target, detail). Alerts are ("alert", severity, title, detail).
TIMELINES: dict[str, list[tuple[str, str, str, str]]] = {
    "index": [
        ("scan", "Home page", "212 elements, 9 pop-ups and banners", "Reading the page"),
        ("remove", "Cookie banner", "covered the bottom of the screen", "Removing clutter"),
        ("remove", "Maintenance alert strip", "moved to the payment step", "Removing clutter"),
        ("remove", "Promo carousel", "3 rotating ads", "Removing clutter"),
        ("alert", "info", "Bill due in 10 days", "$84.12 due Nov 3, 2026"),
        ("remove", "Ways to save, outage map, events, FAQ", "4 sections", "Removing clutter"),
        ("remove", "Footer", "25 links", "Removing clutter"),
        ("keep", "Amount due", "$84.12", "Rebuilding the layout"),
        ("change", "Pay link", "13px link → large \"Check payment options\" button", "Rebuilding the layout"),
        ("change", "Text size", "13px → 24px", "Rebuilding the layout"),
    ],
    "billing": [
        ("scan", "Account Center", "64 elements, 1 pop-up", "Reading the page"),
        ("remove", "Survey pop-up", "was blocking the page", "Removing clutter"),
        ("alert", "info", "Pop-up closed", "\"How are we doing?\" survey blocked the payment buttons"),
        ("remove", "Sidebar", "6 account links", "Removing clutter"),
        ("remove", "Recent activity table", "not needed to pay", "Removing clutter"),
        ("remove", "AutoPay promo", "advert", "Removing clutter"),
        ("keep", "Balance and due date", "$84.12 · Nov 3, 2026", "Rebuilding the layout"),
        ("change", "One-time payment tile", "small tile → large button", "Rebuilding the layout"),
    ],
    "pay": [
        ("scan", "Payment form", "4 fields, 2 sidebars", "Reading the page"),
        ("alert", "info", "Card fields found", "Values stay on this device; only labels are tracked"),
        ("remove", "Sidebars", "payment locations, promo", "Removing clutter"),
        ("alert", "warning", "Maintenance Sat 11/1, 1–5 AM", "Online payments may be delayed during that window"),
        ("remove", "Maintenance strip", "flagged as an alert", "Removing clutter"),
        ("keep", "Card number, expiration, security code", "bound to the real form", "Rebuilding the layout"),
        ("change", "Inputs", "11px → 28px, card formats enforced", "Rebuilding the layout"),
        ("change", "Submit payment", "small link-style button → large green button", "Rebuilding the layout"),
    ],
    "confirmation": [
        ("scan", "Payment status page", "41 elements, 1 pop-up, 3 ads", "Reading the page"),
        ("remove", "Feedback pop-up", "\"How easy was it to pay today?\"", "Removing clutter"),
        ("remove", "Upsell tiles", "7 offers", "Removing clutter"),
        ("alert", "warning", "Card convenience fee may apply", "Fine print: tariff schedule 14-B"),
        ("keep", "Confirmation number", "found in 11px fine print", "Rebuilding the layout"),
        ("change", "Receipt", "fine print → large confirmation", "Rebuilding the layout"),
    ],
}


async def demo_reframe(req: ReframeRequest, tracker: "Tracker | None" = None) -> ReframeResponse | None:
    result = _step(req)
    if result is None:
        return None
    total = random.uniform(1.5, 2.5)
    timeline = TIMELINES.get(_page(req.snapshot.url), [])
    if tracker is None or not timeline:
        await asyncio.sleep(total)
        return result
    tracker.start()
    gap = total / (len(timeline) + 1)
    for i, (kind, a, b, c) in enumerate(timeline, 1):
        await asyncio.sleep(gap)
        if kind == "alert":
            tracker.alert(a, b, c)
        else:
            tracker.event(kind, a, b, c, int(90 * i / len(timeline)))
    await asyncio.sleep(gap)
    tracker.finish("done", result.reply)
    await tracker.flush()
    return result
