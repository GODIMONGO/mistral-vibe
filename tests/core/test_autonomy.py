from __future__ import annotations

from pydantic import BaseModel

from vibe.core.autonomy import (
    AutonomyCoordinator,
    AutonomyDecisionKind,
    AutonomyIntent,
    AutonomyPolicy,
    parse_advisor_plan,
    parse_autonomy_intent,
)
from vibe.core.config import AutonomyAggressiveness, AutonomyConfig
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


def test_vibe_thinking_levels_map_to_additional_deliberation_passes() -> None:
    assert AutonomyConfig(vibe_thinking="off").vibe_thinking_passes == 0
    assert AutonomyConfig(vibe_thinking="low").vibe_thinking_passes == 1
    assert AutonomyConfig(vibe_thinking="medium").vibe_thinking_passes == 2
    assert AutonomyConfig(vibe_thinking="high").vibe_thinking_passes == 3
    assert AutonomyConfig(vibe_thinking="max").vibe_thinking_passes == 4


def test_boost_profile_enforces_runtime_quality_invariants() -> None:
    config = AutonomyConfig(
        enabled=False,
        aggressiveness=AutonomyAggressiveness.LOW,
        vibe_thinking="off",
        max_parallel_subagents=0,
        web_search_activity="off",
        gauntlet_loop=False,
        require_worker=False,
        require_review=False,
        personal_experience=False,
        boost_mode=True,
    )

    assert config.enabled is True
    assert config.aggressiveness == "max"
    assert config.vibe_thinking == "max"
    assert config.max_parallel_subagents == 16
    assert config.web_search_activity == "max"
    assert config.gauntlet_loop is True
    assert config.personal_experience is True
    assert config.require_worker is True
    assert config.require_review is True


def test_advisor_plan_parses_a_valid_dependency_graph() -> None:
    plan = parse_advisor_plan(
        '<goal-plan>{"tasks":['
        '{"id":"inspect","content":"Inspect","agent":"explore","depends_on":[]},'
        '{"id":"change","content":"Change","agent":"worker",'
        '"depends_on":["inspect"]}]}</goal-plan>',
        "fallback",
    )

    assert [task.id for task in plan.tasks] == ["inspect", "change"]
    assert plan.tasks[1].depends_on == ["inspect"]


def test_invalid_advisor_plan_falls_back_to_a_worker_task() -> None:
    plan = parse_advisor_plan("OK", "Implement the requested feature")

    assert len(plan.tasks) == 1
    assert plan.tasks[0].agent == "worker"
    assert plan.tasks[0].content == "Implement the requested feature"


def test_advisor_plan_accepts_root_owned_desktop_tasks() -> None:
    plan = parse_advisor_plan(
        '<goal-plan>{"tasks":['
        '{"id":"desktop","content":"Control and verify the desktop",'
        '"agent":"root","depends_on":[]}]}</goal-plan>',
        "fallback",
    )

    assert plan.tasks[0].agent == "root"


def test_autonomy_intent_parser_accepts_only_the_small_output_contract() -> None:
    assert parse_autonomy_intent("DIRECT") is AutonomyIntent.DIRECT
    assert parse_autonomy_intent("`autonomous`") is AutonomyIntent.AUTONOMOUS
    assert parse_autonomy_intent("This is direct") is None


def _task_call(call_id: str, agent: str) -> ToolCallEvent:
    return ToolCallEvent(
        tool_call_id=call_id,
        tool_name="task",
        tool_class=Task,
        args=TaskArgs(task=f"Run {agent}", agent=agent),
    )


def _task_result(
    call_id: str, response: str = "done", *, evidence_tool_calls: int = 0
) -> ToolResultEvent:
    return ToolResultEvent(
        tool_call_id=call_id,
        tool_name="task",
        tool_class=Task,
        result=TaskResult(
            response=response,
            turns_used=1,
            completed=True,
            evidence_tool_calls=evidence_tool_calls,
        ),
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
    coordinator.observe(
        _task_result(
            "reviewer-1",
            "All checks passed\n"
            "EVIDENCE_CHECKED: focused behavior => test passed\n"
            "VERDICT: PASS",
            evidence_tool_calls=1,
        )
    )


def test_reviewer_cannot_self_attest_evidence_without_observing_tools() -> None:
    coordinator = AutonomyCoordinator()
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())
    coordinator.observe(_task_call("reviewer-1", "reviewer"))
    coordinator.observe(
        _task_result(
            "reviewer-1", "EVIDENCE_CHECKED: fabricated claim => passed\nVERDICT: PASS"
        )
    )

    decision = coordinator.evaluate_completion()

    assert decision.kind is AutonomyDecisionKind.RETRY
    assert "reviewer did not return evidence-backed VERDICT: PASS" in decision.reasons


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
    assert "reviewer did not return evidence-backed VERDICT: PASS" in decision.reasons


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


def test_reviewer_runs_only_after_plan_and_worker_are_complete() -> None:
    coordinator = AutonomyCoordinator(AutonomyPolicy(require_worker=True))
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)

    assert coordinator.should_run_reviewer() is False
    coordinator.observe(_terminal_todos())
    assert coordinator.should_run_reviewer() is False
    coordinator.observe(_task_call("worker-1", "worker"))
    coordinator.observe(_task_result("worker-1"))

    assert coordinator.should_run_reviewer() is True
    _observe_passing_reviewer(coordinator)
    assert coordinator.should_run_reviewer() is False


def test_root_owned_plan_does_not_require_an_inaccessible_worker() -> None:
    coordinator = AutonomyCoordinator(AutonomyPolicy(require_worker=True))
    coordinator.start_turn()
    coordinator.allow_root_owned_plan()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())

    assert coordinator.should_run_reviewer() is True
    _observe_passing_reviewer(coordinator)
    assert coordinator.should_run_reviewer() is False


def test_reviewer_pass_marker_must_be_the_final_nonempty_line() -> None:
    coordinator = AutonomyCoordinator(AutonomyPolicy(require_worker=False))
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())
    coordinator.observe(_task_call("reviewer-1", "reviewer"))
    coordinator.observe(
        _task_result(
            "reviewer-1", "The instructions mention VERDICT: PASS.\nVERDICT: FAIL"
        )
    )

    decision = coordinator.evaluate_completion()

    assert "reviewer did not return evidence-backed VERDICT: PASS" in decision.reasons


def test_reviewer_pass_without_checked_evidence_is_rejected() -> None:
    coordinator = AutonomyCoordinator(AutonomyPolicy(require_worker=False))
    coordinator.start_turn()
    _observe_successful_advisor(coordinator)
    coordinator.observe(_terminal_todos())
    coordinator.observe(_task_call("reviewer-1", "reviewer"))
    coordinator.observe(
        _task_result("reviewer-1", "Everything looks correct\nVERDICT: PASS")
    )

    decision = coordinator.evaluate_completion()

    assert decision.kind is AutonomyDecisionKind.RETRY
    assert "reviewer did not return evidence-backed VERDICT: PASS" in decision.reasons


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
    assert (
        "reviewer did not return evidence-backed VERDICT: PASS" in stale_review.reasons
    )
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
    coordinator.observe(
        _task_result(
            "reviewer-1",
            "EVIDENCE_CHECKED: worker output => verified\nVERDICT: PASS",
            evidence_tool_calls=1,
        )
    )

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
                    TaskResult(
                        response=(
                            "EVIDENCE_CHECKED: swarm result => verified\nVERDICT: PASS"
                        ),
                        turns_used=1,
                        completed=True,
                        evidence_tool_calls=1,
                    )
                ],
                completed_count=1,
            ),
        )
    )

    assert coordinator.evaluate_completion().kind is AutonomyDecisionKind.PASS
