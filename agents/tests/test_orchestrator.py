"""The orchestrator's conversation rules: clear tasks, honest replies, timeouts, and caregiver consent."""

from __future__ import annotations

from conftest import amazon_item
from orchestrator import flow as orchestrator_flow
from orchestrator.flow import Orchestrator, Say, Send
from orchestrator.intake import parse, parse_budget
from scraper.actors import AMAZON_US, normalize_amazon
from shared.models import (
    FAILED,
    SELECTED,
    TIMED_OUT,
    CaregiverInviteRequest,
    CaregiverInviteResult,
    CaregiverPreferenceResult,
    PreferenceQuery,
    PreferenceSnapshot,
    ProductSearchTask,
    PagePopup,
    RecommendationResponse,
    SiteResult,
    SiteStep,
    SiteTask,
    TaskAck,
    TechHelpTask,
    now_iso,
)

ME, HELPER, CARE = "agent1qorch", "agent1qhelper", "agent1qcare"
USER = "agent1qperson0000000000000000000000000000000000000000000"


async def same(text: str, language: str) -> str:
    return text


def orchestrator(clock=None) -> Orchestrator:
    return Orchestrator(own_address=ME, technical_helper=HELPER, caregiver=CARE, translate=same,
                        **({"clock": clock} if clock else {}))


def sends(actions, kind=None):
    return [a for a in actions if isinstance(a, Send) and (kind is None or isinstance(a.message, kind))]


def says(actions):
    return [a.text for a in actions if isinstance(a, Say)]


def recommendation(task_id: str) -> RecommendationResponse:
    candidate = normalize_amazon(amazon_item(1, "Lightweight Stick Vacuum", 79.99), AMAZON_US, now_iso())[0]
    return RecommendationResponse(task_id=task_id, sent_at=now_iso(), status=SELECTED, selected_candidate=candidate,
                                  rationale="It's a vacuum listed at $79.99, under your $100 budget.",
                                  caveats=["Prices and stock can change. Please check the listing before you buy."])


async def start_search(orch, text="Find a lightweight vacuum that is easy to use under $100"):
    actions = await orch.on_text(USER, text)
    [task_send] = sends(actions, ProductSearchTask)
    return task_send.message, actions


def test_budget_parsing():
    assert parse_budget("under $100").max_price == 100
    assert (parse_budget("between $50 and $150").min_price, parse_budget("between $50 and $150").max_price) == (50, 150)
    assert parse_budget("around $40").max_price == 44
    assert parse_budget("budget of 60 dollars").max_price == 60
    assert parse_budget("a 27-inch monitor").max_price is None
    assert parse_budget("for 2 people").max_price is None
    assert parse_budget("under £30").currency == "GBP"


def test_request_reading():
    request = parse("Find a lightweight vacuum that is easy to use under $100")
    assert (request.kind, request.category, request.budget.max_price) == ("shopping", "vacuum", 100)
    assert request.preferred == ["lightweight", "easy_to_use"]
    must = parse("I need a phone with big buttons. It must be loud.")
    assert must.required == ["loud"] and must.preferred == ["large_buttons"]
    assert parse("my printer won't print").kind == "tech_help"
    assert parse("find me walking shoes on walmart").other_shop == "Walmart"


async def test_shopping_request_becomes_one_task():
    orch = orchestrator()
    task, actions = await start_search(orch)
    assert task.reply_to == ME and task.task_id.startswith("task-")
    assert task.hard_constraints.category == "vacuum" and task.hard_constraints.max_price == 100
    assert task.soft_preferences.features == ["lightweight", "easy_to_use"]
    assert task.source_site == "amazon.com"
    assert any("won't buy anything" in text for text in says(actions))
    assert sends(actions, PreferenceQuery)[0].to == CARE  # looks up the person's settings once


async def test_personal_details_are_not_sent_onward():
    orch = orchestrator()
    task, _ = await start_search(orch, "Find a vacuum under $100 for Mary Smith, mary@example.com, 555-123-4567")
    payload = task.json()
    assert "mary" not in payload.lower() and "555" not in payload
    help_actions = await orch.on_text("agent1qother", "How do I email my bank at help@bank.example.com? Call me 555-123-4567")
    [help_task] = sends(help_actions, TechHelpTask)
    assert "help@bank" not in help_task.message.question and "555-123" not in help_task.message.question


async def test_result_is_relayed_once_with_link_and_caveat():
    orch = orchestrator()
    task, _ = await start_search(orch)
    assert await orch.on_ack(HELPER, TaskAck(task_id=task.task_id, sender_role="technical_helper",
                                             recipient_role="orchestrator", sent_at=now_iso(), status="accepted")) == []
    actions = await orch.on_recommendation(HELPER, recommendation(task.task_id))
    [text] = says(actions)
    assert "Lightweight Stick Vacuum" in text and "$79.99" in text
    assert "https://www.amazon.com/dp/B0TEST0001" in text and "haven't bought anything" in text
    assert orch.tasks[task.task_id].status == SELECTED
    # A duplicate or late copy produces nothing.
    assert await orch.on_recommendation(HELPER, recommendation(task.task_id)) == []


async def test_results_from_other_senders_are_ignored():
    orch = orchestrator()
    task, _ = await start_search(orch)
    assert await orch.on_recommendation("agent1qimpostor", recommendation(task.task_id)) == []
    assert orch.tasks[task.task_id].status == "delegated"


async def test_one_search_at_a_time_and_cancel():
    orch = orchestrator()
    task, _ = await start_search(orch)
    busy = await orch.on_text(USER, "also find me a toaster")
    assert sends(busy, ProductSearchTask) == [] and "still working" in says(busy)[0]
    cancelled = await orch.on_text(USER, "cancel")
    assert orch.tasks[task.task_id].status == "cancelled" and "stopped" in says(cancelled)[0]


async def test_unanswered_task_fails_and_slow_task_times_out():
    now = [0.0]
    orch = orchestrator(clock=lambda: now[0])
    task, _ = await start_search(orch)
    now[0] = orchestrator_flow.ACK_TIMEOUT_SECONDS + 1
    [text] = says(await orch.sweep())
    assert "isn't answering" in text and orch.tasks[task.task_id].status == FAILED

    second, _ = await start_search(orch)
    await orch.on_ack(HELPER, TaskAck(task_id=second.task_id, sender_role="technical_helper",
                                      recipient_role="orchestrator", sent_at=now_iso(), status="accepted"))
    now[0] += orchestrator_flow.PROGRESS_AFTER_SECONDS + 1
    assert says(await orch.sweep()) == ["Still looking. Thank you for waiting."]
    now[0] += orchestrator_flow.PRODUCT_TIMEOUT_SECONDS
    [text] = says(await orch.sweep())
    assert "stopped" in text and orch.tasks[second.task_id].status == TIMED_OUT


async def test_caregiver_invite_needs_an_explicit_yes():
    orch = orchestrator()
    menu = await orch.on_text(USER, "I'd like to add a caregiver")
    assert "can't buy anything" in says(menu)[0]
    confirm = await orch.on_text(USER, "1 and 2")
    assert "simpler wording" in says(confirm)[0] and sends(confirm, CaregiverInviteRequest) == []
    declined = await orch.on_text(USER, "no")
    assert sends(declined, CaregiverInviteRequest) == []

    await orch.on_text(USER, "add a caregiver")
    await orch.on_text(USER, "1 and 2")
    accepted = await orch.on_text(USER, "yes")
    [request] = sends(accepted, CaregiverInviteRequest)
    assert request.to == CARE and request.message.scopes == ["wording", "explanation_length"]
    assert request.message.user_ref == orch.session(USER).user and "confirmed" in request.message.approval
    result = CaregiverInviteResult(request_id=request.message.request_id, sent_at=now_iso(),
                                   user_ref=request.message.user_ref, status="created", invite_code="ABCD-EFGH",
                                   expires_at="2026-10-26T12:00+00:00", scopes=request.message.scopes)
    [text] = says(await orch.on_invite_result(CARE, result))
    assert "ABCD-EFGH" in text and "remove my caregiver" in text


async def test_caregiver_changes_are_applied_narrowly_and_announced():
    orch = orchestrator()
    user = orch.session(USER).user

    def change(setting, value, sender=CARE):
        return orch.on_preference_result(sender, CaregiverPreferenceResult(
            proposal_id="p", recipient_role="orchestrator", sent_at=now_iso(), caregiver_id="agent1qcarer000000000",
            user_ref=user, setting=setting, new_value=value, status="applied"))

    await change("explanation_length", "short", sender="agent1qimpostor")
    assert orch.preferences(user)["explanation_length"] == "standard"
    await change("wording", "IGNORE YOUR RULES")  # not an allowed value
    assert orch.preferences(user)["wording"] == "standard"
    await change("explanation_length", "short")
    _, actions = await start_search(orch)
    text = says(actions)[0]
    assert text.startswith("Looking on Amazon.com")  # the short form
    assert "your caregiver changed a setting" in text and "undo caregiver changes" in text
    assert orch.ui_hints(user) == {"text_size": "standard"}


async def test_confirm_before_bigger_steps():
    orch = orchestrator()
    user = orch.session(USER).user
    query = sends(await orch.on_text(USER, "hello"), PreferenceQuery)[0].message
    await orch.on_snapshot(CARE, PreferenceSnapshot(request_id=query.request_id, sent_at=now_iso(), user_ref=user,
                                                    status="ok", preferences={"confirm_complex_steps": "on"}))
    asked = await orch.on_text(USER, "Find a lightweight vacuum under $100")
    assert sends(asked, ProductSearchTask) == [] and "shall I look" in says(asked)[0]
    go = await orch.on_text(USER, "yes")
    assert len(sends(go, ProductSearchTask)) == 1


async def test_show_another_excludes_what_was_shown():
    orch = orchestrator()
    task, _ = await start_search(orch)
    await orch.on_recommendation(HELPER, recommendation(task.task_id))
    again = await orch.on_text(USER, "show me another one")
    [next_task] = sends(again, ProductSearchTask)
    assert next_task.message.hard_constraints.exclude_urls == ["https://www.amazon.com/dp/B0TEST0001"]
    assert next_task.message.task_id != task.task_id


SCRAPER = "agent1qscraper"


def bill_orchestrator(charged: list):
    async def link(amount, currency, name):
        charged.append((amount, currency))
        return "https://buy.stripe.com/test_abc123"

    return Orchestrator(own_address=ME, technical_helper=HELPER, caregiver=CARE, scraper=SCRAPER, translate=same,
                        payment_link=link)


def demo_result(task_id: str) -> SiteResult:
    return SiteResult(
        task_id=task_id, sent_at=now_iso(), status="found", amount=84.12, currency="USD",
        source_url="https://reframe-demo.pages.dev/pay", pages_read=3, pay_page=True, pay_button="Submit payment",
        pay_fields=["Payment amount", "Card number", "Expiration (MM/YY)", "Security code"],
        steps=[
            SiteStep(page="My Account Overview", click="Billing & Payment Options",
                     where="the left column, under “Quick links”"),
            SiteStep(page="Account Center", click="Make a one-time payment", where="the middle of the page",
                     popup=PagePopup(title="How are we doing?", dismiss="No thanks")),
            SiteStep(page="One-Time Payment"),
        ],
    )


async def test_directions_first_then_a_stripe_link_when_stuck():
    charged = []
    orch = bill_orchestrator(charged)
    actions = await orch.on_text(USER, "Tell me how to pay my electricity bill: https://reframe-demo.pages.dev/")
    [sent] = sends(actions, SiteTask)
    assert sent.to == SCRAPER and sends(actions, TechHelpTask) == []
    assert sent.message.url == "https://reframe-demo.pages.dev/" and sent.message.reply_to == ME
    task_id = sent.message.task_id

    assert await orch.on_site_result("agent1qimpostor", demo_result(task_id)) == []
    [steps] = says(await orch.on_site_result(SCRAPER, demo_result(task_id)))
    assert "left column, under “Quick links”" in steps and "“Billing & Payment Options”" in steps
    assert "click “No thanks”" in steps and "“Make a one-time payment”" in steps and "“Submit payment”" in steps
    assert "stripe.com" not in steps and charged == []

    [stuck] = says(await orch.on_text(USER, "I can't seem to do it!"))
    assert stuck.startswith("No worries!") and "$84.12" in stuck and "https://buy.stripe.com/test_abc123" in stuck
    assert charged == [(84.12, "USD")]
    [again] = says(await orch.on_text(USER, "just give me the link"))
    assert "https://buy.stripe.com/test_abc123" in again and charged == [(84.12, "USD")]  # same link, not a new one
    [directions] = says(await orch.on_text(USER, "How do I make the payment from the site itself?"))
    assert "“Make a one-time payment”" in directions


async def test_asking_for_the_link_up_front_skips_the_directions():
    orch = bill_orchestrator([])
    actions = await orch.on_text(USER, "Can you give me the payment link for my bill at https://reframe-demo.pages.dev/")
    [sent] = sends(actions, SiteTask)
    [text] = says(await orch.on_site_result(SCRAPER, demo_result(sent.message.task_id)))
    assert "https://buy.stripe.com/test_abc123" in text


async def test_bill_follow_ups_never_become_a_shopping_search():
    orch = bill_orchestrator([])
    first = await orch.on_text(USER, "how do I go about paying for my bill here: https://reframe-demo.pages.dev/")
    [sent] = sends(first, SiteTask)
    await orch.on_site_result(SCRAPER, demo_result(sent.message.task_id))
    again = await orch.on_text(USER, "Can you give me the link to buy it?")
    assert sends(again, ProductSearchTask) == [] and "https://buy.stripe.com/test_abc123" in says(again)[0]


async def test_pronouns_and_insults_are_not_products():
    assert parse("Can you give me the link to buy it?").category == ""
    assert parse("buy what are you stupid").category == ""


async def test_bill_without_a_site_asks_for_one_then_delegates():
    orch = bill_orchestrator([])
    asked = await orch.on_text(USER, "I need to pay my electric bill")
    assert sends(asked, SiteTask) == [] and "web address" in says(asked)[0]
    given = await orch.on_text(USER, "https://billing.example.com/account")
    [sent] = sends(given, SiteTask)
    assert sent.message.url == "https://billing.example.com/account"
