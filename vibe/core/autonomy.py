from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from vibe.core.subagents import SwarmArgs, SwarmResult, TaskArgs, TaskResult
from vibe.core.tools.builtins.todo import TodoResult, TodoStatus
from vibe.core.tools.ui import ToolUIDataAdapter
from vibe.core.types import BaseEvent, ToolCallEvent, ToolResultEvent
from vibe.utils.tool_presentation import ToolEffectKind

GOAL_ADVISOR_AGENT = "goal-advisor"
REVIEWER_AGENT = "reviewer"
WORKER_AGENT = "worker"
REVIEW_PASS_MARKER = "VERDICT: PASS"
_MIN_INSTRUCTION_CHARS = 80

_MUTATION_EFFECTS = frozenset({
    ToolEffectKind.FILE_EDIT,
    ToolEffectKind.FILE_WRITE,
    ToolEffectKind.SHELL,
    ToolEffectKind.WORKTREE,
})


class AutonomyDecisionKind(StrEnum):
    PASS = auto()
    RETRY = auto()
    BLOCK = auto()


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    max_retries: int = 3
    max_instruction_chars: int = 1_200
    require_worker: bool = False
    require_review: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.max_instruction_chars < _MIN_INSTRUCTION_CHARS:
            raise ValueError("max_instruction_chars must be at least 80")


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    kind: AutonomyDecisionKind
    instruction: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.kind is not AutonomyDecisionKind.RETRY


@dataclass(slots=True)
class _TaskCall:
    agent: str
    sequence: int


@dataclass(slots=True)
class _SwarmCall:
    agents: tuple[str, ...]
    sequence: int


class AutonomyCoordinator:
    def __init__(self, policy: AutonomyPolicy | None = None) -> None:
        self.policy = policy or AutonomyPolicy()
        self.turn_number = 0
        self._retry_count = 0
        self._sequence = 0
        self._task_calls: dict[str, _TaskCall] = {}
        self._swarm_calls: dict[str, _SwarmCall] = {}
        self._mutation_calls: set[str] = set()
        self._advisor_completed = False
        self._worker_completed = False
        self._todos: TodoResult | None = None
        self._latest_required_sequence = -1
        self._reviewer_sequence = -1
        self._reviewer_passed = False

    @property
    def retry_count(self) -> int:
        return self._retry_count

    def start_turn(self) -> None:
        self.turn_number += 1
        self._retry_count = 0
        self._sequence = 0
        self._task_calls.clear()
        self._swarm_calls.clear()
        self._mutation_calls.clear()
        self._advisor_completed = False
        self._worker_completed = False
        self._todos = None
        self._latest_required_sequence = -1
        self._reviewer_sequence = -1
        self._reviewer_passed = False

    def observe(self, event: BaseEvent) -> None:
        self._sequence += 1
        match event:
            case ToolCallEvent():
                self._observe_call(event)
            case ToolResultEvent():
                self._observe_result(event)

    def evaluate_completion(self) -> AutonomyDecision:
        reasons = self._incomplete_reasons()
        if not reasons:
            return AutonomyDecision(AutonomyDecisionKind.PASS)

        if self._retry_count >= self.policy.max_retries:
            return AutonomyDecision(
                AutonomyDecisionKind.BLOCK,
                instruction=self._bounded_instruction(
                    "Autonomous completion blocked after the retry budget was "
                    "exhausted. Report the unresolved requirements without "
                    "claiming completion.",
                    reasons,
                ),
                reasons=reasons,
            )

        self._retry_count += 1
        return AutonomyDecision(
            AutonomyDecisionKind.RETRY,
            instruction=self._bounded_instruction(
                "Continue working. Resolve every requirement below, then run a "
                "fresh reviewer after the last mutation before finishing.",
                reasons,
            ),
            reasons=reasons,
        )

    def _observe_call(self, event: ToolCallEvent) -> None:
        if isinstance(event.args, TaskArgs):
            self._task_calls[event.tool_call_id] = _TaskCall(
                agent=event.args.agent, sequence=self._sequence
            )
        elif isinstance(event.args, SwarmArgs):
            self._swarm_calls[event.tool_call_id] = _SwarmCall(
                agents=tuple(task.agent for task in event.args.tasks),
                sequence=self._sequence,
            )
        if ToolUIDataAdapter(event.tool_class).effect_kind in _MUTATION_EFFECTS:
            self._mutation_calls.add(event.tool_call_id)

    def _observe_result(self, event: ToolResultEvent) -> None:
        if isinstance(event.result, TodoResult) and self._result_succeeded(event):
            self._todos = event.result
            self._latest_required_sequence = self._sequence
            self._reviewer_passed = False

        if event.tool_call_id in self._mutation_calls and self._result_succeeded(event):
            self._latest_required_sequence = self._sequence
            self._reviewer_passed = False

        task_call = self._task_calls.get(event.tool_call_id)
        if task_call is not None and isinstance(event.result, TaskResult):
            if self._result_succeeded(event):
                self._observe_subagent_result(
                    task_call.agent, task_call.sequence, self._sequence, event.result
                )
            return

        swarm_call = self._swarm_calls.get(event.tool_call_id)
        if swarm_call is None or not isinstance(event.result, SwarmResult):
            return
        if not self._result_succeeded(event):
            return
        for agent, result in zip(swarm_call.agents, event.result.results, strict=False):
            self._observe_subagent_result(
                agent, swarm_call.sequence, self._sequence, result
            )

    def _observe_subagent_result(
        self, agent: str, call_sequence: int, result_sequence: int, result: TaskResult
    ) -> None:
        if not result.completed:
            return
        match agent:
            case "goal-advisor":
                self._advisor_completed = True
                self._latest_required_sequence = result_sequence
                self._reviewer_passed = False
            case "worker":
                self._worker_completed = True
                self._latest_required_sequence = result_sequence
                self._reviewer_passed = False
            case "reviewer":
                self._reviewer_sequence = call_sequence
                self._reviewer_passed = REVIEW_PASS_MARKER in result.response

    @staticmethod
    def _result_succeeded(event: ToolResultEvent) -> bool:
        return not (event.error or event.skipped or event.cancelled)

    def _incomplete_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self._advisor_completed:
            reasons.append("goal advisor has not completed successfully")
        if self._todos is None or not self._todos.todos:
            reasons.append("the plan/todo list is missing")
        elif any(
            todo.status not in {TodoStatus.COMPLETED, TodoStatus.CANCELLED}
            for todo in self._todos.todos
        ):
            reasons.append("the plan/todo list still has non-terminal items")
        if self.policy.require_worker and not self._worker_completed:
            reasons.append("a worker has not completed successfully")
        if self.policy.require_review:
            if not self._reviewer_passed:
                reasons.append("reviewer did not return VERDICT: PASS")
            elif self._reviewer_sequence <= self._latest_required_sequence:
                reasons.append("reviewer result predates the latest required work")
        return tuple(reasons)

    def _bounded_instruction(self, heading: str, reasons: tuple[str, ...]) -> str:
        instruction = heading + "\n" + "\n".join(f"- {reason}" for reason in reasons)
        limit = self.policy.max_instruction_chars
        if len(instruction) <= limit:
            return instruction
        marker = "\n[retry instruction truncated]"
        return instruction[: limit - len(marker)].rstrip() + marker


__all__ = [
    "GOAL_ADVISOR_AGENT",
    "REVIEWER_AGENT",
    "REVIEW_PASS_MARKER",
    "WORKER_AGENT",
    "AutonomyCoordinator",
    "AutonomyDecision",
    "AutonomyDecisionKind",
    "AutonomyPolicy",
]
