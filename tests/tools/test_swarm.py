from __future__ import annotations

import pytest

from tests.conftest import ConfigBuilder, OrchestratorLoader
from tests.mock.utils import collect_result
from vibe.core.agents.manager import AgentManager
from vibe.core.config import VibeConfigSchema
from vibe.core.subagents import SwarmArgs, SwarmResult, TaskArgs, TaskResult
from vibe.core.tools.base import BaseToolState, InvokeContext, ToolError, ToolPermission
from vibe.core.tools.builtins.swarm import Swarm, SwarmConfig, SwarmToolResult
from vibe.core.tools.permissions import PermissionContext
from vibe.core.types import ToolStreamEvent


class FakeSwarmRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[TaskArgs], int, int]] = []

    async def run(
        self, args: TaskArgs, ctx: InvokeContext, *, max_result_chars: int = 0
    ):
        if False:
            yield TaskResult(response="", turns_used=0, completed=False)
        return

    async def run_many(
        self,
        args: list[TaskArgs],
        ctx: InvokeContext,
        *,
        max_parallel: int,
        max_result_chars: int = 0,
    ):
        self.calls.append((args, max_parallel, max_result_chars))
        yield ToolStreamEvent(
            tool_name="swarm", message="progress", tool_call_id=ctx.tool_call_id
        )
        results = [
            TaskResult(response=task.task, turns_used=1, completed=True)
            for task in args
        ]
        yield SwarmResult(results=results, completed_count=len(results))


@pytest.fixture
def swarm_tool() -> Swarm:
    config = SwarmConfig(max_parallel=8, max_result_chars=1234)
    return Swarm(config_getter=lambda: config, state=BaseToolState())


@pytest.fixture
def swarm_ctx(
    build_config: ConfigBuilder, load_orchestrator: OrchestratorLoader[VibeConfigSchema]
) -> InvokeContext:
    manager = AgentManager(load_orchestrator(build_config()))
    return InvokeContext(
        tool_call_id="swarm-1",
        session_id="parent-1",
        agent_manager=manager,
        subagent_runner=FakeSwarmRunner(),
    )


def test_safe_swarm_is_auto_allowed(swarm_tool: Swarm) -> None:
    result = swarm_tool.resolve_permission(
        SwarmArgs(tasks=[TaskArgs(task="inspect", agent="explore")])
    )

    assert isinstance(result, PermissionContext)
    assert result.permission is ToolPermission.ALWAYS


@pytest.mark.asyncio
async def test_swarm_streams_progress_and_returns_typed_result(
    swarm_tool: Swarm, swarm_ctx: InvokeContext
) -> None:
    args = SwarmArgs(
        tasks=[TaskArgs(task="first"), TaskArgs(task="second", agent="explore")]
    )

    events = [event async for event in swarm_tool.run(args, swarm_ctx)]

    assert isinstance(events[0], ToolStreamEvent)
    assert events[0].message == "progress"
    assert events[1] == SwarmToolResult(
        results=[
            TaskResult(response="first", turns_used=1, completed=True),
            TaskResult(response="second", turns_used=1, completed=True),
        ],
        completed_count=2,
    )
    runner = swarm_ctx.subagent_runner
    assert isinstance(runner, FakeSwarmRunner)
    assert runner.calls == [(args.tasks, 2, 1234)]


@pytest.mark.asyncio
async def test_swarm_rejects_mutating_or_primary_agents(
    swarm_tool: Swarm, swarm_ctx: InvokeContext
) -> None:
    with pytest.raises(ToolError, match="not allowed"):
        await collect_result(
            swarm_tool.run(
                SwarmArgs(tasks=[TaskArgs(task="edit", agent="worker")]), swarm_ctx
            )
        )


@pytest.mark.asyncio
async def test_swarm_enforces_task_count(swarm_ctx: InvokeContext) -> None:
    config = SwarmConfig(max_tasks=1)
    tool = Swarm(config_getter=lambda: config, state=BaseToolState())

    with pytest.raises(ToolError, match="at most 1"):
        await collect_result(
            tool.run(
                SwarmArgs(tasks=[TaskArgs(task="one"), TaskArgs(task="two")]), swarm_ctx
            )
        )


@pytest.mark.asyncio
async def test_zero_effort_disables_swarm(
    swarm_tool: Swarm,
    swarm_ctx: InvokeContext,
    build_config: ConfigBuilder,
    load_orchestrator: OrchestratorLoader[VibeConfigSchema],
) -> None:
    swarm_ctx.agent_manager = AgentManager(
        load_orchestrator(build_config(autonomy={"max_parallel_subagents": 0}))
    )

    with pytest.raises(ToolError, match="Subagents are disabled"):
        await collect_result(
            swarm_tool.run(
                SwarmArgs(tasks=[TaskArgs(task="inspect", agent="explore")]), swarm_ctx
            )
        )
