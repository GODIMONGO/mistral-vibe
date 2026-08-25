from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibe.core.subagents import SwarmArgs, SwarmResult, TaskArgs, TaskResult
from vibe.core.tools.builtins.todo import TodoItem, TodoResult, TodoStatus
from vibe.core.tools.ui import ToolUIDataAdapter
from vibe.core.types import BaseEvent, ToolCallEvent, ToolResultEvent
from vibe.utils.tool_presentation import ToolEffectKind

GOAL_ADVISOR_AGENT = "goal-advisor"
REVIEWER_AGENT = "reviewer"
WORKER_AGENT = "worker"
REVIEW_PASS_MARKER = "VERDICT: PASS"
REVIEW_EVIDENCE_PREFIX = "EVIDENCE_CHECKED:"
AUTONOMY_PLAN_START = "<goal-plan>"
AUTONOMY_PLAN_END = "</goal-plan>"
MAX_AUTOMATIC_PLAN_TASKS = 16
MAX_AUTOMATIC_TASK_CHARS = 2_000
_MIN_INSTRUCTION_CHARS = 80

_MUTATION_EFFECTS = frozenset({
    ToolEffectKind.FILE_EDIT,
    ToolEffectKind.FILE_WRITE,
    ToolEffectKind.SHELL,
    ToolEffectKind.WORKTREE,
})


class AutonomyPlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    content: str = Field(min_length=1, max_length=MAX_AUTOMATIC_TASK_CHARS)
    agent: str = Field(pattern=r"^(explore|worker)$")
    depends_on: list[str] = Field(default_factory=list)


class AutonomyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[AutonomyPlanTask] = Field(
        min_length=1, max_length=MAX_AUTOMATIC_PLAN_TASKS
    )

    @model_validator(mode="after")
    def validate_graph(self) -> AutonomyPlan:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("plan task IDs must be unique")
        known = set(ids)
        graph = {task.id: task.depends_on for task in self.tasks}
        for task in self.tasks:
            if task.id in task.depends_on:
                raise ValueError(f"plan task '{task.id}' cannot depend on itself")
            if unknown := set(task.depends_on) - known:
                names = ", ".join(sorted(unknown))
                raise ValueError(
                    f"plan task '{task.id}' has unknown dependencies: {names}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("plan dependency graph contains a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in graph:
            visit(task_id)
        return self

    def to_todos(self) -> list[TodoItem]:
        return [
            TodoItem(id=task.id, content=task.content, depends_on=task.depends_on)
            for task in self.tasks
        ]


def parse_advisor_plan(response: str, objective: str) -> AutonomyPlan:
    start = response.find(AUTONOMY_PLAN_START)
    end = response.find(AUTONOMY_PLAN_END, start + len(AUTONOMY_PLAN_START))
    if start >= 0 and end > start:
        payload = response[start + len(AUTONOMY_PLAN_START) : end].strip()
        try:
            return AutonomyPlan.model_validate(json.loads(payload))
        except (json.JSONDecodeError, ValueError):
            pass

    fallback = objective.strip()[:MAX_AUTOMATIC_TASK_CHARS]
    if not fallback:
        fallback = "Complete and verify the user's objective"
    return AutonomyPlan(
        tasks=[AutonomyPlanTask(id="execute-goal", content=fallback, agent="worker")]
    )


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

    def seed_advisor_completed(self) -> None:
        self._advisor_completed = True
        self._latest_required_sequence = self._sequence

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

    def should_run_reviewer(self) -> bool:
        if not self.policy.require_review or not self._advisor_completed:
            return False
        if self._todos is None or not self._todos.todos:
            return False
        if any(
            todo.status not in {TodoStatus.COMPLETED, TodoStatus.CANCELLED}
            for todo in self._todos.todos
        ):
            return False
        if self.policy.require_worker and not self._worker_completed:
            return False
        return not self._reviewer_passed or (
            self._reviewer_sequence <= self._latest_required_sequence
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
                lines = [
                    line.strip()
                    for line in result.response.splitlines()
                    if line.strip()
                ]
                checked_evidence = any(
                    line.startswith(REVIEW_EVIDENCE_PREFIX)
                    and line.removeprefix(REVIEW_EVIDENCE_PREFIX).strip()
                    for line in lines
                )
                self._reviewer_passed = (
                    bool(lines) and lines[-1] == REVIEW_PASS_MARKER and checked_evidence
                )

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
                reasons.append("reviewer did not return evidence-backed VERDICT: PASS")
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
    "AUTONOMY_PLAN_END",
    "AUTONOMY_PLAN_START",
    "GOAL_ADVISOR_AGENT",
    "REVIEWER_AGENT",
    "REVIEW_PASS_MARKER",
    "WORKER_AGENT",
    "AutonomyCoordinator",
    "AutonomyDecision",
    "AutonomyDecisionKind",
    "AutonomyPlan",
    "AutonomyPlanTask",
    "AutonomyPolicy",
    "parse_advisor_plan",
]
