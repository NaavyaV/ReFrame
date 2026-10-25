"""Website tasks from the orchestrator, worked through with the technical helper.

The scraper reads the page the person named and sends it to the technical helper.
The helper says whether it is the payment page, reads the amount due if the page
shows one, and otherwise names links on the same site to follow next. The scraper
follows them, remembering which link it clicked on which page, until the helper
finds the payment page or MAX_PAGES pages have been read. It then reports the
amount and the clicks that lead to the payment page to the orchestrator.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from uagents import Model

from shared.models import (
    FAILED,
    ORCHESTRATOR,
    SCRAPER,
    ErrorInfo,
    PageCheck,
    PageLink,
    PagePopup,
    PageResponse,
    PageVerdict,
    SiteResult,
    SiteStep,
    SiteTask,
    TaskAck,
    new_id,
    now_iso,
)
from shared.security import UnsafeURL, check_public_url

from scraper.page import ScrapeError, fetch

MAX_PAGES = 6
VERDICT_TIMEOUT_SECONDS = 45
DONE_KEPT = 200


@dataclass
class Send:
    to: str
    message: Model


def _norm(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def page_name(page: PageResponse) -> str:
    """What a person sees as the page's name: "My Account Overview", not "BrightGrid Energy | My Account Overview"."""
    for separator in (" | ", " – ", " — ", " - "):
        if separator in page.title:
            return page.title.split(separator)[-1].strip()
    return page.title or (page.headings[0] if page.headings else urlsplit(page.url).path or page.url)


async def read_page(task_id: str, request_id: str, url: str) -> PageResponse:
    try:
        page = await asyncio.to_thread(fetch, url)
    except ScrapeError as exc:
        return PageResponse(task_id=task_id, request_id=request_id, sent_at=now_iso(), status="error", url=url,
                            error=ErrorInfo(code="page_unavailable", message=str(exc)))
    return PageResponse(
        task_id=task_id, request_id=request_id, sent_at=now_iso(), status="ok", url=page.url,
        title=page.title, headings=page.headings[:20],
        links=[PageLink(label=label, url=href, where=where)
               for (label, href), where in zip(page.links[:60], page.link_where[:60])],
        buttons=[PageLink(label=label, where=where) for label, where in page.buttons],
        fields=page.fields, popups=[PagePopup(title=title, dismiss=dismiss) for title, dismiss in page.popups],
        text=page.text, collected_at=now_iso(),
    )


Reader = Callable[[str, str, str], Awaitable[PageResponse]]


@dataclass
class SiteJob:
    task: SiteTask
    queue: list[tuple[str, list[SiteStep]]]  # (url, the clicks that reach it)
    visited: list[str] = field(default_factory=list)
    request_id: str = ""
    sent_at: float = 0.0
    page: PageResponse | None = None
    trail: list[SiteStep] = field(default_factory=list)  # clicks that reached the current page
    pages_read: int = 0
    amount: float | None = None
    currency: str = ""
    evidence: str = ""
    amount_trail: list[SiteStep] = field(default_factory=list)
    amount_page: PageResponse | None = None
    last_error: ErrorInfo | None = None


def _popup(page: PageResponse) -> PagePopup | None:
    return next((popup for popup in page.popups if popup.dismiss), None)


class SiteJobs:
    def __init__(self, *, orchestrator: str, technical_helper: str, read: Reader = read_page,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.orchestrator = orchestrator
        self.technical_helper = technical_helper
        self._read = read
        self._clock = clock
        self.jobs: dict[str, SiteJob] = {}
        self.done: dict[str, SiteResult] = {}

    def _ack(self, task_id: str, status: str) -> Send:
        return Send(self.orchestrator, TaskAck(task_id=task_id, sender_role=SCRAPER, recipient_role=ORCHESTRATOR,
                                               sent_at=now_iso(), status=status))

    async def on_task(self, sender: str, task: SiteTask) -> list[Send]:
        def reject(code: str, message: str) -> list[Send]:
            return [Send(sender, TaskAck(task_id=task.task_id, sender_role=SCRAPER, recipient_role=ORCHESTRATOR,
                                         sent_at=now_iso(), status="rejected",
                                         error=ErrorInfo(code=code, message=message)))]

        if sender != self.orchestrator:
            return reject("unauthorized_sender", "Only the Hold My Hand Orchestrator can send website tasks.")
        if task.reply_to != sender:
            return reject("bad_request", "reply_to must be the sender's own address.")
        if task.task_id in self.done:
            return [self._ack(task.task_id, "duplicate"), Send(sender, self.done[task.task_id])]
        if task.task_id in self.jobs:
            return [self._ack(task.task_id, "duplicate")]
        try:
            check_public_url(task.url, resolve=False)
        except UnsafeURL as exc:
            return reject("bad_request", str(exc))
        job = SiteJob(task, queue=[(task.url, [])])
        self.jobs[task.task_id] = job
        return [self._ack(task.task_id, "accepted"), *await self._read_next(job)]

    async def _read_next(self, job: SiteJob) -> list[Send]:
        while job.queue and len(job.visited) < MAX_PAGES:
            url, trail = job.queue.pop(0)
            if _norm(url) in job.visited:
                continue
            job.visited.append(_norm(url))
            request_id = new_id("check")
            page = await self._read(job.task.task_id, request_id, url)
            if page.status != "ok":
                job.last_error = page.error
                continue
            job.pages_read += 1
            job.visited.append(_norm(page.url))
            job.page, job.trail = page, trail
            job.request_id = request_id
            job.sent_at = self._clock()
            return [Send(self.technical_helper, PageCheck(
                task_id=job.task.task_id, request_id=request_id, sent_at=now_iso(), goal=job.task.goal,
                origin=job.task.url, page=page,
            ))]
        if job.amount is not None and job.amount_page is not None:
            return [self._finish(job, job.amount_page, job.amount_trail)]
        if job.pages_read == 0:
            return [self._fail(job, job.last_error or ErrorInfo(code="page_unavailable",
                                                                message="I couldn't open that site."))]
        return [self._finish_not_found(job)]

    async def on_verdict(self, sender: str, verdict: PageVerdict) -> list[Send]:
        if sender != self.technical_helper:
            return []
        job = self.jobs.get(verdict.task_id)
        if job is None or verdict.request_id != job.request_id or job.page is None:
            return []
        job.request_id = ""
        page = job.page
        if verdict.amount is not None:
            job.amount, job.currency, job.evidence = verdict.amount, verdict.currency, verdict.evidence
            job.amount_page, job.amount_trail = page, job.trail
        if verdict.status == "found":
            return [self._finish(job, page, job.trail, pay_button=verdict.pay_button)]
        queued = {_norm(url) for url, _ in job.queue}
        for link in verdict.next_links:
            try:
                check_public_url(link.url, resolve=False)
            except UnsafeURL:
                continue
            if _norm(link.url) in job.visited or _norm(link.url) in queued:
                continue
            step = SiteStep(page=page_name(page), click=link.label, where=link.where, popup=_popup(page))
            job.queue.append((link.url, [*job.trail, step]))
            queued.add(_norm(link.url))
        return await self._read_next(job)

    async def sweep(self) -> list[Send]:
        now = self._clock()
        late = [job for job in self.jobs.values() if job.request_id and now - job.sent_at > VERDICT_TIMEOUT_SECONDS]
        return [self._fail(job, ErrorInfo(code="timeout", message="The technical helper didn't answer in time.",
                                          retryable=True)) for job in late]

    def _finish(self, job: SiteJob, page: PageResponse, trail: list[SiteStep], *, pay_button: str = "") -> Send:
        last = SiteStep(page=page_name(page), popup=_popup(page))
        return self._send(job, SiteResult(
            task_id=job.task.task_id, sent_at=now_iso(), status="found", amount=job.amount, currency=job.currency,
            evidence=job.evidence, source_url=page.url, pages_read=job.pages_read, steps=[*trail, last],
            pay_page=bool(pay_button), pay_fields=page.fields if pay_button else [], pay_button=pay_button,
        ))

    def _finish_not_found(self, job: SiteJob) -> Send:
        return self._send(job, SiteResult(task_id=job.task.task_id, sent_at=now_iso(), status="not_found",
                                          pages_read=job.pages_read))

    def _fail(self, job: SiteJob, error: ErrorInfo) -> Send:
        return self._send(job, SiteResult(task_id=job.task.task_id, sent_at=now_iso(), status=FAILED,
                                          pages_read=job.pages_read, error=error))

    def _send(self, job: SiteJob, result: SiteResult) -> Send:
        del self.jobs[job.task.task_id]
        self.done[job.task.task_id] = result
        if len(self.done) > DONE_KEPT:
            self.done.pop(next(iter(self.done)))
        return Send(job.task.reply_to, result)
