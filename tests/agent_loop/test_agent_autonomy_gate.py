from __future__ import annotations

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop import AgentTurnOptions
from vibe.core.config import AutonomyConfig
from vibe.core.subagents import TaskArgs, TaskResult
from vibe.core.tools.base import InvokeContext
from vibe.core.types import AssistantEvent, Role, ToolCallEvent, ToolStreamEvent


class _AdvisorRunner:
    def __init__(self) -> None:
        self.calls: list[TaskArgs] = []

    async def run(
        self, args: TaskArgs, _ctx: InvokeContext, *, max_result_chars: int = 0
    ):
        del max_result_chars
        self.calls.append(args)
        yield TaskResult(
            response="Inspect, implement, then verify.", turns_used=1, completed=True
        )

    async def run_many(
        self,
        _args: list[TaskArgs],
        _ctx: InvokeContext,
        *,
        max_parallel: int,
        max_result_chars: int = 0,
    ):
        del max_parallel, max_result_chars
        if False:
            yield ToolStreamEvent(tool_name="swarm", message="", tool_call_id="")
        return


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
async def test_autonomy_runs_advisor_before_first_main_model_call() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="I should plan")],
        [mock_llm_chunk(content="Still incomplete")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True,
            max_review_retries=1,
            require_worker=False,
            require_review=False,
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner()

    events = [
        event
        async for event in agent.act("Finish the migration", subagent_runner=runner)
    ]

    assert len(runner.calls) == 1
    assert runner.calls[0].agent == "goal-advisor"
    assert "Finish the migration" in runner.calls[0].task
    advisor_call = next(
        event
        for event in events
        if isinstance(event, ToolCallEvent)
        and isinstance(event.args, TaskArgs)
        and event.args.agent == "goal-advisor"
    )
    assert advisor_call.presentation is not None
    assert advisor_call.presentation.display.summary == "Running goal advisor"
    first_request = backend.requests_messages[0]
    assert any(
        message.role is Role.tool
        and message.name == "task"
        and "Inspect, implement, then verify" in (message.content or "")
        for message in first_request
    )
    assert "Immediately materialize" in (first_request[-1].content or "")


@pytest.mark.asyncio
async def test_injected_retry_reuses_completed_advisor() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Initial incomplete response")],
        [mock_llm_chunk(content="Initial blocked response")],
        [mock_llm_chunk(content="Retry incomplete response")],
        [mock_llm_chunk(content="Retry blocked response")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True,
            max_review_retries=1,
            require_worker=False,
            require_review=False,
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner()

    _ = [
        event
        async for event in agent.act("Finish the migration", subagent_runner=runner)
    ]
    _ = [
        event
        async for event in agent.act(
            "Retry the interrupted response",
            subagent_runner=runner,
            turn_options=AgentTurnOptions(injected=True),
        )
    ]

    assert len(runner.calls) == 1
    assert all(
        "Retry the interrupted response" not in call.task for call in runner.calls
    )


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
