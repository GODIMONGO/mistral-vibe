from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibe.core.tools.ui import ToolUIDataAdapter
from vibe.core.types import AssistantEvent, BaseEvent, ToolResultEvent, ToolStreamEvent

if TYPE_CHECKING:
    from vibe.core.tools.base import InvokeContext


class TaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(description="The task for the agent to perform")
    agent: str = Field(
        default="explore",
        description="The type of specialized subagent to use for this task",
    )


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: str = Field(description="The accumulated response from the subagent")
    turns_used: int = Field(description="Number of turns the subagent used")
    completed: bool = Field(description="Whether the task completed normally")
    truncated: bool = Field(
        default=False, description="Whether the response was truncated to its budget"
    )
    original_chars: int = Field(
        default=0, ge=0, description="Response length before bounded accumulation"
    )

    @model_validator(mode="after")
    def infer_untruncated_length(self) -> TaskResult:
        if self.original_chars == 0 and self.response and not self.truncated:
            self.original_chars = len(self.response)
        return self


class SwarmArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskArgs] = Field(
        min_length=1, description="Independent read-only tasks to run concurrently"
    )


class SwarmResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[TaskResult]
    completed_count: int


class SubagentRunnerPort(Protocol):
    def run(
        self, args: TaskArgs, ctx: InvokeContext, *, max_result_chars: int = 0
    ) -> AsyncGenerator[ToolStreamEvent | TaskResult, None]: ...

    def run_many(
        self,
        args: list[TaskArgs],
        ctx: InvokeContext,
        *,
        max_parallel: int,
        max_result_chars: int = 0,
    ) -> AsyncGenerator[ToolStreamEvent | SwarmResult, None]: ...


DEFAULT_SUBAGENT_RESULT_MAX_CHARS = 32_768
MIN_SUBAGENT_RESULT_MAX_CHARS = 1_024
SUBAGENT_TRUNCATION_MARKER = "\n\n[... subagent response truncated ...]\n\n"


@dataclass(slots=True)
class SubagentRunAccumulator:
    max_chars: int = DEFAULT_SUBAGENT_RESULT_MAX_CHARS
    _head: str = ""
    _tail: str = ""
    _original_chars: int = 0
    _truncated: bool = False
    _completed: bool = True

    def __post_init__(self) -> None:
        if self.max_chars <= len(SUBAGENT_TRUNCATION_MARKER):
            raise ValueError("max_chars must fit the subagent truncation marker")

    def observe(self, event: BaseEvent, *, tool_call_id: str) -> ToolStreamEvent | None:
        if isinstance(event, AssistantEvent):
            if event.content:
                self._append(event.content)
            if event.stopped_by_middleware:
                self._completed = False
            return None
        if not isinstance(event, ToolResultEvent):
            return None
        if event.skipped:
            self._completed = False
            return None
        if event.result is None or event.tool_class is None:
            return None
        if event.presentation is not None:
            display = event.presentation.display
        else:
            display = ToolUIDataAdapter(event.tool_class).get_result_display(event)
        return ToolStreamEvent(
            tool_name="task",
            message=f"{event.tool_name}: {display.text}",
            tool_call_id=tool_call_id,
        )

    def record_error(self, message: str) -> None:
        self._completed = False
        self._append(f"\n[Subagent error: {message}]")

    def build_result(self, *, turns_used: int, completed: bool = True) -> TaskResult:
        return TaskResult(
            response=self._bounded_response(),
            turns_used=turns_used,
            completed=self._completed and completed,
            truncated=self._truncated,
            original_chars=self._original_chars,
        )

    def _append(self, content: str) -> None:
        self._original_chars += len(content)
        if not self._truncated and len(self._head) + len(content) <= self.max_chars:
            self._head += content
            return

        tail_chars = self._tail_chars
        if not self._truncated:
            previous = self._head
            head_chars = self.max_chars - len(SUBAGENT_TRUNCATION_MARKER) - tail_chars
            needed = max(0, head_chars - len(previous))
            self._head = (previous + content[:needed])[:head_chars]
            self._tail = self._ending(previous, content, tail_chars)
            self._truncated = True
            return

        if len(content) >= tail_chars:
            self._tail = content[-tail_chars:]
            return
        self._tail = (self._tail[-(tail_chars - len(content)) :] + content)[
            -tail_chars:
        ]

    @property
    def _tail_chars(self) -> int:
        available = self.max_chars - len(SUBAGENT_TRUNCATION_MARKER)
        return available // 2

    @staticmethod
    def _ending(previous: str, content: str, limit: int) -> str:
        if len(content) >= limit:
            return content[-limit:]
        return previous[-(limit - len(content)) :] + content

    def _bounded_response(self) -> str:
        if not self._truncated:
            return self._head
        return self._head + SUBAGENT_TRUNCATION_MARKER + self._tail


def prepare_subagent_prompt(task: str, ctx: InvokeContext) -> str:
    if ctx.scratchpad_dir is None:
        return task
    return (
        f"Scratchpad directory: {ctx.scratchpad_dir}\n"
        "You can read and write files here without permission prompts.\n\n"
        f"{task}"
    )
