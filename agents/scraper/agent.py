"""Hold My Hand Scraper uAgent.

Takes ScrapeRequest and PageRequest messages from the technical helper only, runs the
approved Apify Actor (or reads one public page), and returns facts with their source.
It never picks a winner. People who chat with it directly are pointed to the
orchestrator, so nobody outside the workflow can start a paid Apify run.

Run on its own:  python agents/scraper/agent.py   (port 8030 unless SCRAPER_PORT is set)
The terminal chat runs one real, paid search per line; set SCRAPER_REPL=0 to turn it off.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.agents import (  # noqa: E402
    AGENTS,
    LOCAL,
    Terminal,
    address,
    chat_protocol,
    deliver,
    make_agent,
    own_agent,
    run,
)
from shared.models import (  # noqa: E402
    SCRAPE_PROTOCOL,
    SCRAPER,
    TECHNICAL_HELPER,
    ErrorInfo,
    PageLink,
    PageRequest,
    PageResponse,
    ScrapeFilters,
    ScrapeRequest,
    ScrapeResponse,
    TaskAck,
    new_id,
    now_iso,
)
from uagents import Agent, Context, Protocol  # noqa: E402

from scraper.flow import Scraper  # noqa: E402
from scraper.page import ScrapeError, fetch  # noqa: E402

RESULT_TTL_SECONDS = 900


def _ack(task_id: str, request_id: str, status: str, error: ErrorInfo | None = None) -> TaskAck:
    return TaskAck(task_id=task_id, request_id=request_id, sender_role=SCRAPER, recipient_role=TECHNICAL_HELPER,
                   sent_at=now_iso(), status=status, error=error)


def _summary(response: ScrapeResponse) -> str:
    if response.error:
        return f"Search failed: {response.error.code}. {response.error.message}"
    if not response.candidates:
        return "No listings matched."
    lines = [f"{len(response.candidates)} listing(s) from {response.search.site}:"]
    for candidate in response.candidates:
        price = f"{candidate.price:.2f} {candidate.currency}" if candidate.price is not None else "price not shown"
        lines.append(f"- {candidate.title} ({candidate.supplier or 'brand not shown'}), {price}\n  {candidate.url}")
    return "\n".join(lines)


def create(scraper: Scraper | None = None, **agent_options) -> tuple[Agent, Terminal]:
    agent = make_agent("scraper", **agent_options)
    scraper = scraper or Scraper()
    helper = address("technical_helper")
    # request_id -> (finished at, response); running searches are in `running`.
    finished: dict[str, tuple[float, ScrapeResponse | PageResponse]] = {}
    running: set[str] = set()

    def forget_old() -> None:
        now = time.monotonic()
        for request_id in [key for key, (at, _) in finished.items() if now - at > RESULT_TTL_SECONDS]:
            del finished[request_id]

    async def start(ctx: Context, sender: str, task_id: str, request_id: str) -> bool:
        """Acknowledge a request. False if it shouldn't be run (wrong sender, or already handled)."""
        if sender != helper:
            ctx.logger.warning(f"Refused a request from {sender}: only the technical helper may ask for searches")
            await ctx.send(sender, _ack(task_id, request_id, "rejected", ErrorInfo(
                code="unauthorized_sender", message="Only the Hold My Hand Technical Helper can request searches.")))
            return False
        forget_old()
        if request_id in finished:
            await ctx.send(sender, _ack(task_id, request_id, "duplicate"))
            await ctx.send(sender, finished[request_id][1])
            return False
        if request_id in running:
            await ctx.send(sender, _ack(task_id, request_id, "duplicate"))
            return False
        running.add(request_id)
        await ctx.send(sender, _ack(task_id, request_id, "accepted"))
        return True

    proto = Protocol(name=SCRAPE_PROTOCOL[0], version=SCRAPE_PROTOCOL[1])

    @proto.on_message(ScrapeRequest, replies={TaskAck, ScrapeResponse})
    async def on_scrape(ctx: Context, sender: str, msg: ScrapeRequest):
        if not await start(ctx, sender, msg.task_id, msg.request_id):
            return
        ctx.logger.info(f"[{msg.task_id}] search {msg.request_id}: '{msg.search_query}' on {msg.site}")
        try:
            response = await scraper.search(msg)
        finally:
            running.discard(msg.request_id)
        finished[msg.request_id] = (time.monotonic(), response)
        ctx.logger.info(f"[{msg.task_id}] {response.status}: {len(response.candidates)} candidate(s), "
                        f"{response.search.items_seen} seen, {response.search.items_dropped} dropped"
                        + (f", error {response.error.code}" if response.error else ""))
        await ctx.send(sender, response)

    @proto.on_message(PageRequest, replies={TaskAck, PageResponse})
    async def on_page(ctx: Context, sender: str, msg: PageRequest):
        if not await start(ctx, sender, msg.task_id, msg.request_id):
            return
        try:
            page = await asyncio.to_thread(fetch, msg.url)
            response = PageResponse(
                task_id=msg.task_id, request_id=msg.request_id, sent_at=now_iso(), status="ok", url=page.url,
                title=page.title, headings=page.headings[:20],
                links=[PageLink(label=label, url=href) for label, href in page.links[:40]],
                text=page.text[:4000], collected_at=now_iso(),
            )
        except ScrapeError as exc:
            response = PageResponse(task_id=msg.task_id, request_id=msg.request_id, sent_at=now_iso(), status="error",
                                    url=msg.url, error=ErrorInfo(code="page_unavailable", message=str(exc)))
        finally:
            running.discard(msg.request_id)
        finished[msg.request_id] = (time.monotonic(), response)
        await ctx.send(sender, response)

    @proto.on_message(TaskAck)
    async def on_ack(ctx: Context, sender: str, msg: TaskAck):
        ctx.logger.debug(f"Ack from {sender} for {msg.task_id}: {msg.status}")

    agent.include(proto, publish_manifest=True)

    orchestrator = AGENTS["orchestrator"].display_name

    async def on_chat(ctx: Context, sender: str, text: str) -> None:
        if sender == LOCAL:
            request = ScrapeRequest(task_id=new_id("local"), request_id=new_id("req"), sender_role="terminal",
                                    sent_at=now_iso(), search_query=text, site="amazon.com", filters=ScrapeFilters())
            await deliver(ctx, sender, "Running one Apify search (this uses Apify credit)...")
            await deliver(ctx, sender, _summary(await scraper.search(request)))
            return
        await deliver(ctx, sender, (
            f"I'm the part of Hold My Hand that collects product listings, so I only take requests from our "
            f"Technical Helper. To shop, please chat with {orchestrator} at {address('orchestrator')}."
        ))

    agent.include(chat_protocol(on_chat, ignore=own_agent), publish_manifest=True)

    @agent.on_event("startup")
    async def startup(ctx: Context):
        token = "set" if scraper.token() else "MISSING: product searches will return integration_unavailable"
        ctx.logger.info(f"Scraper ready. Apify token: {token}. Accepting requests from {helper}")

    terminal = Terminal(agent, "Hold My Hand Scraper (each line runs one paid Apify search)", on_chat,
                        AGENTS["scraper"].repl_var)
    return agent, terminal


if __name__ == "__main__":
    run(*create())
