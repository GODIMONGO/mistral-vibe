from __future__ import annotations

from types import MappingProxyType

import pytest

from tests.conftest import build_test_agent_loop
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.harness import (
    HarnessCapability,
    HarnessDecision,
    HarnessPhase,
    HarnessPlugin,
)


@pytest.mark.asyncio
async def test_agent_turn_runs_through_harness_lifecycle() -> None:
    phases: list[HarnessPhase] = []

    async def observe(event, next_call):
        phases.append(event.phase)
        return await next_call(event)

    observed = {
        phase: observe
        for phase in (
            HarnessPhase.TURN_START,
            HarnessPhase.PRE_STEP,
            HarnessPhase.MODEL_REQUEST,
            HarnessPhase.TURN_STOPPING,
            HarnessPhase.TURN_END,
        )
    }
    agent = build_test_agent_loop(
        backend=FakeBackend(mock_llm_chunk(content="done")),
        harness_plugins=(
            HarnessPlugin(
                name="test-observer",
                capabilities=frozenset({HarnessCapability.AGENT_LOOP}),
                interceptors=MappingProxyType(observed),
            ),
        ),
    )

    try:
        [event async for event in agent.act("Hello")]
    finally:
        await agent.aclose()

    assert phases == [
        HarnessPhase.TURN_START,
        HarnessPhase.PRE_STEP,
        HarnessPhase.MODEL_REQUEST,
        HarnessPhase.TURN_STOPPING,
        HarnessPhase.TURN_END,
    ]
    assert agent.harness.phase is HarnessPhase.CLOSED


@pytest.mark.asyncio
async def test_completion_gate_can_force_a_verified_follow_up_step() -> None:
    calls = 0

    async def verify(event, next_call):
        nonlocal calls
        downstream = await next_call(event)
        calls += 1
        if calls == 1:
            return HarnessDecision(
                event=downstream.event,
                continue_prompt="Run verification before completing.",
            )
        return downstream

    backend = FakeBackend([
        [mock_llm_chunk(content="looks done")],
        [mock_llm_chunk(content="verified")],
    ])
    agent = build_test_agent_loop(
        backend=backend,
        harness_plugins=(
            HarnessPlugin(
                name="verification-gate",
                capabilities=frozenset({HarnessCapability.REVIEW}),
                interceptors=MappingProxyType({HarnessPhase.TURN_STOPPING: verify}),
            ),
        ),
    )

    try:
        [event async for event in agent.act("Fix it")]
    finally:
        await agent.aclose()

    assert len(backend.requests_messages) == 2
    assert any(
        message.content == "Run verification before completing."
        for message in backend.requests_messages[1]
    )
