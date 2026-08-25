from __future__ import annotations

from pydantic import BaseModel

from vibe.core.autonomy import AutonomyCoordinator, AutonomyDecisionKind, AutonomyPolicy
from vibe.core.subagents import SwarmArgs, SwarmResult, TaskArgs, TaskResult
from vibe.core.tools.builtins.swarm import Swarm
from vibe.core.tools.builtins.task import Task
from vibe.core.tools.builtins.todo import (
    Todo,
    TodoItem,
    TodoPriority,
    TodoResult,
    TodoStatus,
)
from vibe.core.tools.builtins.write_file import WriteFile
from vibe.core.types import ToolCallEvent, ToolResultEvent


class _MutationResult(BaseModel):
    changed: bool = True


def _task_call(call_id: str, agent: str) -> ToolCallEvent:
    return ToolCallEvent(
        tool_call_id=call_id,
        tool_name="task",
        tool_class=Task,
        args=TaskArgs(task=f"Run {agent}", agent=agent),
    )


def _task_result(call_id: str, response: str = "done") -> ToolResultEvent:
    return ToolResultEvent(
        tool_call_id=call_id,
        tool_name="task",
        tool_class=Task,
        result=TaskResult(response=response, turns_used=1, completed=True),
    )


def _terminal_todos() -> ToolResultEvent:
    return ToolResultEvent(
        tool_call_id="todo-1",
        tool_name="todo",
        tool_class=Todo,
        result=TodoResult(
            verb="Updated",
            todos=[
                TodoItem(
                    id="implement",
                    content="Implement and verify",
                    status=TodoStatus.COMPLETED,
                    priority=TodoPriority.HIGH,
                )
            ],
            total_count=1,
        ),
    )


def _observe_successful_advisor(coordinator: AutonomyCoordinator) -> None:
    coordinator.observe(_task_call("advisor-1", "goal-advisor"))
    coordinator.observe(_task_result("advisor-1"))


def _observe_passing_reviewer(coordinator: AutonomyCoordinator) -> None:
    coordinator.observe(_task_call("reviewer-1", "reviewer"))
    coordinator.observe(_task_result("reviewer-1", "All checks passed\nVERDICT: PASS"))


def test_completion_passes_with_advisor_terminal_plan_and_fresh_review() -> None:
    coordinator = AutonomyCoordinator()
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())
    coordinator.observe(
        ToolCallEvent(
            tool_call_id="write-1", tool_name="write_file", tool_class=WriteFile
        )
    )
    coordinator.observe(
        ToolResultEvent(
            tool_call_id="write-1",
            tool_name="write_file",
            tool_class=WriteFile,
            result=_MutationResult(),
        )
    )
    _observe_passing_reviewer(coordinator)

    assert coordinator.evaluate_completion().kind is AutonomyDecisionKind.PASS


def test_mutation_after_review_requires_a_fresh_reviewer() -> None:
    coordinator = AutonomyCoordinator()
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())
    _observe_passing_reviewer(coordinator)
    coordinator.observe(
        ToolCallEvent(
            tool_call_id="write-1", tool_name="write_file", tool_class=WriteFile
        )
    )
    coordinator.observe(
        ToolResultEvent(
            tool_call_id="write-1",
            tool_name="write_file",
            tool_class=WriteFile,
            result=_MutationResult(),
        )
    )

    decision = coordinator.evaluate_completion()

    assert decision.kind is AutonomyDecisionKind.RETRY
    assert "reviewer did not return VERDICT: PASS" in decision.reasons


def test_retry_budget_ends_in_a_bounded_terminal_block() -> None:
    coordinator = AutonomyCoordinator(
        AutonomyPolicy(max_retries=1, max_instruction_chars=100)
    )
    coordinator.start_turn()

    retry = coordinator.evaluate_completion()
    blocked = coordinator.evaluate_completion()

    assert retry.kind is AutonomyDecisionKind.RETRY
    assert blocked.kind is AutonomyDecisionKind.BLOCK
    assert blocked.terminal is True
    assert blocked.instruction is not None
    assert len(blocked.instruction) <= 100


def test_required_worker_must_complete_successfully() -> None:
    coordinator = AutonomyCoordinator(AutonomyPolicy(require_worker=True))
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())
    _observe_passing_reviewer(coordinator)

    missing = coordinator.evaluate_completion()
    coordinator.observe(_task_call("worker-1", "worker"))
    coordinator.observe(_task_result("worker-1"))

    assert "a worker has not completed successfully" in missing.reasons
    stale_review = coordinator.evaluate_completion()
    assert "reviewer did not return VERDICT: PASS" in stale_review.reasons
    _observe_passing_reviewer(coordinator)
    assert coordinator.evaluate_completion().kind is AutonomyDecisionKind.PASS


def test_review_can_be_disabled_without_weakening_other_requirements() -> None:
    coordinator = AutonomyCoordinator(AutonomyPolicy(require_review=False))
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())

    assert coordinator.evaluate_completion().kind is AutonomyDecisionKind.PASS


def test_reviewer_launched_concurrently_with_worker_is_stale() -> None:
    coordinator = AutonomyCoordinator(AutonomyPolicy(require_worker=True))
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())
    coordinator.observe(_task_call("reviewer-1", "reviewer"))
    coordinator.observe(_task_call("worker-1", "worker"))
    coordinator.observe(_task_result("worker-1"))
    coordinator.observe(_task_result("reviewer-1", "VERDICT: PASS"))

    decision = coordinator.evaluate_completion()

    assert "reviewer result predates the latest required work" in decision.reasons


def test_non_terminal_todo_prevents_completion() -> None:
    coordinator = AutonomyCoordinator()
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(
        ToolResultEvent(
            tool_call_id="todo-1",
            tool_name="todo",
            tool_class=Todo,
            result=TodoResult(
                verb="Updated",
                todos=[TodoItem(id="verify", content="Run tests")],
                total_count=1,
            ),
        )
    )
    _observe_passing_reviewer(coordinator)

    decision = coordinator.evaluate_completion()

    assert "the plan/todo list still has non-terminal items" in decision.reasons


def test_swarm_advisor_and_reviewer_count_toward_completion() -> None:
    coordinator = AutonomyCoordinator()
    coordinator.start_turn()
    swarm_args = SwarmArgs(tasks=[TaskArgs(task="Advise", agent="goal-advisor")])
    coordinator.observe(
        ToolCallEvent(
            tool_call_id="swarm-1", tool_name="swarm", tool_class=Swarm, args=swarm_args
        )
    )
    coordinator.observe(
        ToolResultEvent(
            tool_call_id="swarm-1",
            tool_name="swarm",
            tool_class=Swarm,
            result=SwarmResult(
                results=[TaskResult(response="criteria", turns_used=1, completed=True)],
                completed_count=1,
            ),
        )
    )
    coordinator.observe(_terminal_todos())
    reviewer_args = SwarmArgs(tasks=[TaskArgs(task="Review", agent="reviewer")])
    coordinator.observe(
        ToolCallEvent(
            tool_call_id="swarm-2",
            tool_name="swarm",
            tool_class=Swarm,
            args=reviewer_args,
        )
    )
    coordinator.observe(
        ToolResultEvent(
            tool_call_id="swarm-2",
            tool_name="swarm",
            tool_class=Swarm,
            result=SwarmResult(
                results=[
                    TaskResult(response="VERDICT: PASS", turns_used=1, completed=True)
                ],
                completed_count=1,
            ),
        )
    )

    assert coordinator.evaluate_completion().kind is AutonomyDecisionKind.PASS
