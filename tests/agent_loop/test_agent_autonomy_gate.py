from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.config_values import WebSearchActivity
from vibe.core.agent_loop import AgentTurnOptions
from vibe.core.compaction import select_model_context
from vibe.core.config import AutonomyConfig
from vibe.core.memory import (
    GlobalMemoryStore,
    build_working_memory_message,
    load_working_memory,
)
from vibe.core.subagents import TaskArgs, TaskResult
from vibe.core.tools.base import InvokeContext
from vibe.core.tools.builtins.todo import TodoArgs, TodoResult, TodoStatus
from vibe.core.tools.builtins.web_search import WebSearchArgs, WebSearchResult
from vibe.core.types import (
    AssistantEvent,
    FunctionCall,
    LLMChunk,
    LLMMessage,
    MemoryStatusEvent,
    ReasoningEvent,
    Role,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
)


def _intent_backend(intent: str, chunks: list[list[LLMChunk]]) -> FakeBackend:
    return FakeBackend([[mock_llm_chunk(content=intent)], *chunks])


def _autonomous_backend(chunks: list[list[LLMChunk]]) -> FakeBackend:
    return _intent_backend("AUTONOMOUS", chunks)


def _direct_backend(chunks: list[list[LLMChunk]]) -> FakeBackend:
    return _intent_backend("DIRECT", chunks)


@pytest.mark.asyncio
async def test_vibe_thinking_adds_private_deliberation_before_main_agent() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Check constraints and verify the result.")],
        [
            mock_llm_chunk(
                content=(
                    "DIRECTION: CONTINUE\nPLAN: 1. Inspect constraints\n"
                    "COMPLETION GATE: Verify the result"
                )
            )
        ],
        [mock_llm_chunk(content="Final answer")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=False, vibe_thinking="low")
        ),
        backend=backend,
    )

    events = [event async for event in agent.act("Solve carefully")]

    assert len(backend.requests_messages) == 3
    assert backend.requests_max_tokens[:2] == [512, 512]
    assert backend.requests_models[0].thinking == "low"
    assert backend.requests_models[1].thinking == "off"
    assert "private strategic reasoning layer" in (
        backend.requests_messages[0][0].content or ""
    )
    assert "<vibe_deliberation>" in (backend.requests_messages[2][-1].content or "")
    assert any(
        isinstance(event, ReasoningEvent)
        and "strategy challenge 1/1" in event.content
        and event.status_text == "Vibe thinking 1/1 · strategy challenge"
        for event in events
    )
    decision_event = next(
        event
        for event in events
        if isinstance(event, ReasoningEvent)
        and event.status_text is not None
        and event.status_text.startswith("Vibe decision · CONTINUE")
    )
    assert "**Direction:** CONTINUE" in decision_event.content
    assert "**Plan:** 1. Inspect constraints" in decision_event.content
    assert "**Completion proof:** Verify the result" in decision_event.content
    reflection_prompt = backend.requests_messages[0][-1].content or ""
    assert "Independent reflection candidate 1/1: strategy challenge" in (
        reflection_prompt
    )
    synthesis_prompt = backend.requests_messages[1][-1].content or ""
    assert '<candidate lens="strategy challenge">' in synthesis_prompt
    injected_prompt = backend.requests_messages[2][-1].content or ""
    assert "change the plan before taking the next action" in injected_prompt
    assert "PLAN: 1. Inspect constraints" in injected_prompt
    assert not any(
        "<vibe_deliberation>" in (message.content or "") for message in agent.messages
    )


@pytest.mark.asyncio
async def test_max_vibe_thinking_uses_distinct_strategy_lenses() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Audit the current direction")],
        [mock_llm_chunk(content="Compare different approaches")],
        [mock_llm_chunk(content="Forecast likely failure")],
        [mock_llm_chunk(content="Derive a constraint-first route")],
        [
            mock_llm_chunk(
                content=(
                    "DIRECTION: PIVOT\nPLAN: 1. Try the safer route\n"
                    "COMPLETION GATE: Verify it"
                )
            )
        ],
        [mock_llm_chunk(content="Final answer")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=False, vibe_thinking="max")
        ),
        backend=backend,
    )

    events = [event async for event in agent.act("Find the right approach")]

    reflection_prompts = [
        request[-1].content or "" for request in backend.requests_messages[:4]
    ]
    assert "direction audit" in reflection_prompts[0]
    assert "alternative search" in reflection_prompts[1]
    assert "failure rehearsal" in reflection_prompts[2]
    assert "constraint-first design" in reflection_prompts[3]
    assert all(
        "Previous brief to challenge and refine" not in prompt
        for prompt in reflection_prompts
    )
    assert all(model.thinking == "max" for model in backend.requests_models[:4])
    assert backend.requests_models[4].thinking == "off"
    assert backend.requests_max_tokens[:5] == [896, 896, 896, 896, 896]
    assert "Original user objective" in reflection_prompts[0]
    assert "Find the right approach" in reflection_prompts[0]
    system_contract = backend.requests_messages[0][0].content or ""
    assert "COMPETING ROUTES:" in system_contract
    assert "PIVOT TRIGGER:" in system_contract
    assert any(
        isinstance(event, ReasoningEvent)
        and event.status_text == "Vibe thinking 4/4 · constraint-first design"
        for event in events
    )
    synthesis_prompt = backend.requests_messages[4][-1].content or ""
    assert synthesis_prompt.count("<candidate lens=") == 4
    assert any(
        isinstance(event, ReasoningEvent)
        and event.status_text == "Vibe thinking · synthesizing 4 options"
        for event in events
    )


@pytest.mark.asyncio
async def test_autonomy_performance_profile_does_not_hide_max_vibe_thinking() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Direction audit")],
        [mock_llm_chunk(content="Alternative search")],
        [mock_llm_chunk(content="Failure rehearsal")],
        [mock_llm_chunk(content="Constraint-first design")],
        [mock_llm_chunk(content="DIRECTION: CONTINUE\nPLAN: 1. Answer")],
        [mock_llm_chunk(content="DIRECT")],
        [mock_llm_chunk(content="Final answer")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=True, vibe_thinking="max")
        ),
        backend=backend,
    )

    events = [event async for event in agent.act("Find the right approach")]

    statuses = [
        event.status_text
        for event in events
        if isinstance(event, ReasoningEvent)
        and event.status_text is not None
        and event.status_text.startswith("Vibe thinking ")
        and "/4" in event.status_text
    ]
    assert statuses == [
        "Vibe thinking 1/4 · direction audit",
        "Vibe thinking 2/4 · alternative search",
        "Vibe thinking 3/4 · failure rehearsal",
        "Vibe thinking 4/4 · constraint-first design",
    ]


@pytest.mark.asyncio
async def test_vibe_thinking_reconsiders_after_tool_results() -> None:
    tool_call = ToolCall(
        id="todo-read",
        index=0,
        function=FunctionCall(name="todo", arguments='{"action":"read"}'),
    )
    backend = FakeBackend([
        [mock_llm_chunk(content="Plan the first decision")],
        [mock_llm_chunk(content="DIRECTION: CONTINUE\nPLAN: 1. Read todos")],
        [mock_llm_chunk(content="Inspecting todos", tool_calls=[tool_call])],
        [
            mock_llm_chunk(
                content=(
                    "DIRECTION: CONTINUE\nEVIDENCE: todo state read\n"
                    "GAP: final report\nNEXT: report state"
                )
            )
        ],
        [mock_llm_chunk(content="Final answer")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            enabled_tools=["todo"],
            tools={"todo": {"permission": "always"}},
            autonomy=AutonomyConfig(enabled=False, vibe_thinking="low"),
        ),
        backend=backend,
    )

    events = [event async for event in agent.act("Read the todo state")]

    assert len(backend.requests_messages) == 5
    fast_check = backend.requests_messages[3]
    assert "tool:" in (fast_check[-1].content or "")
    assert backend.requests_max_tokens[3] == 192
    assert backend.requests_models[3].thinking == "low"
    vibe_reasoning_ids = {
        event.message_id
        for event in events
        if isinstance(event, ReasoningEvent)
        and event.message_id is not None
        and event.message_id.startswith("vibe-thinking-")
    }
    fast_reasoning_ids = {
        event.message_id
        for event in events
        if isinstance(event, ReasoningEvent)
        and event.message_id is not None
        and event.message_id.startswith("fast-thinking-")
    }
    assert len(vibe_reasoning_ids) == 1
    assert len(fast_reasoning_ids) == 1
    assert any(
        isinstance(event, ReasoningEvent)
        and event.status_text == "Fast thinking · self-check 1/10"
        for event in events
    )
    fast_summary = next(
        event
        for event in events
        if isinstance(event, ReasoningEvent)
        and event.status_text is not None
        and event.status_text.startswith("Fast check · CONTINUE")
    )
    assert "**Evidence:** todo state read" in fast_summary.content
    assert "**Missing proof:** final report" in fast_summary.content
    assert "**Next action:** report state" in fast_summary.content
    assert not any(
        "<vibe_deliberation>" in (message.content or "")
        or "<fast_thinking>" in (message.content or "")
        for message in agent.messages
    )


@pytest.mark.asyncio
async def test_full_vibe_thinking_runs_again_on_the_tenth_main_turn() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Direction audit")],
        [mock_llm_chunk(content="Alternative search")],
        [mock_llm_chunk(content="DIRECTION: PIVOT\nPLAN: 1. Change route")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=False, vibe_thinking="medium")
        ),
        backend=backend,
    )
    agent._vibe_turns_since_full = 10

    events = [
        event async for event in agent._run_vibe_deliberation("Complete the goal")
    ]

    assert len(backend.requests_messages) == 3
    assert agent._vibe_turns_since_full == 0
    assert [
        event.status_text
        for event in events
        if isinstance(event, ReasoningEvent)
        and event.status_text is not None
        and event.status_text.startswith("Vibe thinking")
        and "/2" in event.status_text
    ] == [
        "Vibe thinking 1/2 · direction audit",
        "Vibe thinking 2/2 · alternative search",
    ]


@pytest.mark.asyncio
async def test_tool_failure_escalates_fast_check_to_full_vibe_thinking() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Recheck direction")],
        [mock_llm_chunk(content="Try an alternative")],
        [mock_llm_chunk(content="DIRECTION: PIVOT\nPLAN: 1. Stop retrying")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=False, vibe_thinking="medium")
        ),
        backend=backend,
    )
    agent._vibe_turns_since_full = 2
    agent.messages.append(
        LLMMessage(
            role=Role.tool,
            name="bash",
            content="<tool_error>command timed out</tool_error>",
        )
    )

    events = [
        event async for event in agent._run_vibe_deliberation("Complete the goal")
    ]

    assert len(backend.requests_messages) == 3
    assert any(
        isinstance(event, ReasoningEvent)
        and event.status_text == "Vibe thinking 2/2 · alternative search"
        for event in events
    )
    assert not any(
        isinstance(event, ReasoningEvent)
        and event.status_text is not None
        and event.status_text.startswith("Fast thinking")
        for event in events
    )


def test_vibe_thinking_context_uses_bounded_fast_and_slow_memory() -> None:
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=False, vibe_thinking="medium")
        ),
        backend=FakeBackend([]),
    )
    agent.messages.append(
        LLMMessage(
            role=Role.system,
            content=(
                "# Global Memory\n<global_memory>server uses a pinned runtime"
                "</global_memory>"
            ),
            injected=True,
        )
    )
    agent.messages.append(
        build_working_memory_message(
            list(agent.messages),
            tool="bash",
            action="run focused tests",
            status="success",
            result="focused tests passed",
        )
    )
    agent.messages.append(
        LLMMessage(role=Role.tool, content="raw-output-" * 4_000, name="bash")
    )

    context = agent._vibe_deliberation_context("Continue safely", thinking="medium")

    assert len(context) <= 6_000
    assert "FAST WORKING MEMORY" in context
    assert "SLOW GLOBAL MEMORY" in context
    assert "server uses a pinned runtime" in context
    assert context.count("raw-output-") < 4_000


@pytest.mark.asyncio
async def test_fast_working_memory_survives_compaction_and_guides_next_turn() -> None:
    tool_call = ToolCall(
        id="todo-read",
        index=0,
        function=FunctionCall(name="todo", arguments='{"action":"read"}'),
    )
    backend = FakeBackend([
        [mock_llm_chunk(content="Inspecting", tool_calls=[tool_call])],
        [mock_llm_chunk(content="Todo state checked")],
        [mock_llm_chunk(content="<summary>Continue after inspection</summary>")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            enabled_tools=["todo"],
            tools={"todo": {"permission": "always"}},
            autonomy=AutonomyConfig(enabled=False),
        ),
        backend=backend,
    )

    _ = [event async for event in agent.act("Read the todo state")]

    working_memory = load_working_memory(agent.messages)
    assert len(working_memory.entries) == 1
    assert working_memory.entries[0].tool == "todo"
    assert working_memory.entries[0].status == "success"
    assert any(
        message.role is Role.system
        and "# Fast Working Memory" in (message.content or "")
        for message in backend.requests_messages[1]
    )

    await agent.compact()

    selected = select_model_context(agent.messages)
    assert (
        sum("# Fast Working Memory" in (message.content or "") for message in selected)
        == 1
    )
    assert any(message.context_boundary == "compaction" for message in selected)


@pytest.mark.asyncio
async def test_fast_working_memory_does_not_split_parallel_tool_results() -> None:
    tool_calls = [
        ToolCall(
            id=f"todo-read-{index}",
            index=index,
            function=FunctionCall(name="todo", arguments='{"action":"read"}'),
        )
        for index in range(2)
    ]
    backend = FakeBackend([
        [mock_llm_chunk(content="Inspecting", tool_calls=tool_calls)],
        [mock_llm_chunk(content="Both inspections complete")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            enabled_tools=["todo"],
            tools={"todo": {"permission": "always"}},
            autonomy=AutonomyConfig(enabled=False),
        ),
        backend=backend,
    )

    _ = [event async for event in agent.act("Inspect twice in parallel")]

    second_request = backend.requests_messages[1]
    tool_indexes = [
        index
        for index, message in enumerate(second_request)
        if message.role is Role.tool
    ]
    assert len(tool_indexes) == 2
    assert tool_indexes[1] == tool_indexes[0] + 1
    assert len(load_working_memory(agent.messages).entries) == 1


@pytest.mark.asyncio
async def test_global_memory_load_is_reported_once_per_session() -> None:
    await GlobalMemoryStore().remember("Prefer verified evidence")
    backend = FakeBackend([
        [mock_llm_chunk(content="First answer")],
        [mock_llm_chunk(content="Second answer")],
    ])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(autonomy=AutonomyConfig(enabled=False)),
        backend=backend,
    )

    first_events = [event async for event in agent.act("First request")]
    second_events = [event async for event in agent.act("Second request")]

    memory_statuses = [
        event
        for event in [*first_events, *second_events]
        if isinstance(event, MemoryStatusEvent) and event.status == "loaded"
    ]
    assert len(memory_statuses) == 1
    assert memory_statuses[0].status == "loaded"
    assert memory_statuses[0].persistent_entries == 1
    assert "1 persistent entries" in memory_statuses[0].message
    assert not any(
        isinstance(event, AssistantEvent | ReasoningEvent)
        and "memory" in event.content.casefold()
        for event in first_events
    )


@pytest.mark.asyncio
@respx.mock
async def test_max_web_search_runs_visible_search_before_main_model() -> None:
    route = respx.get("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<a class="result__a" href="https://example.com/docs">Docs</a>'
                '<a class="result__snippet">Current primary documentation</a>'
            ),
        )
    )
    backend = FakeBackend([[mock_llm_chunk(content="Verified answer")]])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=False, web_search_activity="max"),
            tools={"web_search": {"permission": "always", "engine": "public"}},
        ),
        backend=backend,
    )

    events = [event async for event in agent.act("latest framework docs")]

    assert route.called
    assert any(
        isinstance(event, ToolCallEvent)
        and isinstance(event.args, WebSearchArgs)
        and event.args.query == "latest framework docs"
        for event in events
    )
    assert any(
        isinstance(event, ToolResultEvent)
        and isinstance(event.result, WebSearchResult)
        and event.result.sources[0].url == "https://example.com/docs"
        for event in events
    )
    assert any(
        message.role is Role.tool and message.name == "web_search"
        for message in backend.requests_messages[0]
    )


@pytest.mark.asyncio
@respx.mock
async def test_max_web_search_keeps_local_infrastructure_objective_private() -> None:
    route = respx.get("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(200, text="")
    )
    backend = FakeBackend([[mock_llm_chunk(content="Inspecting local sources")]])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(enabled=False, web_search_activity="max"),
            tools={"web_search": {"permission": "always", "engine": "public"}},
        ),
        backend=backend,
    )
    objective = (
        "Проверь сервер 135.125.188.210:40050 через тесты из "
        r"C:\Users\me\Documents\GitHub\RUIP и прочитай "
        "codex://threads/01a0210a-cc16-7c21-9e72-873274ff37b9"
    )

    events = [event async for event in agent.act(objective)]

    assert not route.called
    assert not any(
        isinstance(event, ToolCallEvent) and isinstance(event.args, WebSearchArgs)
        for event in events
    )


@pytest.mark.parametrize(
    ("activity", "objective", "expected"),
    [
        ("low", "find the latest release", False),
        ("low", "search the web for release notes", True),
        ("low", "rename this local variable", False),
        ("medium", "check the current documentation", True),
        ("medium", "research API error handling", True),
        ("medium", "rename this local variable", False),
        ("high", "compare API approaches", True),
        ("high", "review this local implementation", True),
        ("max", "review this implementation", True),
        ("max", "hi", False),
    ],
)
def test_web_effort_levels_control_real_presearch_policy(
    activity: WebSearchActivity, objective: str, expected: bool
) -> None:
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(web_search_activity=activity)
        )
    )

    assert agent._should_presearch(objective, activity=activity) is expected


@pytest.mark.asyncio
@respx.mock
async def test_high_web_activity_searches_for_authoritative_recovery_after_failure(
    tmp_path: Path,
) -> None:
    route = respx.get("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<a class="result__a" href="https://example.com/primary-docs">'
                "Primary docs</a>"
                '<a class="result__snippet">Authoritative recovery steps</a>'
            ),
        )
    )
    agent = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(
                enabled=False, web_search_activity="high", personal_experience=False
            ),
            tools={"web_search": {"permission": "always", "engine": "public"}},
        ),
        cwd=tmp_path,
    )
    agent.messages.append(
        LLMMessage(
            role=Role.tool,
            name="git_bash",
            content="<tool_error>package resolver rejected the lockfile</tool_error>",
        )
    )

    events = [
        event
        async for event in agent._run_recovery_web_search("repair dependency build")
    ]

    assert route.called
    call = next(
        event
        for event in events
        if isinstance(event, ToolCallEvent) and isinstance(event.args, WebSearchArgs)
    )
    assert isinstance(call.args, WebSearchArgs)
    assert "primary documentation" in call.args.query
    assert "package resolver rejected" in call.args.query


class _FailAfterIntentBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__([[mock_llm_chunk(content="AUTONOMOUS")]])

    async def complete(self, **kwargs: Any) -> LLMChunk:
        if self.requests_messages:
            raise RuntimeError("interrupted backend")
        return await super().complete(**kwargs)


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
        yield TaskResult(
            response=response,
            turns_used=1,
            completed=True,
            evidence_tool_calls=1 if args.agent == "reviewer" else 0,
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


class _FailingAdvisorRunner(_AdvisorRunner):
    async def run(
        self, args: TaskArgs, _ctx: InvokeContext, *, max_result_chars: int = 0
    ):
        del max_result_chars
        self.calls.append(args)
        if args.agent == "goal-advisor":
            yield TaskResult(
                response="advisor API unavailable", turns_used=1, completed=False
            )
            return
        yield TaskResult(
            response="completed\nTASK_RESULT: PASS", turns_used=1, completed=True
        )


@pytest.mark.asyncio
async def test_computer_capability_question_skips_slow_autonomy_bootstrap() -> None:
    backend = _direct_backend([[mock_llm_chunk(content="Да, через computer_use.")]])
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
    assert len(backend.requests_messages) == 2
    assert backend.requests_max_tokens[0] == 16
    assert any(
        isinstance(event, AssistantEvent) and "computer_use" in event.content
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["hi", "Hello!", "привет", "спасибо", "ok"])
async def test_trivial_conversation_does_not_start_advisor_or_subagents(
    prompt: str,
) -> None:
    backend = _direct_backend([[mock_llm_chunk(content="Brief reply")]])
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
    assert len(backend.requests_messages) == 2


@pytest.mark.asyncio
async def test_natural_task_management_is_analyzed_then_sent_to_main_model() -> None:
    backend = _direct_backend([[mock_llm_chunk(content="Задачи убраны")]])
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

    _ = [event async for event in agent.act("убери все задачи", subagent_runner=runner)]

    assert runner.calls == []
    assert len(backend.requests_messages) == 2
    assert backend.requests_messages[1][-1].content == "убери все задачи"


@pytest.mark.asyncio
async def test_invalid_intent_response_fails_open_to_main_model() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="uncertain")],
        [mock_llm_chunk(content="Handled directly")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"], autonomy=AutonomyConfig(enabled=True)
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner()

    _ = [
        event async for event in agent.act("Ambiguous request", subagent_runner=runner)
    ]

    assert runner.calls == []
    assert len(backend.requests_messages) == 2


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
    backend = _autonomous_backend([
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


@pytest.mark.asyncio
async def test_failed_goal_advisor_falls_back_to_active_model_and_continues() -> None:
    backend = _autonomous_backend([
        [
            mock_llm_chunk(
                content=(
                    '<goal-plan>{"tasks":[{"id":"inspect",'
                    '"content":"Inspect local sources","agent":"worker",'
                    '"depends_on":[]}]}</goal-plan>'
                )
            )
        ],
        [mock_llm_chunk(content="Completed with local evidence")],
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
    runner = _FailingAdvisorRunner()

    events = [
        event
        async for event in agent.act(
            "Inspect the local infrastructure", subagent_runner=runner
        )
    ]

    assert [call.agent for call in runner.calls] == ["goal-advisor", "worker"]
    assert len(backend.requests_messages) == 3
    assert any(
        isinstance(event, AssistantEvent)
        and "active model fallback" in event.content
        and not event.stopped_by_middleware
        for event in events
    )
    assert not any(
        isinstance(event, AssistantEvent)
        and "Autonomous planning stopped" in event.content
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
    backend = _autonomous_backend([[mock_llm_chunk(content="Must not run")]])
    config = build_test_vibe_config(
        autonomy=AutonomyConfig(
            enabled=True, require_worker=False, require_review=False
        )
    )
    agent = build_test_agent_loop(config=config, backend=backend)

    events = [event async for event in agent.act("Do the work")]

    assert len(backend.requests_messages) == 1
    blocked = [
        event
        for event in events
        if isinstance(event, AssistantEvent) and event.stopped_by_middleware
    ]
    assert len(blocked) == 1
    assert "subagent runtime is unavailable" in blocked[0].content


@pytest.mark.asyncio
async def test_main_model_analyzes_intent_before_advisor_and_execution() -> None:
    backend = _autonomous_backend([
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
    intent_request = backend.requests_messages[0]
    assert backend.requests_tools[0] == []
    assert backend.requests_max_tokens[0] == 16
    assert "Classify whether" in (intent_request[0].content or "")
    first_request = backend.requests_messages[1]
    assert any(
        message.role is Role.tool
        and message.name == "task"
        and "<goal-plan>" in (message.content or "")
        for message in first_request
    )
    assert any(
        "Ready explore/worker tasks were delegated" in (message.content or "")
        for message in first_request
    )


@pytest.mark.asyncio
async def test_gauntlet_forces_autonomy_and_specializes_advisor() -> None:
    backend = FakeBackend([[mock_llm_chunk(content="Integrated winning result")]])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(
            enabled=True, gauntlet_loop=True, require_worker=False, require_review=False
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"build","content":"Build against the real bar","agent":"worker",'
        '"depends_on":[]}]}</goal-plan>'
    )

    _ = [
        event async for event in agent.act("Build the best CLI", subagent_runner=runner)
    ]

    assert len(backend.requests_messages) == 1
    assert runner.calls[0].agent == "goal-advisor"
    assert "named, fetchable, directly comparable quality bar" in runner.calls[0].task


@pytest.mark.asyncio
async def test_boost_runs_deep_planning_and_adversarial_review() -> None:
    backend = FakeBackend([
        [mock_llm_chunk(content="Direction audit")],
        [mock_llm_chunk(content="Alternative search")],
        [mock_llm_chunk(content="Failure rehearsal")],
        [mock_llm_chunk(content="Constraint-first design")],
        [mock_llm_chunk(content="DIRECTION: CONTINUE\nPLAN: 1. Integrate")],
        [mock_llm_chunk(content="AUTONOMOUS")],
        [mock_llm_chunk(content="Integrated verified result")],
    ])
    config = build_test_vibe_config(
        enabled_tools=["task", "todo"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["goal-advisor", "reviewer", "worker", "explore"],
            }
        },
        autonomy=AutonomyConfig(boost_mode=True),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"implement","content":"Implement and verify",'
        '"agent":"worker","depends_on":[]}]}</goal-plan>'
    )

    _ = [
        event
        async for event in agent.act(
            "Implement the verified migration", subagent_runner=runner
        )
    ]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "worker",
        "reviewer",
    ]
    assert "BOOST is enabled" in runner.calls[0].task
    assert "root Vibe Thinking preflight ran before delegation" in runner.calls[0].task
    assert "PLAN: 1. Integrate" in runner.calls[0].task
    assert "separate known facts from assumptions" in runner.calls[0].task
    assert "BOOST review is adversarial" in runner.calls[-1].task
    assert "challenge the chosen approach" in runner.calls[-1].task
    assert "private strategic reasoning layer" in (
        backend.requests_messages[0][0].content or ""
    )
    assert "compact decision synthesizer" in (
        backend.requests_messages[4][0].content or ""
    )
    assert backend.requests_max_tokens[5] == 16


@pytest.mark.asyncio
async def test_boost_keeps_a_trivial_greeting_direct_and_skips_extra_thinking() -> None:
    backend = _direct_backend([[mock_llm_chunk(content="Hello")]])
    agent = build_test_agent_loop(
        config=build_test_vibe_config(autonomy=AutonomyConfig(boost_mode=True)),
        backend=backend,
    )

    events = [event async for event in agent.act("hi")]

    assert len(backend.requests_messages) == 2
    assert not any(
        isinstance(event, ReasoningEvent)
        and event.status_text is not None
        and event.status_text.startswith("Vibe thinking")
        for event in events
    )


@pytest.mark.asyncio
async def test_autonomy_materializes_plan_and_delegates_before_main_model() -> None:
    backend = _autonomous_backend([
        [mock_llm_chunk(content="Integrated automatic work")]
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
    first_request = backend.requests_messages[1]
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
    backend = _autonomous_backend([
        [mock_llm_chunk(content="Integrated automatic work")]
    ])
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
        event
        async for event in agent.act(
            "Implement API integration and database tests", subagent_runner=runner
        )
    ]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "worker",
        "reviewer",
    ]
    assert [call.max_turns for call in runner.calls] == [4, 10, 5]
    assert [call.timeout_seconds for call in runner.calls] == [180, 720, 300]
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
async def test_mini_code_action_cannot_be_downgraded_past_full_pipeline() -> None:
    backend = _direct_backend([[mock_llm_chunk(content="Integrated mini fix")]])
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
        '<goal-plan>{"tasks":[{"id":"fix","content":"Fix and test typo",'
        '"agent":"worker","depends_on":[]}]}</goal-plan>'
    )

    _ = [
        event
        async for event in agent.act(
            "Исправь опечатку в README", subagent_runner=runner
        )
    ]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "worker",
        "reviewer",
    ]
    assert [call.max_turns for call in runner.calls] == [3, 6, 3]
    assert [call.timeout_seconds for call in runner.calls] == [120, 480, 180]


@pytest.mark.asyncio
async def test_medium_plan_dispatches_three_independent_workers_in_one_wave() -> None:
    backend = _autonomous_backend([
        [mock_llm_chunk(content="Integrated parallel changes")]
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
            max_parallel_subagents=3,
            require_worker=True,
            require_review=True,
        ),
    )
    agent = build_test_agent_loop(config=config, backend=backend)
    runner = _AdvisorRunner(
        '<goal-plan>{"tasks":['
        '{"id":"api","content":"Update API module","agent":"worker",'
        '"depends_on":[]},'
        '{"id":"ui","content":"Update UI module","agent":"worker",'
        '"depends_on":[]},'
        '{"id":"tests","content":"Update independent tests","agent":"worker",'
        '"depends_on":[]}]}</goal-plan>'
    )

    events = [
        event
        async for event in agent.act(
            "Добавь API, frontend и независимые тесты", subagent_runner=runner
        )
    ]

    assert [call.agent for call in runner.calls] == [
        "goal-advisor",
        "worker",
        "worker",
        "worker",
        "reviewer",
    ]
    worker_calls = [
        event
        for event in events
        if isinstance(event, ToolCallEvent)
        and isinstance(event.args, TaskArgs)
        and event.args.agent == "worker"
    ]
    assert len(worker_calls) == 3


@pytest.mark.asyncio
async def test_autonomy_compacts_repeated_objective_and_stored_worker_result() -> None:
    backend = _autonomous_backend([
        [mock_llm_chunk(content="Integrated automatic work")]
    ])
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
    backend = _autonomous_backend([[mock_llm_chunk(content="Integrated")]])
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
    backend = _autonomous_backend([[mock_llm_chunk(content="Must not run")]])
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
    assert len(backend.requests_messages) == 1
    assert any(
        isinstance(event, AssistantEvent)
        and event.stopped_by_middleware
        and "initial todo update failed" in event.content
        for event in events
    )


@pytest.mark.asyncio
async def test_worker_failure_does_not_complete_todo() -> None:
    backend = _autonomous_backend([
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
    backend = _autonomous_backend([
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
    assert len(backend.requests_messages) == 3
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
    agent = build_test_agent_loop(config=config, backend=_FailAfterIntentBackend())
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
    backend = _autonomous_backend([
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
        assert "## Fast batched execution" in system_prompt
        assert "same assistant response" in system_prompt
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
