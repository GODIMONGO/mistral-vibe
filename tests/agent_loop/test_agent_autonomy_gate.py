from __future__ import annotations

import asyncio

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop import AgentTurnOptions
from vibe.core.config import AutonomyConfig
from vibe.core.subagents import TaskArgs, TaskResult
from vibe.core.tools.base import InvokeContext
from vibe.core.tools.builtins.todo import TodoArgs, TodoResult, TodoStatus
from vibe.core.types import (
    AssistantEvent,
    FunctionCall,
    Role,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
)


class _AdvisorRunner:
    def __init__(
        self,
        advisor_response: str = "Inspect, implement, then verify.",
        worker_response: str = "completed\nTASK_RESULT: PASS",
    ) -> None:
        self.calls: list[TaskArgs] = []
        self.advisor_response = advisor_response
        self.worker_response = worker_response

    async def run(
        self, args: TaskArgs, _ctx: InvokeContext, *, max_result_chars: int = 0
    ):
        del max_result_chars
        self.calls.append(args)
        if args.agent == "reviewer":
            response = (
                "All acceptance criteria are satisfied.\n"
                "EVIDENCE_CHECKED: acceptance criteria => focused tests passed\n"
                "VERDICT: PASS"
            )
        elif args.agent == "goal-advisor":
            response = self.advisor_response
        else:
            response = self.worker_response
        yield TaskResult(response=response, turns_used=1, completed=True)

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
async def test_computer_capability_question_skips_slow_autonomy_bootstrap() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Да, через computer_use.")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo", "computer_use"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(enabled=True, require_worker=True),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner()

    events = [
        event
        async for event in agent.act("ты можешь управлять пк?", subagent_runner=runner)
    ]

    assert runner.calls == []
    assert len(backend.requests_messages) == 1
    assert any(
        isinstance(event, AssistantEvent) and "computer_use" in event.content
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["hi", "Hello!", "привет", "спасибо", "ok"])
async def test_trivial_conversation_does_not_start_advisor_or_subagents(
    prompt: str,
) -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Brief reply")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(enabled=True, require_worker=True),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner()

    _ = [event async for event in agent.act(prompt, subagent_runner=runner)]

    assert runner.calls == []
    assert len(backend.requests_messages) == 1


@pytest.mark.asyncio
async def test_root_desktop_plan_stays_on_main_agent() -> None:
    complete_todo = ToolCall(
        id="complete-desktop",
        index=0,
        function=FunctionCall(
            name="todo",
            arguments=(
                '{"action":"write","todos":[{"id":"desktop",'
                '"content":"Observe and verify the desktop","status":"completed",'
                '"priority":"medium","depends_on":[]}]}'
            ),
        ),
    )
    backend = FakeBackend([
        [mock_llm_chunk(content="Desktop verified", tool_calls=[complete_todo])],
        [mock_llm_chunk(content="Desktop task complete")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo", "computer_use"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True, require_worker=True, require_review=False
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":[{"id":"desktop",'
        '"content":"Observe and verify the desktop","agent":"root",'
        '"depends_on":[]}]}</goal-plan>'
    )

    events = [
        event
        async for event in agent.act("Open and inspect the app", subagent_runner=runner)
    ]

    assert [call.agent for call in runner.calls] == ["goal-advisor"]
    assert not any(
        isinstance(event, ToolCallEvent)
        and isinstance(event.args, TaskArgs)
        and event.args.agent in {"explore", "worker"}
        for event in events
    )
    final_plan = next(
        event.result
        for event in reversed(events)
        if isinstance(event, ToolResultEvent) and isinstance(event.result, TodoResult)
    )
    assert final_plan.todos[0].status is TodoStatus.COMPLETED


class _CancellingSecondWorkerRunner(_AdvisorRunner):
    def __init__(self) -> None:
        super().__init__(
            '<goal-plan>{"tasks":['
            '{"id":"first","content":"First mutation","agent":"worker",'
            '"depends_on":[]},'
            '{"id":"second","content":"Second mutation","agent":"worker",'
            '"depends_on":["first"]}]}</goal-plan>'
        )
        self.cancelled_second = False

    async def run(
        self, args: TaskArgs, _ctx: InvokeContext, *, max_result_chars: int = 0
    ):
        del max_result_chars
        self.calls.append(args)
        if args.agent == "goal-advisor":
            yield TaskResult(
                response=self.advisor_response, turns_used=1, completed=True
            )
            return
        if "Assigned task (second)" in args.task and not self.cancelled_second:
            self.cancelled_second = True
            raise asyncio.CancelledError
        yield TaskResult(
            response="completed\nTASK_RESULT: PASS", turns_used=1, completed=True
        )


@pytest.mark.asyncio
async def test_autonomy_without_subagent_runtime_stops_before_main_model() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Must not run")]])
    config = build_test_vibe_config(
        autonomy=AutonomyConfig(
            enabled=True, require_worker=False, require_review=False
        )
    )
    agent = build_test_agent_loop(config=config, backend=backend)

    events = [event async for event in agent.act("Do the work")]

    assert len(backend.requests_messages) == 0
    blocked = [
        event
        for event in events
        if isinstance(event, AssistantEvent) and event.stopped_by_middleware
    ]
    assert len(blocked) == 1
    assert "subagent runtime is unavailable" in blocked[0].content


@pytest.mark.asyncio
async def test_autonomy_runs_advisor_before_first_main_model_call() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="I should plan")],
        [mock_llm_chunk(content="Still incomplete")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
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
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"inspect","content":"Inspect","agent":"explore",'
        '"depends_on":[]}]}</goal-plan>'
    )

    events = [
        event
        async for event in agent.act("Finish the migration", subagent_runner=runner)
    ]

    assert [call.agent for call in runner.calls] == ["goal-advisor", "explore"]
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
        and "<goal-plan>" in (message.content or "")
        for message in first_request
    )
    assert "Ready explore/worker tasks were delegated" in (
        first_request[-1].content or ""
    )


@pytest.mark.asyncio
async def test_autonomy_materializes_plan_and_delegates_before_main_model() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Integrated automatic work")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True, require_worker=True, require_review=False
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"inspect","content":"Inspect the target","agent":"explore",'
        '"depends_on":[]},'
        '{"id":"implement","content":"Implement the change","agent":"worker",'
        '"depends_on":["inspect"]}]}</goal-plan>'
    )

    events = [
        event
        async for event in agent.act("Finish the migration", subagent_runner=runner)
    ]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "explore",
        "worker",
    ]
    assert "Dependency inspect:\ncompleted\nTASK_RESULT: PASS" in runner.calls[2].task
    plan_call_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ToolCallEvent)
        and isinstance(event.args, TodoArgs)
        and event.args.action == "write"
    )
    worker_call_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ToolCallEvent)
        and isinstance(event.args, TaskArgs)
        and event.args.agent == "worker"
    )
    assert plan_call_index < worker_call_index
    final_plan = next(
        event.result
        for event in reversed(events)
        if isinstance(event, ToolResultEvent) and isinstance(event.result, TodoResult)
    )
    assert all(todo.status is TodoStatus.COMPLETED for todo in final_plan.todos)
    first_request = backend.requests_messages[0]
    assert any(
        message.role is Role.tool and message.name == "todo"
        for message in first_request
    )
    assert (
        sum(
            message.role is Role.tool and message.name == "task"
            for message in first_request
        )
        == 3
    )


@pytest.mark.asyncio
async def test_autonomy_runs_reviewer_after_automatic_worker() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Integrated automatic work")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(enabled=True, require_worker=True, require_review=True),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"implement","content":"Implement and verify","agent":"worker",'
        '"depends_on":[]}]}</goal-plan>'
    )

    events = [
        event async for event in agent.act("Ship the fix", subagent_runner=runner)
    ]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "worker",
        "reviewer",
    ]
    assert "Bounded execution evidence" in runner.calls[2].task
    assert "EVIDENCE_CHECKED:" in runner.calls[2].task
    assert "implement (worker): completed\nTASK_RESULT: PASS" in runner.calls[2].task
    reviewer_call = next(
        event
        for event in events
        if isinstance(event, ToolCallEvent)
        and isinstance(event.args, TaskArgs)
        and event.args.agent == "reviewer"
    )
    assert reviewer_call.presentation is not None
    assert reviewer_call.presentation.display.summary == "Running reviewer"
    assert not any(
        isinstance(event, AssistantEvent) and event.stopped_by_middleware
        for event in events
    )


@pytest.mark.asyncio
async def test_autonomy_compacts_repeated_objective_and_stored_worker_result() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Integrated automatic work")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker"],
            }
        },
        autonomy=AutonomyConfig(enabled=True, require_worker=True, require_review=True),
    )
    objective = "objective-start\n" + ("middle-context\n" * 1_000) + "objective-end"
    worker_response = (
        "result-start\n" + ("verbose-result\n" * 500) + "TASK_RESULT: PASS"
    )
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"implement","content":"Implement and verify",'
        '"agent":"worker","depends_on":[]}]}</goal-plan>',
        worker_response=worker_response,
    )
    agent = build_test_agent_loop(config=config, backend=backend)

    _ = [event async for event in agent.act(objective, subagent_runner=runner)]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "worker",
        "reviewer",
    ]
    for call in runner.calls:
        assert "objective-start" in call.task
        assert "objective-end" in call.task
        assert "[... objective omitted to save context ...]" in call.task
    stored_result = agent._autonomy_task_results["implement"]
    assert len(stored_result) <= 4_000
    assert "result-start" in stored_result
    assert stored_result.endswith("TASK_RESULT: PASS")
    reviewer_prompt = runner.calls[-1].task
    assert reviewer_prompt.count("implement (worker):") == 1
    evidence = reviewer_prompt.split("Bounded execution evidence:\n", maxsplit=1)[1]
    assert len(evidence) <= 8_000


@pytest.mark.asyncio
async def test_required_worker_replaces_an_all_explore_advisor_plan() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Integrated")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True, require_worker=True, require_review=False
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"inspect","content":"Inspect only","agent":"explore",'
        '"depends_on":[]}]}</goal-plan>'
    )

    _ = [event async for event in agent.act("Make the change", subagent_runner=runner)]

    assert [call.agent for call in runner.calls] == ["goal-advisor", "worker"]


@pytest.mark.asyncio
async def test_failed_todo_write_blocks_worker_dispatch() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Must not run")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            },
            "todo": {"permission": "never"},
        },
        autonomy=AutonomyConfig(
            enabled=True, require_worker=True, require_review=False
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"implement","content":"Implement","agent":"worker",'
        '"depends_on":[]}]}</goal-plan>'
    )

    events = [event async for event in agent.act("Make change", subagent_runner=runner)]

    assert [call.agent for call in runner.calls] == ["goal-advisor"]
    assert len(backend.requests_messages) == 0
    assert any(
        isinstance(event, AssistantEvent)
        and event.stopped_by_middleware
        and "initial todo update failed" in event.content
        for event in events
    )


@pytest.mark.asyncio
async def test_worker_failure_does_not_complete_todo() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Cannot claim completion")],
        [mock_llm_chunk(content="Still blocked")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True,
            max_review_retries=1,
            require_worker=True,
            require_review=False,
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        advisor_response=(
            '<goal-plan>{"tasks":['
            '{"id":"implement","content":"Implement","agent":"worker",'
            '"depends_on":[]}]}</goal-plan>'
        ),
        worker_response="Blocked by missing dependency\nTASK_RESULT: FAIL",
    )

    events = [event async for event in agent.act("Make change", subagent_runner=runner)]

    final_plan = next(
        event.result
        for event in reversed(events)
        if isinstance(event, ToolResultEvent) and isinstance(event.result, TodoResult)
    )
    assert final_plan.todos[0].status is TodoStatus.PENDING
    assert any(
        isinstance(event, AssistantEvent) and event.stopped_by_middleware
        for event in events
    )


@pytest.mark.asyncio
async def test_injected_retry_reuses_completed_advisor() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Initial integrated response")],
        [mock_llm_chunk(content="Retry integrated response")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True, require_worker=True, require_review=False
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"implement","content":"Implement and verify","agent":"worker",'
        '"depends_on":[]}]}</goal-plan>'
    )

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

    assert [call.agent for call in runner.calls] == ["goal-advisor", "worker"]
    assert len(backend.requests_messages) == 2
    assert all(
        "Retry the interrupted response" not in call.task for call in runner.calls
    )


@pytest.mark.asyncio
async def test_injected_retry_reviews_the_original_objective() -> None:
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(enabled=True, require_worker=True, require_review=True),
    )
    agent = build_test_agent_loop(
        config=config,
        backend=FakeBackend(exception_to_raise=RuntimeError("interrupted backend")),
    )
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"implement","content":"Implement","agent":"worker",'
        '"depends_on":[]}]}</goal-plan>'
    )

    with pytest.raises(RuntimeError, match="interrupted backend"):
        _ = [
            event
            async for event in agent.act(
                "Original migration objective", subagent_runner=runner
            )
        ]

    replacement = FakeBackend([[mock_llm_chunk(content="Integrated after retry")]])
    agent.backend = replacement
    _ = [
        event
        async for event in agent.act(
            "Retry the interrupted response",
            subagent_runner=runner,
            turn_options=AgentTurnOptions(injected=True),
        )
    ]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "worker",
        "reviewer",
    ]
    assert "Original migration objective" in runner.calls[-1].task
    assert "Retry the interrupted response" not in runner.calls[-1].task


@pytest.mark.asyncio
async def test_injected_retry_resumes_only_the_cancelled_worker() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Incomplete after cancellation")],
        [mock_llm_chunk(content="Still incomplete")],
        [mock_llm_chunk(content="Integrated resumed worker")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True,
            max_review_retries=1,
            require_worker=True,
            require_review=False,
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _CancellingSecondWorkerRunner()

    _ = [
        event async for event in agent.act("Run both mutations", subagent_runner=runner)
    ]
    retry_events = [
        event
        async for event in agent.act(
            "Retry the interrupted response",
            subagent_runner=runner,
            turn_options=AgentTurnOptions(injected=True),
        )
    ]

    assigned = [call.task for call in runner.calls if call.agent == "worker"]
    assert sum("Assigned task (first)" in task for task in assigned) == 1
    assert sum("Assigned task (second)" in task for task in assigned) == 2
    final_plan = next(
        event.result
        for event in reversed(retry_events)
        if isinstance(event, ToolResultEvent) and isinstance(event.result, TodoResult)
    )
    assert all(todo.status is TodoStatus.COMPLETED for todo in final_plan.todos)


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
        assert "Vibe runs the reviewer automatically" in system_prompt
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
