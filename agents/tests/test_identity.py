"""1. Each agent builds with its own stable identity, a valid port and mailbox enabled."""

from __future__ import annotations

import asyncio
import re

import pytest
from uagents_core.identity import Identity

from shared import agents as shared_agents
from shared.agents import AGENTS, address, fill_missing_seeds, make_agent

REQUIRED_HEADINGS = ["## Summary", "## What it does", "## Constraints and scope", "## Example prompts", "## Keywords"]


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def build_all(unregister, loop):
    from caretaker.agent import create as caregiver
    from orchestrator.agent import create as orchestrator
    from scraper.agent import create as scraper
    from technical_helper.agent import create as helper

    built = {}
    for key, create in (("orchestrator", orchestrator), ("technical_helper", helper), ("scraper", scraper),
                        ("caregiver", caregiver)):
        agent, _terminal = create(loop=loop)
        unregister.append(agent)
        built[key] = agent
    return built


def test_four_agents_have_distinct_stable_identities(unregister, loop):
    built = build_all(unregister, loop)
    addresses = {key: agent.address for key, agent in built.items()}
    assert len(set(addresses.values())) == 4
    for key, agent in built.items():
        seed = shared_agents.seed(key)
        assert agent.address == Identity.from_seed(seed, 0).address == address(key)
        assert agent.name == AGENTS[key].display_name
        assert agent.name.startswith("Hold My Hand ")
    # Building again gives the same addresses: identity comes only from the seed.
    again = build_all(unregister, loop)
    assert {key: agent.address for key, agent in again.items()} == addresses


def test_ports_are_valid_distinct_and_unchanged():
    ports = {key: shared_agents.port(key) for key in AGENTS}
    assert ports == {"orchestrator": 8040, "technical_helper": 8010, "scraper": 8030, "caregiver": 8020}
    assert len(set(ports.values())) == 4
    assert all(1024 < value < 65536 for value in ports.values())
    assert 8000 not in ports.values()  # the backend's port


def test_mailbox_enabled_and_profile_ready(unregister, loop):
    for key, agent in build_all(unregister, loop).items():
        assert agent._use_mailbox, key
        assert any("agentverse.ai" in endpoint.url for endpoint in agent._endpoints)
        assert len(AGENTS[key].description) <= 300


def test_each_agent_registers_the_chat_protocol(unregister, loop):
    from uagents_core.contrib.protocols.chat import chat_protocol_spec

    for agent in build_all(unregister, loop).values():
        names = {getattr(proto, "name", "") for proto in agent.protocols.values()}
        assert chat_protocol_spec.name in names


def test_readmes_have_profile_sections_and_are_ascii():
    for spec in AGENTS.values():
        text = (shared_agents.AGENTS_DIR / spec.folder / "README.md").read_bytes()
        decoded = text.decode("ascii")  # uAgents reads it with the platform encoding
        assert decoded.startswith(f"# {spec.display_name}\n")
        for heading in REQUIRED_HEADINGS:
            assert heading in decoded, (spec.key, heading)


def test_fill_missing_seeds_only_fills_empty_values(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    original = (
        "GROQ_API_KEY=keep-me\r\n"
        "# Generated locally\r\n"
        "ORCHESTRATOR_SEED=existing-orchestrator\r\n"
        "WEBSITE_AGENT_SEED=\r\n"
        "CAREGIVER_SEED=\r\n"
        "\r\n"
        "TECHNICAL_HELPER_SEED=existing-helper\r\n"
    )
    env.write_bytes(original.encode())
    monkeypatch.setenv("ORCHESTRATOR_SEED", "existing-orchestrator")
    monkeypatch.setenv("TECHNICAL_HELPER_SEED", "existing-helper")
    monkeypatch.setenv("CAREGIVER_SEED", "")
    monkeypatch.setenv("SCRAPER_SEED", "")
    filled = fill_missing_seeds(env)
    assert sorted(filled) == ["CAREGIVER_SEED", "SCRAPER_SEED"]
    lines = env.read_bytes().decode().split("\r\n")
    assert lines[0] == "GROQ_API_KEY=keep-me"
    assert lines[2] == "ORCHESTRATOR_SEED=existing-orchestrator"
    assert lines[3] == "WEBSITE_AGENT_SEED="  # reserved: never filled or reused
    assert re.fullmatch(r"CAREGIVER_SEED=[0-9a-f]{64}", lines[4])
    assert lines[6] == "TECHNICAL_HELPER_SEED=existing-helper"
    assert re.search(r"^SCRAPER_SEED=[0-9a-f]{64}$", env.read_text(), re.M)
    # A second run changes nothing.
    before = env.read_bytes()
    assert fill_missing_seeds(env) == []
    assert env.read_bytes() == before


def test_missing_seed_stops_the_agent_with_a_clear_message(monkeypatch):
    monkeypatch.setenv("SCRAPER_SEED", "")
    try:
        make_agent("scraper")
    except SystemExit as exc:
        assert "SCRAPER_SEED" in str(exc)
    else:
        raise AssertionError("expected the agent to refuse to start without a seed")
