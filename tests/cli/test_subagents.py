from __future__ import annotations

import pytest

from vibe.app_server.models import (
    CompletedEffectState,
    EffectCallDisplay,
    EffectResultDisplay,
    FailedEffectState,
    GenericEffectDetail,
    PendingEffectState,
    PublicEffectEntry,
    PublicEntryGenerationStatus,
    PublicError,
    PublicMessageEntry,
    RunningEffectState,
    SubagentEffectDetail,
    SubagentEffectInput,
    SubagentEffectOutput,
)
from vibe.cli.subagents import format_subagent_status


def _display() -> EffectCallDisplay:
    return EffectCallDisplay(summary="Running subagent", status_text="Running")


def _entry(
    index: int,
    *,
    agent: str = "explore",
    task: str = "Inspect the project",
    state=None,
    child_session_id: str | None = None,
) -> PublicEffectEntry:
    return PublicEffectEntry(
        id=f"effect-{index}",
        session_id="root",
        turn_id="turn-1",
        created_at=index,
        updated_at=index,
        generation_status=PublicEntryGenerationStatus.COMPLETED,
        title="task",
        detail=SubagentEffectDetail(
            tool_name="task",
            display=_display(),
            input=SubagentEffectInput(task=task, agent=agent),
            child_session_id=child_session_id,
        ),
        state=state or PendingEffectState(),
    )


def _completed(
    *, turns: int = 2, duration_ms: float = 1_500, completed: bool = True
) -> CompletedEffectState:
    return CompletedEffectState(
        output=SubagentEffectOutput(
            response="done", turns_used=turns, completed=completed
        ).model_dump(mode="json"),
        duration_ms=duration_ms,
        display=EffectResultDisplay(success=completed, message="done"),
    )


def test_formats_active_before_newest_recent_with_a_total_limit() -> None:
    history = [
        _entry(1, state=_completed(), child_session_id="old"),
        _entry(2, agent="worker", state=RunningEffectState(), child_session_id="run"),
        _entry(3, state=_completed(turns=3), child_session_id="new"),
    ]

    result = format_subagent_status(history, limit=2)

    assert result.splitlines() == [
        "Subagents: 1 active, 2 recent (showing 2/3):",
        "- running | agent=worker | task=Inspect the project | child=run",
        "- completed | agent=explore | task=Inspect the project | child=new | turns=3 | duration=1.5s",
    ]


def test_active_entries_fill_the_limit_without_leaking_recent_entries() -> None:
    history = [
        _entry(1, state=_completed(), child_session_id="recent"),
        _entry(2, state=RunningEffectState(), child_session_id="active-1"),
        _entry(3, state=RunningEffectState(), child_session_id="active-2"),
    ]

    result = format_subagent_status(history, limit=2)

    assert "child=active-1" in result
    assert "child=active-2" in result
    assert "child=recent" not in result
    assert len(result.splitlines()) == 3


def test_formats_interrupted_turns_and_subsecond_duration() -> None:
    result = format_subagent_status([
        _entry(
            1,
            state=_completed(turns=4, duration_ms=250, completed=False),
            child_session_id="child-1",
        )
    ])

    assert (
        "interrupted | agent=explore | task=Inspect the project | child=child-1 "
        "| turns=4 | duration=250ms"
    ) in result


def test_bounds_and_compacts_task_and_marks_an_unlinked_child() -> None:
    result = format_subagent_status(
        [_entry(1, task="  inspect\n\nall   relevant   files  ")], task_max_chars=18
    )

    assert "task=inspect all rel... | child=pending" in result


def test_ignores_messages_and_non_subagent_effects() -> None:
    message = PublicMessageEntry(
        id="message-1",
        session_id="root",
        created_at=1,
        updated_at=1,
        generation_status=PublicEntryGenerationStatus.COMPLETED,
        role="assistant",
        content=[],
    )
    generic = PublicEffectEntry(
        id="effect-1",
        session_id="root",
        created_at=2,
        updated_at=2,
        generation_status=PublicEntryGenerationStatus.COMPLETED,
        title="weather",
        detail=GenericEffectDetail(tool_name="weather", display=_display(), input=None),
        state=RunningEffectState(),
    )

    assert format_subagent_status([message, generic]) == "No subagents found."


def test_failed_status_includes_duration_without_turns() -> None:
    failed = FailedEffectState(
        error=PublicError(message="boom"),
        duration_ms=2_000,
        display=EffectResultDisplay(success=False, message="failed"),
    )

    result = format_subagent_status([_entry(1, state=failed)])

    assert "failed | agent=explore" in result
    assert "duration=2.0s" in result
    assert "turns=" not in result


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"limit": 0}, "limit"), ({"task_max_chars": 7}, "task_max_chars")],
)
def test_rejects_invalid_bounds(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        format_subagent_status([], **kwargs)
