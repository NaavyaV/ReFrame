"""Latency benchmark: uv run python tests/bench_models.py"""

import asyncio
import time

from app import reframer
from app.config import get_settings
from app.schemas import ReframeRequest

SNAPSHOT = {
    "url": "http://127.0.0.1:5173/billing.html",
    "title": "BrightGrid Energy | Account Center",
    "headings": ["Account Center", "Recent activity", "How are we doing?"],
    "has_dialog": True,
    "theme": {
        "font_family": "Arial, sans-serif", "font_size": "13px", "text_color": "rgb(68, 68, 68)",
        "background": "rgb(238, 241, 244)", "link_color": "rgb(11, 79, 138)",
        "button": {"cls": "small-btn", "background": "rgb(11, 79, 138)", "color": "rgb(255, 255, 255)", "border_radius": "2px"},
        "header": {"background": "rgb(255, 255, 255)", "border_bottom": "3px solid rgb(242, 169, 0)"},
        "card": {"background": "rgb(255, 255, 255)", "border": "1px solid rgb(213, 219, 225)"},
    },
    "elements": [
        {"ref": "r1", "kind": "control", "tag": "button", "role": "button", "text": "Start survey", "cls": "small-btn", "region": "dialog"},
        {"ref": "r2", "kind": "control", "tag": "button", "role": "button", "text": "No thanks", "cls": "small-btn", "region": "dialog"},
        {"ref": "r3", "kind": "control", "tag": "a", "role": "link", "text": "Home", "href": "/index.html", "region": "nav"},
        {"ref": "r4", "kind": "control", "tag": "a", "role": "link", "text": "Save Energy", "href": "#", "region": "nav"},
        {"ref": "r5", "kind": "control", "tag": "a", "role": "link", "text": "Outages", "href": "#", "region": "nav"},
        {"ref": "r6", "kind": "control", "tag": "a", "role": "link", "text": "Profile & contact", "href": "#", "region": "aside"},
        {"ref": "r7", "kind": "control", "tag": "a", "role": "link", "text": "Paperless settings", "href": "#", "region": "aside"},
        {"ref": "r8", "kind": "control", "tag": "a", "role": "link", "text": "AutoPay enrollment", "href": "#", "region": "aside"},
        {"ref": "r9", "kind": "control", "tag": "a", "role": "link", "text": "Documents & History", "href": "/statements.html", "region": "aside"},
        {"ref": "r10", "kind": "control", "tag": "a", "role": "link", "text": "Make a one-time payment", "href": "/pay.html", "region": "main"},
        {"ref": "r11", "kind": "control", "tag": "a", "role": "link", "text": "Payment arrangements", "href": "#", "region": "main"},
        {"ref": "r12", "kind": "block", "tag": "header", "text": "BrightGrid Home Save Energy Outages Help", "cls": ""},
        {"ref": "r13", "kind": "block", "tag": "main", "text": "Account Center Balance $84.12 Due date Nov 3, 2026", "region": "main"},
        {"ref": "r14", "kind": "block", "tag": "aside", "text": "[promo] Enroll in AutoPay and never miss a due date!", "cls": "promo"},
    ],
}

MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]


async def main() -> None:
    s = get_settings()
    req = ReframeRequest(message="I want to download my electricity bill", snapshot=SNAPSHOT, continuation=True)
    for model in MODELS:
        s.groq_fast_model = model
        for _ in range(2):
            t = time.perf_counter()
            try:
                res = await reframer.reframe(req)
                ops = [o.op for o in res.operations]
                html = next((o.html for o in res.operations if o.op == "render"), "")
                print(f"{model:24s} {int((time.perf_counter()-t)*1000):5d}ms ops={ops} html={len(html)}b")
            except Exception as e:
                print(model, "ERR", str(e)[:120])
    print("\nSample html:\n", html[:1200])


if __name__ == "__main__":
    asyncio.run(main())
