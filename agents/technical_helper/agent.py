"""Hold My Hand Technical Helper uAgent.

Takes ProductSearchTask and TechHelpTask messages from the orchestrator only. For a
product it asks the scraper for real listings, checks them against the person's
budget and needs, retries at most twice with tighter searches, and returns one
product or a clear no_match. It never reads pages itself; the scraper does that.

People can also chat with it directly about a device or website problem; shopping
goes through the orchestrator.

Run on its own:  python agents/technical_helper/agent.py   (port 8010 unless TECHNICAL_HELPER_PORT is set)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.agents import (  # noqa: E402
    AGENTS,
    RESET_WORDS,
    Terminal,
    address,
    chat_protocol,
    deliver,
    make_agent,
    own_agent,
    run,
)
from shared.models import (  # noqa: E402
    SHOPPING_PROTOCOL,
    PageResponse,
    ProductSearchTask,
    ScrapeResponse,
    TaskAck,
    TechHelpTask,
)
from uagents import Agent, Context, Protocol  # noqa: E402

from technical_helper.flow import ProductSearches, Send, TechHelp  # noqa: E402


async def send_all(ctx: Context, sends: list[Send]) -> None:
    for send in sends:
        await ctx.send(send.to, send.message)


def create(searches: ProductSearches | None = None, help_desk: TechHelp | None = None,
           **agent_options) -> tuple[Agent, Terminal]:
    agent = make_agent("technical_helper", **agent_options)
    scraper, orchestrator = address("scraper"), address("orchestrator")
    searches = searches or ProductSearches(scraper=scraper, orchestrator=orchestrator)
    help_desk = help_desk or TechHelp(scraper=scraper, orchestrator=orchestrator)

    # From the orchestrator.
    shopping = Protocol(name=SHOPPING_PROTOCOL[0], version=SHOPPING_PROTOCOL[1])

    @shopping.on_message(ProductSearchTask, replies={TaskAck})
    async def on_product_task(ctx: Context, sender: str, msg: ProductSearchTask):
        ctx.logger.info(f"[{msg.task_id}] product task: {msg.hard_constraints.category}"
                        + (f" up to {msg.hard_constraints.max_price:g} {msg.hard_constraints.currency}"
                           if msg.hard_constraints.max_price else ""))
        await send_all(ctx, await searches.on_task(sender, msg))

    @shopping.on_message(TechHelpTask, replies={TaskAck})
    async def on_help_task(ctx: Context, sender: str, msg: TechHelpTask):
        ctx.logger.info(f"[{msg.task_id}] help task" + (" with a page" if msg.page_url else ""))
        await send_all(ctx, await help_desk.on_task(sender, msg))

    agent.include(shopping, publish_manifest=True)

    # From the scraper.
    scraping = Protocol(name="hmh-scrape-client", version="1.0.0")

    @scraping.on_message(TaskAck)
    async def on_scraper_ack(ctx: Context, sender: str, msg: TaskAck):
        if msg.status == "rejected":
            ctx.logger.warning(f"[{msg.task_id}] scraper refused {msg.request_id}: {msg.error.code if msg.error else ''}")
        await send_all(ctx, await searches.on_scrape_ack(sender, msg))

    @scraping.on_message(ScrapeResponse)
    async def on_listings(ctx: Context, sender: str, msg: ScrapeResponse):
        sends = await searches.on_scrape_response(sender, msg)
        for send in sends:
            kind = type(send.message).__name__
            status = getattr(send.message, "status", "")
            ctx.logger.info(f"[{msg.task_id}] {msg.status} with {len(msg.candidates)} listing(s) -> {kind} {status}")
        await send_all(ctx, sends)

    @scraping.on_message(PageResponse)
    async def on_page(ctx: Context, sender: str, msg: PageResponse):
        await send_all(ctx, await help_desk.on_page(sender, msg))

    agent.include(scraping)

    @agent.on_interval(period=5.0)
    async def sweep(ctx: Context):
        await send_all(ctx, await searches.sweep())
        await send_all(ctx, await help_desk.sweep())

    orchestrator_name = AGENTS["orchestrator"].display_name

    async def on_chat(ctx: Context, sender: str, text: str) -> None:
        if text.lower() in RESET_WORDS:
            help_desk.reset(sender)
            await deliver(ctx, sender, "Okay. What would you like help with?")
            return
        reply, _ = await help_desk.answer(sender, text)
        if sender != "local":
            reply += f"\n\nLooking for something to buy? Chat with {orchestrator_name} ({orchestrator})."
        await deliver(ctx, sender, reply)

    agent.include(chat_protocol(on_chat, on_end=help_desk.reset, ignore=own_agent), publish_manifest=True)

    @agent.on_event("startup")
    async def startup(ctx: Context):
        ctx.logger.info(f"Technical helper ready. Orchestrator {orchestrator}, scraper {scraper}")

    terminal = Terminal(agent, "Hold My Hand Technical Helper", on_chat, AGENTS["technical_helper"].repl_var)
    return agent, terminal


if __name__ == "__main__":
    run(*create())
