from __future__ import annotations

from vibe.core.execution_budget import (
    BudgetSignal,
    ExecutionBudgetTracker,
    TaskScale,
    classify_task,
    requires_autonomous_pipeline,
)


def test_classifies_direct_mini_medium_and_large_without_model_call() -> None:
    assert classify_task("Почему Python использует GIL?").scale is TaskScale.DIRECT
    assert classify_task("Исправь опечатку в README").scale is TaskScale.MINI
    assert (
        classify_task("Добавь API, тесты и интеграцию с базой данных").scale
        is TaskScale.MEDIUM
    )
    assert (
        classify_task(
            "Выполни все задачи, сделай полный редизайн всего проекта и задеплой в продакшен"
        ).scale
        is TaskScale.LARGE
    )


def test_force_large_preserves_direct_questions() -> None:
    assert (
        classify_task("Объясни этот алгоритм", force_large=True).scale
        is TaskScale.DIRECT
    )
    assert classify_task("Исправь API", force_large=True).scale is TaskScale.LARGE


def test_local_pipeline_gate_distinguishes_code_work_from_task_management() -> None:
    assert requires_autonomous_pipeline("Исправь опечатку в README") is True
    assert requires_autonomous_pipeline("Добавь API и тесты") is True
    assert requires_autonomous_pipeline("убери все задачи") is False
    assert requires_autonomous_pipeline("Почему backend вернул ошибку?") is False


def test_profiles_control_orchestration_without_total_limits() -> None:
    mini = classify_task("Исправь опечатку")
    medium = classify_task("Добавь API, тесты и интеграцию")
    large = classify_task("Выполни все задачи и полный редизайн всего проекта")
    assert mini.max_parallel_subagents == 1
    assert medium.max_parallel_subagents == 3
    assert large.max_parallel_subagents == 4


def test_tracker_never_stops_on_elapsed_time_or_main_turns() -> None:
    now = [100.0]
    profile = classify_task("Исправь опечатку")
    tracker = ExecutionBudgetTracker(
        profile, initial_tokens=1_000, clock=lambda: now[0]
    )

    assert tracker.check(1_000) == (BudgetSignal.CONTINUE, None)
    now[0] += 24 * 60 * 60
    for _ in range(1_000):
        tracker.record_main_turn()
    assert tracker.remaining_seconds() is None
    assert tracker.check(1_000) == (BudgetSignal.CONTINUE, None)


def test_tracker_never_stops_on_token_usage() -> None:
    profile = classify_task("Исправь опечатку")
    tracker = ExecutionBudgetTracker(profile, initial_tokens=100_000)
    assert tracker.check(100_000_000) == (BudgetSignal.CONTINUE, None)


def test_third_identical_read_is_blocked_until_a_mutation() -> None:
    tracker = ExecutionBudgetTracker(
        classify_task("Исправь API, тесты и интеграцию"), initial_tokens=0
    )
    assert tracker.repeated_read("read_file", '{"file_path":"app.py"}') is False
    assert tracker.repeated_read("read_file", '{"file_path":"app.py"}') is False
    assert tracker.repeated_read("read_file", '{"file_path":"app.py"}') is True
    assert tracker.repeated_read("read_file", '{"file_path":"other.py"}') is False
    tracker.record_mutation()
    assert tracker.repeated_read("read_file", '{"file_path":"app.py"}') is False


def test_polling_and_orchestration_tools_are_not_deduplicated() -> None:
    tracker = ExecutionBudgetTracker(
        classify_task("Исправь API, тесты и интеграцию"), initial_tokens=0
    )
    for _ in range(4):
        assert tracker.repeated_read("bash_output", '{"session_id":1}') is False
        assert tracker.repeated_read("task", '{"agent":"worker"}') is False
