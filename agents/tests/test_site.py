"""Website tasks: the scraper and the technical helper go back and forth until they reach the payment page."""

from __future__ import annotations

from scraper import site
from scraper.page import read_html
from scraper.site import SiteJobs
from shared.models import PageCheck, PageLink, PagePopup, PageResponse, SiteResult, SiteTask, TaskAck, now_iso
from technical_helper.flow import PageReview, find_amount

ORCH, HELPER, SCRAPER = "agent1qorch", "agent1qhelper", "agent1qscraper"
ROOT = "https://billing.example.com"

PAGES = {
    f"{ROOT}/account": PageResponse(
        task_id="", request_id="", sent_at="", status="ok", url=f"{ROOT}/account", title="Energy Co | Overview",
        text="Welcome back. Current balance $84.12 Due Nov 3",
        links=[PageLink(label="Account Center", url=f"{ROOT}/billing", where="the menu at the top"),
               PageLink(label="Billing & Payment Options", url=f"{ROOT}/billing",
                        where="the left column, under “Quick links”"),
               PageLink(label="News", url="https://other.example.net/news", where="the middle of the page")],
    ),
    f"{ROOT}/billing": PageResponse(
        task_id="", request_id="", sent_at="", status="ok", url=f"{ROOT}/billing", title="Energy Co | Account Center",
        text="Account Center Balance $84.12",
        links=[PageLink(label="Make a one-time payment", url=f"{ROOT}/pay", where="the middle of the page")],
        popups=[PagePopup(title="How are we doing?", dismiss="No thanks")],
    ),
    f"{ROOT}/pay": PageResponse(
        task_id="", request_id="", sent_at="", status="ok", url=f"{ROOT}/pay", title="Energy Co | One-Time Payment",
        text="One-Time Payment", fields=["Payment amount", "Card number", "Security code"],
        buttons=[PageLink(label="Submit payment", where="the middle of the page")],
    ),
}


async def fake_read(task_id: str, request_id: str, url: str) -> PageResponse:
    page = PAGES.get(url) or PageResponse(task_id="", request_id="", sent_at="", status="ok", url=url, text="Nothing")
    return page.copy(update={"task_id": task_id, "request_id": request_id, "sent_at": now_iso()})


def task(url=f"{ROOT}/account"):
    return SiteTask(task_id="bill-1", sent_at=now_iso(), url=url, reply_to=ORCH)


async def run_loop(jobs: SiteJobs, review: PageReview, start: list) -> SiteResult:
    pending = [send for send in start if not isinstance(send.message, TaskAck)]
    for _ in range(site.MAX_PAGES + 1):
        [send] = pending
        if isinstance(send.message, SiteResult):
            return send.message
        assert send.to == HELPER and isinstance(send.message, PageCheck)
        [verdict] = review.on_check(SCRAPER, send.message)
        pending = await jobs.on_verdict(HELPER, verdict.message)
    raise AssertionError("the loop didn't end")


def test_amount_is_read_from_the_page_text():
    assert find_amount("Amount due: $84.12 by Nov 3. Last payment $70.00")[:2] == (84.12, "USD")
    assert find_amount("Balance $1,204.50 Total $9.99")[:2] == (1204.5, "USD")
    assert find_amount("Thanks for paying on time") is None


async def test_scraper_and_helper_follow_the_clicks_to_the_payment_page():
    jobs = SiteJobs(orchestrator=ORCH, technical_helper=HELPER, read=fake_read)
    start = await jobs.on_task(ORCH, task())
    assert start[0].to == ORCH and start[0].message.status == "accepted"
    result = await run_loop(jobs, PageReview(scraper=SCRAPER), start)
    assert result.status == "found" and result.amount == 84.12 and result.pay_page
    assert result.pay_button == "Submit payment" and "Card number" in result.pay_fields
    first, second, last = result.steps
    assert (first.page, first.click, first.where) == ("Overview", "Billing & Payment Options",
                                                      "the left column, under “Quick links”")
    assert second.click == "Make a one-time payment" and second.popup.dismiss == "No thanks"
    assert last.page == "One-Time Payment" and not last.click


async def test_loop_ends_not_found_without_guessing():
    jobs = SiteJobs(orchestrator=ORCH, technical_helper=HELPER, read=fake_read)
    result = await run_loop(jobs, PageReview(scraper=SCRAPER), await jobs.on_task(ORCH, task(f"{ROOT}/x")))
    assert result.status == "not_found" and result.amount is None


async def test_only_the_orchestrator_and_helper_are_trusted():
    jobs = SiteJobs(orchestrator=ORCH, technical_helper=HELPER, read=fake_read)
    [refused] = await jobs.on_task("agent1qimpostor", task())
    assert refused.message.status == "rejected"
    start = await jobs.on_task(ORCH, task())
    check = start[1].message
    assert PageReview(scraper=SCRAPER).on_check("agent1qimpostor", check) == []
    [verdict] = PageReview(scraper=SCRAPER).on_check(SCRAPER, check)
    assert await jobs.on_verdict("agent1qimpostor", verdict.message) == []


def test_reader_notes_where_links_sit_and_pop_ups():
    page = read_html(f"{ROOT}/billing", """
        <title>Energy Co | Account Center</title>
        <header><nav><a href="/">Home</a></nav></header>
        <aside><strong>Quick links</strong><a href="/billing">Billing &amp; Payment Options</a></aside>
        <main><h2>Account Center</h2><a href="/pay">Make a one-time payment</a>
          <form><label>Card number</label><input name="cc"><button type="submit">Submit payment</button></form></main>
        <div role="dialog" aria-label="Survey"><h3>How are we doing?</h3>
          <button>Start survey</button><button>No thanks</button></div>
    """)
    where = dict(zip([label for label, _ in page.links], page.link_where))
    assert where["Home"] == "the menu at the top"
    assert where["Billing & Payment Options"] == "the left column, under “Quick links”"
    assert where["Make a one-time payment"] == "the middle of the page, under “Account Center”"
    assert ("Submit payment", "the middle of the page, under “Account Center”") in page.buttons
    assert page.fields == ["Card number"] and page.popups == [("How are we doing?", "No thanks")]
