"""What the orchestrator says to people.

Plain, calm and specific. Every product reply says the price was seen at a moment
in time, gives the link, and makes clear nothing was bought. Caregiver settings
only change length, wording and which facts are pointed out; never the product.
"""

from __future__ import annotations

from shared import llm
from shared.features import label
from shared.format import money
from shared.models import APPROVED_SITES, ProductSearchTask, RecommendationResponse, SiteResult, SiteStep
from shared.preferences import LANGUAGE_NAMES


HELP = (
    "I can help you find something to buy online, or work through a problem with a phone, computer or website.\n\n"
    "For shopping, tell me what you need and your budget, for example: \"Find a lightweight vacuum that's easy to "
    "use, under $100.\" I'll look at real listings on {site} and suggest one, with its price and link. "
    "I never buy anything; you decide.\n\n"
    "For a bill, send me the website and I'll walk you through paying it, or send you a link to pay.\n\n"
    "Say \"start over\" at any time to begin again."
)


def _site(task: ProductSearchTask) -> str:
    return APPROVED_SITES.get(task.source_site, task.source_site)


def help_text() -> str:
    return HELP.format(site=" or ".join(APPROVED_SITES.values()))


def describe_search(task: ProductSearchTask) -> str:
    hard, soft = task.hard_constraints, task.soft_preferences
    text = f"a {hard.category}"
    if hard.min_price is not None and hard.max_price is not None:
        text += f" between {money(hard.min_price, hard.currency)} and {money(hard.max_price, hard.currency)}"
    elif hard.max_price is not None:
        text += f" under {money(hard.max_price, hard.currency)}"
    features = [label(key) for key in [*hard.required_features, *soft.features]]
    if features:
        text += " that's " + (", ".join(features[:-1]) + " and " + features[-1] if len(features) > 1 else features[0])
    return text


def searching(task: ProductSearchTask, short: bool) -> str:
    if short:
        return f"Looking on {_site(task)} for {describe_search(task)}. This takes a minute or two."
    return (f"Thanks. I'm looking on {_site(task)} for {describe_search(task)}. This usually takes a minute or two, "
            "and I won't buy anything.")


def confirm_search(task: ProductSearchTask) -> str:
    return f"Just to check: shall I look on {_site(task)} for {describe_search(task)}? Please reply yes or no."


def product(rec: RecommendationResponse, task: ProductSearchTask, *, short: bool, highlight: bool) -> str:
    candidate = rec.selected_candidate
    assert candidate is not None
    lines = ["I found one that fits." if not short else "Here's one that fits:", ""]
    name = f"**{candidate.title}**"
    if candidate.supplier:
        name += f"\nBrand: {candidate.supplier}"
    lines.append(name)
    if candidate.price is not None:
        lines.append(f"Price when I looked: {money(candidate.price, candidate.currency)}")
    else:
        lines.append("Price: not shown on the listing")
    lines.append(f"Why this one: {rec.rationale}")
    if highlight and candidate.features:
        lines.append("")
        lines.append("What the listing says:")
        for feature in candidate.features[:3]:
            lines.append(f"- {feature[:140]}")
    lines.append("")
    lines.append(f"Link: {candidate.url}")
    lines.append("")
    if short:
        lines.append("I haven't bought anything. Prices can change, so check the listing first.")
    else:
        extra = [caveat for caveat in rec.caveats[1:3]]
        if extra:
            lines.append(" ".join(extra))
        lines.append("I haven't bought anything. Prices and stock can change, so please check the listing before "
                     "you decide. If this isn't right, say \"show me another\" or tell me what to change.")
    return "\n".join(lines)


def no_match(rec: RecommendationResponse, task: ProductSearchTask, *, short: bool) -> str:
    reason = rec.rationale.removeprefix("I couldn't find a good match: ").rstrip(".")
    if short:
        return f"I couldn't find {describe_search(task)} that fits. Would you like to change the budget or features?"
    return (f"I couldn't find {describe_search(task)} that fits everything you asked for: {reason}.\n\n"
            "Would you like me to try again with a different budget, or fewer must-haves?")


FAILURE_TEXT = {
    "integration_unavailable": "Product search isn't switched on yet, so I can't look up listings right now. "
                               "The person who set up Hold My Hand needs to add the Apify connection.",
    "unauthorized": "Product search isn't working because its account key was refused. The person who set up "
                    "Hold My Hand needs to check it.",
    "budget_exceeded": "I've reached the limit for product searches for now. Please try again later.",
    "site_not_allowed": "I can only search approved shops at the moment.",
    "actor_not_found": "Product search isn't set up correctly. The person who set up Hold My Hand needs to check it.",
}


def failed(code: str) -> str:
    return FAILURE_TEXT.get(code, "The shop search isn't responding right now. Please try again in a few minutes.")


def other_shop(name: str) -> str:
    sites = " or ".join(APPROVED_SITES.values())
    return f"I can't search {name} yet, only {sites}. I'll look there instead; tell me if you'd rather not."


def _click(step: SiteStep) -> str:
    where = step.where if step.where and step.where != "the page" else ""
    if where.startswith("the page, "):
        where = where.removeprefix("the page, ")
    if not where:
        return f"click “{step.click}”"
    return f"look in {where}{',' if 'under' in where else ''} and click “{step.click}”"


def bill_steps(result: SiteResult) -> str:
    """Click-by-click directions to the payment page, from what the scraper actually saw."""
    due = f" {money(result.amount, result.currency)}" if result.amount is not None else ""
    lines = [f"Here's how to pay your{due} bill on the site:", ""]
    for number, step in enumerate(result.steps, start=1):
        parts = []
        if step.popup:
            parts.append(f"If a “{step.popup.title}” pop-up appears, click “{step.popup.dismiss}” to close it.")
        if step.click:
            action = _click(step)
            parts.append(f"{'Then ' + action if parts else action[0].upper() + action[1:]}.")
            lines.append(f"{number}. On the “{step.page}” page: {' '.join(parts)}")
            continue
        if result.pay_page:
            fields = [name for name in result.pay_fields if "amount" not in name.lower()]
            if result.amount is not None and any("amount" in name.lower() for name in result.pay_fields):
                parts.append(f"Check that the payment amount says {money(result.amount, result.currency)}.")
            if fields:
                quoted = [f"“{name}”" for name in fields]
                listed = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]
                parts.append(f"Fill in {listed}.")
            parts.append(f"Then click “{result.pay_button}”.")
        else:
            parts.append("Look for a button to pay. It may ask you to sign in first.")
        lines.append(f"{number}. On the “{step.page}” page: {' '.join(parts)}")
    lines += ["", "If you get stuck, just tell me and I'll send you a link to pay instead."]
    return "\n".join(lines)


def bill_link(result: SiteResult, url: str, *, stuck: bool) -> str:
    due = money(result.amount, result.currency)
    if stuck:
        return f"No worries! I found your payment. You can pay your {due} bill here:\n{url}"
    return f"Here's a link to pay your {due} bill:\n{url}"


def caregiver_notice(changes: list[str]) -> str:
    return (f"Note: your caregiver changed a setting ({'; '.join(changes)}). Say \"caregiver settings\" to see "
            "your settings or \"undo caregiver changes\" to undo them.")


async def translate(text: str, language: str) -> str:
    """Translate a reply, keeping links, prices and product names exactly. English if anything goes wrong."""
    if language == "en" or not llm.configured():
        return text
    name = LANGUAGE_NAMES.get(language, language)
    try:
        translated = await llm.complete(
            [
                {"role": "system", "content": (
                    f"Translate the message into {name} for an older adult. Keep every URL, price, number, "
                    "product name and brand exactly as written. Keep the line breaks. Reply with the translation only."
                )},
                {"role": "user", "content": text},
            ],
            max_tokens=1200,
            temperature=0.1,
        )
    except llm.LLMError:
        return text
    return translated if translated and llm.grounded(translated, text) else text
