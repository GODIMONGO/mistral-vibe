from __future__ import annotations

from collections.abc import AsyncGenerator
import fnmatch

from pydantic import Field, computed_field, field_validator

from vibe.core.agents.models import AgentType
from vibe.core.subagents import MIN_SUBAGENT_RESULT_MAX_CHARS, SwarmArgs, SwarmResult
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.permissions import PermissionContext
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import ToolCallEvent, ToolResultEvent, ToolStreamEvent
from vibe.utils.tool_presentation import ToolEffectKind

SAFE_SWARM_AGENTS = frozenset({"explore", "goal-advisor", "reviewer"})


class SwarmToolResult(SwarmResult):
    @computed_field
    @property
    def total_count(self) -> int:
        return len(self.results)


class SwarmConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    max_tasks: int = Field(default=16, ge=1, le=64)
    max_parallel: int = Field(default=4, ge=1, le=16)
    max_result_chars: int = Field(
        default=0,
        ge=0,
        description="Per-agent result limit; zero uses the autonomy setting",
    )

    @field_validator("max_result_chars")
    @classmethod
    def validate_result_limit(cls, value: int) -> int:
        if 0 < value < MIN_SUBAGENT_RESULT_MAX_CHARS:
            raise ValueError(
                f"max_result_chars must be zero or at least "
                f"{MIN_SUBAGENT_RESULT_MAX_CHARS}"
            )
        return value


class Swarm(
    BaseTool[SwarmArgs, SwarmToolResult, SwarmConfig, BaseToolState],
    ToolUIData[SwarmArgs, SwarmToolResult],
):
    effect_kind = ToolEffectKind.TOOL

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        count = len(event.args.tasks) if isinstance(event.args, SwarmArgs) else 0
        return ToolCallDisplay(
            summary=f"Running swarm of {count} agents",
            verb="Running",
            message=f"{count} read-only agents",
            settled_verb="Ran",
            settled_message=f"{count} read-only agents",
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        result = event.result
        if not isinstance(result, SwarmToolResult):
            return ToolResultDisplay(success=True, verb="Completed", message="swarm")
        return ToolResultDisplay(
            success=result.completed_count == result.total_count,
            verb="Completed",
            message=f"{result.completed_count}/{result.total_count} swarm tasks",
        )

    @classmethod
    def get_status_text(cls) -> str:
        return "Running swarm"

    def resolve_permission(self, args: SwarmArgs) -> PermissionContext | None:
        agents = [task.agent for task in args.tasks]
        if any(
            fnmatch.fnmatch(agent, pattern)
            for agent in agents
            for pattern in self.config.denylist
        ):
            return PermissionContext(permission=ToolPermission.NEVER)
        if all(agent in SAFE_SWARM_AGENTS for agent in agents):
            return PermissionContext(permission=ToolPermission.ALWAYS)
        return None

    async def run(
        self, args: SwarmArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | SwarmToolResult, None]:
        if ctx is None or ctx.agent_manager is None:
            raise ToolError("Swarm tool requires agent_manager in context")
        if ctx.subagent_runner is None:
            raise ToolError("Swarm tool requires a subagent runner in context")
        if ctx.agent_manager.active_profile.agent_type is AgentType.SUBAGENT:
            raise ToolError("Agent depth limit of 1 reached")
        if len(args.tasks) > self.config.max_tasks:
            raise ToolError(f"Swarm supports at most {self.config.max_tasks} tasks")

        for task in args.tasks:
            if task.agent not in SAFE_SWARM_AGENTS:
                allowed = ", ".join(sorted(SAFE_SWARM_AGENTS))
                raise ToolError(
                    f"Agent '{task.agent}' is not allowed in a swarm. Allowed: {allowed}"
                )
            try:
                profile = ctx.agent_manager.get_agent(task.agent)
            except ValueError as exc:
                raise ToolError(f"Unknown agent: {task.agent}") from exc
            if profile.agent_type is not AgentType.SUBAGENT:
                raise ToolError(f"Agent '{task.agent}' is not a subagent")

        autonomy = getattr(ctx.agent_manager.config, "autonomy", None)
        autonomy_parallel = getattr(autonomy, "effective_parallel_subagents", None)
        max_parallel = self.config.max_parallel
        if isinstance(autonomy_parallel, int) and autonomy_parallel > 0:
            max_parallel = min(max_parallel, autonomy_parallel)

        async for event in ctx.subagent_runner.run_many(
            args.tasks,
            ctx,
            max_parallel=max_parallel,
            max_result_chars=self.config.max_result_chars,
        ):
            if isinstance(event, SwarmResult):
                yield SwarmToolResult.model_validate(event.model_dump())
            else:
                yield event
