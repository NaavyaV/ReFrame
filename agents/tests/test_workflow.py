"""2 and 6. The real agents, wired together in one process with a fake Apify client.

Person -> Orchestrator -> Technical Helper -> Scraper -> (fake Apify) -> Scraper -> Technical Helper
-> Orchestrator -> Person, using real uAgents message handlers and local dispatch, no network.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec

from caretaker.agent import create as create_caregiver
from caretaker.flow import CaregiverService
from caretaker.store import MemoryStore
from conftest import FakeApify, amazon_item
from orchestrator.agent import BridgeMessage, BridgeStatusRequest
from orchestrator.agent import create as create_orchestrator
from orchestrator.flow import Orchestrator
from scraper.agent import create as create_scraper
from scraper.flow import Scraper
from shared.agents import address
from shared.models import SELECTED
from shared.security import bridge_signature
from technical_helper.agent import create as create_helper
from technical_helper.flow import ProductSearches, TechHelp

LISTINGS = [
    amazon_item(1, "Heavy Duty Upright Vacuum", 189.0),
    amazon_item(2, "Lightweight Stick Vacuum, Easy to Use, 4.5 lb", 79.99, features=["Lightweight and easy to use"]),
    amazon_item(3, "Replacement Filters for Stick Vacuum", 14.99),
]


class Person:
    """A chat user agent that records what it receives."""

    def __init__(self, seed: str, loop) -> None:
        self.agent = Agent(name="test person", seed=seed, loop=loop)
        self.texts: list[str] = []
        self.acks = 0
        proto = Protocol(spec=chat_protocol_spec)

        @proto.on_message(ChatMessage)
        async def on_message(ctx: Context, sender: str, msg: ChatMessage):
            self.texts.append(msg.text())
            await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))

        @proto.on_message(ChatAcknowledgement)
        async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
            self.acks += 1

        self.agent.include(proto)

    async def say(self, to: str, text: str) -> None:
        await self.agent._build_context().send(to, ChatMessage(content=[TextContent(text=text)], msg_id=uuid.uuid4()))

    async def wait_for(self, words: str, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for text in self.texts:
                if words in text:
                    return text
            await asyncio.sleep(0.05)
        raise AssertionError(f"never received {words!r}; got {self.texts}")


@pytest.fixture
async def system(unregister):
    loop = asyncio.get_running_loop()
    apify = FakeApify(LISTINGS)
    scraper = Scraper(apify.factory, token="test-token")
    seen_requests = []
    original_search = scraper.search

    async def recording_search(request):
        seen_requests.append(request)
        return await original_search(request)

    scraper.search = recording_search
    searches = ProductSearches(scraper=address("scraper"), orchestrator=address("orchestrator"), explain=None)
    store = MemoryStore()
    orchestrator_flow = Orchestrator(own_address=address("orchestrator"), technical_helper=address("technical_helper"),
                                     caregiver=address("caregiver"))
    agents = {
        "orchestrator": create_orchestrator(orchestrator_flow, loop=loop)[0],
        "technical_helper": create_helper(searches, TechHelp(scraper=address("scraper"),
                                                             orchestrator=address("orchestrator")), loop=loop)[0],
        "scraper": create_scraper(scraper, loop=loop)[0],
        "caregiver": create_caregiver(CaregiverService(store, orchestrator=address("orchestrator")), loop=loop)[0],
    }
    person = Person("hmh-test-person-seed-0001", loop)
    carer = Person("hmh-test-carer-seed-0001", loop)
    everyone = [*agents.values(), person.agent, carer.agent]
    unregister.extend(everyone)
    for agent in everyone:
        agent.start_message_dispenser()
        agent.start_message_receivers()
    yield {"agents": agents, "person": person, "carer": carer, "apify": apify, "requests": seen_requests,
           "searches": searches, "flow": orchestrator_flow, "store": store}
    for agent in everyone:
        for task in (agent._message_queue_task, agent._dispenser_task):
            if task is not None:
                task.cancel()
    await asyncio.sleep(0)


async def test_addresses_are_derived_from_seeds(system):
    agents = system["agents"]
    assert {key: agent.address for key, agent in agents.items()} == {key: address(key) for key in agents}
    flow = system["flow"]
    assert flow.technical_helper == agents["technical_helper"].address
    assert flow.caregiver == agents["caregiver"].address
    assert system["searches"].scraper == agents["scraper"].address


async def test_shopping_request_end_to_end(system):
    person, flow = system["person"], system["flow"]
    await person.say(address("orchestrator"), "Find a lightweight vacuum that is easy to use under $100")
    reply = await person.wait_for("Link:")
    assert any("I'm looking on Amazon.com" in text for text in person.texts)
    assert "Lightweight Stick Vacuum" in reply and "$79.99" in reply
    assert "https://www.amazon.com/dp/B0TEST0002" in reply and "haven't bought anything" in reply

    [task] = [t for t in flow.tasks.values() if t.kind == "product"]
    assert task.status == SELECTED
    # The same correlation id all the way through.
    assert [r.task_id for r in system["requests"]] == [task.task_id]
    result = system["searches"].done[task.task_id]
    assert result.task_id == task.task_id and result.selected_candidate.url == "https://www.amazon.com/dp/B0TEST0002"
    assert system["apify"].actor_ids == ["junglee/amazon-crawler"]
    assert person.acks >= 1  # the orchestrator acknowledged the chat message

    # Nothing keeps bouncing around afterwards.
    count = len(person.texts)
    await asyncio.sleep(0.5)
    assert len(person.texts) == count and len(system["requests"]) == 1


async def test_bridge_end_to_end(system, monkeypatch):
    orchestrator = system["agents"]["orchestrator"]
    secret = "hmh-test-bridge-secret-0001"

    def signed(model, user_id, body, **fields):
        stamp, nonce = int(time.time()), uuid.uuid4().hex
        return model(user_id=user_id, timestamp=stamp, nonce=nonce,
                     signature=bridge_signature(secret, stamp, nonce, user_id, body), **fields)

    forged = BridgeMessage(user_id="u1", message="Find a vacuum under $100", timestamp=int(time.time()), nonce="n1",
                           signature="0" * 64)
    refused = await orchestrator.handle_rest("POST", "/bridge/message", forged)
    assert not refused.ok and refused.error == "bad_signature"

    request = signed(BridgeMessage, "u1", "Find a lightweight vacuum under $100",
                     message="Find a lightweight vacuum under $100")
    first = await orchestrator.handle_rest("POST", "/bridge/message", request)
    assert first.ok and first.task_id and any("looking" in m for m in first.messages)
    replay = await orchestrator.handle_rest("POST", "/bridge/message", request)
    assert not replay.ok and replay.error == "replayed_request"

    deadline = time.monotonic() + 10
    status = first
    while time.monotonic() < deadline and status.status != SELECTED:
        await asyncio.sleep(0.1)
        status = await orchestrator.handle_rest("POST", "/bridge/status",
                                                signed(BridgeStatusRequest, "u1", first.task_id, task_id=first.task_id))
    assert status.status == SELECTED
    assert status.result["selected_candidate"]["url"] == "https://www.amazon.com/dp/B0TEST0002"
    assert status.ui_hints == {"text_size": "standard"}

    other = await orchestrator.handle_rest("POST", "/bridge/status",
                                           signed(BridgeStatusRequest, "u2", first.task_id, task_id=first.task_id))
    assert other.ok and other.task_id == "" and other.result == {}  # another user can't read it


async def test_caregiver_flow_end_to_end(system):
    person, carer, flow = system["person"], system["carer"], system["flow"]
    orchestrator, caregiver = address("orchestrator"), address("caregiver")
    await person.say(orchestrator, "Please add a caregiver")
    await person.wait_for("Which of these may they change?")
    await person.say(orchestrator, "2")
    await person.wait_for("Shall I create a one-time code")
    await person.say(orchestrator, "yes")
    code_text = await person.wait_for("Here is the code")
    code = code_text.split("**")[1]

    await carer.say(caregiver, code)
    await carer.wait_for("You're now linked")
    await carer.say(caregiver, "Turn on simpler wording")
    await carer.wait_for("didn't allow you")  # not in the scope the person chose
    await carer.say(caregiver, "Use shorter explanations please")
    await carer.wait_for("Done: shorter explanations: on")

    user = flow.session(person.agent.address).user
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and flow.preferences(user)["explanation_length"] != "short":
        await asyncio.sleep(0.05)
    assert flow.preferences(user)["explanation_length"] == "short"
    assert system["store"].prefs[user]["explanation_length"].set_by == carer.agent.address

    await person.say(orchestrator, "Find a lightweight vacuum under $100")
    note = await person.wait_for("your caregiver changed a setting")
    assert note.startswith("Looking on Amazon.com")

    await person.wait_for("Link:")
    await person.say(orchestrator, "remove my caregiver")
    await person.wait_for("Remove your caregiver's access")
    await person.say(orchestrator, "yes")
    await person.wait_for("they no longer have access")
    await carer.say(caregiver, "Use shorter explanations please")
    await carer.wait_for("don't have caregiver access")


async def test_our_own_agents_never_chat_each_other_into_a_loop(system):
    helper = system["agents"]["technical_helper"]
    await helper._build_context().send(
        address("orchestrator"), ChatMessage(content=[TextContent(text="hello?")], msg_id=uuid.uuid4()))
    await asyncio.sleep(0.5)
    assert address("technical_helper") not in system["flow"].sessions
