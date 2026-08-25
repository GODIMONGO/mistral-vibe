from __future__ import annotations

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import AutonomyConfig
from vibe.core.types import AssistantEvent, Role


@pytest.mark.asyncio
async def test_autonomy_gate_retries_then_blocks_unverified_completion() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Done without evidence")],
        [mock_llm_chunk(content="Still done without evidence")],
    ])
    config = build_test_vibe_config(
        autonomy=AutonomyConfig(
            enabled=True,
            max_review_retries=1,
            require_worker=False,
            require_review=False,
        )
    )
    agent = build_test_agent_loop(config=config, backend=backend)

    events = [event async for event in agent.act("Do the work")]

    assert len(backend.requests_messages) == 2
    retry = backend.requests_messages[1][-1]
    assert retry.role is Role.user
    assert retry.injected is True
    assert "goal advisor has not completed" in (retry.content or "")
    blocked = [
        event
        for event in events
        if isinstance(event, AssistantEvent) and event.stopped_by_middleware
    ]
    assert len(blocked) == 1
    assert "Autonomous completion blocked" in blocked[0].content


@pytest.mark.asyncio
async def test_autonomy_extends_the_mistral_cli_prompt() -> None:
    config = build_test_vibe_config(
        system_prompt_id="cli", autonomy=AutonomyConfig(enabled=True)
    )
    agent = build_test_agent_loop(config=config)
    await agent.wait_until_ready()
    try:
        system_prompt = agent.messages[0].content or ""
        assert "You are Mistral Vibe" in system_prompt
        assert "## Critical instructions" in system_prompt
        assert "## Autonomous operating protocol" in system_prompt
        assert "Run the reviewer last" in system_prompt
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_autonomy_gate_is_disabled_for_regular_agents() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Regular completion")]])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(autonomy=AutonomyConfig(enabled=False)),
        backend=backend,
    )

    events = [event async for event in agent.act("Answer briefly")]

    assert len(backend.requests_messages) == 1
    assert not any(
        isinstance(event, AssistantEvent) and event.stopped_by_middleware
        for event in events
    )
