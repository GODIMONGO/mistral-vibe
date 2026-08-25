from __future__ import annotations

import pytest

from vibe.app_server.events import HistoryEntryAdded
from vibe.app_server.models import (
    CompletedEffectState,
    EffectCallDisplay,
    EffectResultDisplay,
    PublicEffectEntry,
    PublicEntryGenerationStatus,
    RunningEffectState,
    TodoEffectDetail,
    TodoEffectInput,
    TodoEffectItem,
    TodoEffectOutput,
    TodoEffectStatus,
)
from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.task_status_bar import TaskStatusBar


def _todo(content: str, status: TodoEffectStatus) -> TodoEffectItem:
    return TodoEffectItem(id=content, content=content, status=status)


def _entry(
    entry_id: str,
    input_todos: list[TodoEffectItem],
    output_todos: list[TodoEffectItem] | None = None,
) -> PublicEffectEntry:
    detail = TodoEffectDetail(
        tool_name="todo",
        display=EffectCallDisplay(summary="Updating todos", status_text="Updating"),
        input=TodoEffectInput(action="write", todos=input_todos),
    )
    if output_todos is None:
        state = RunningEffectState()
        generation_status = PublicEntryGenerationStatus.IN_PROGRESS
    else:
        state = CompletedEffectState(
            output=TodoEffectOutput(todos=output_todos).model_dump(mode="json"),
            display=EffectResultDisplay(success=True, message="Updated todos"),
        )
        generation_status = PublicEntryGenerationStatus.COMPLETED
    return PublicEffectEntry(
        id=entry_id,
        session_id="session",
        turn_id="turn",
        created_at=1,
        updated_at=1,
        generation_status=generation_status,
        title="Todo",
        detail=detail,
        state=state,
    )


def test_task_status_bar_groups_and_renders_task_states() -> None:
    widget = TaskStatusBar()
    entry = _entry(
        "todos",
        [
            _todo("Implement status bar", TodoEffectStatus.IN_PROGRESS),
            _todo("Inspect todo events", TodoEffectStatus.COMPLETED),
            _todo("Run UI tests", TodoEffectStatus.PENDING),
        ],
    )

    assert widget.observe(entry)
    widget.watch_state(widget.state)

    rendered = str(widget.render())
    assert "Tasks 1/3 done" in rendered
    assert "▶ Working: Implement status bar" in rendered
    assert "✓ Done: Inspect todo events" in rendered
    assert "○ Waiting: Run UI tests" in rendered


def test_completed_todo_output_replaces_stale_call_input() -> None:
    widget = TaskStatusBar()
    entry = _entry(
        "todos",
        [_todo("Implement", TodoEffectStatus.IN_PROGRESS)],
        [_todo("Implement", TodoEffectStatus.COMPLETED)],
    )

    assert widget.observe(entry)

    assert widget.state.in_progress == ()
    assert widget.state.completed == ("Implement",)


def test_restore_uses_latest_todo_plan() -> None:
    widget = TaskStatusBar()
    earlier = _entry("earlier", [_todo("Inspect", TodoEffectStatus.IN_PROGRESS)])
    latest = _entry(
        "latest",
        [
            _todo("Inspect", TodoEffectStatus.COMPLETED),
            _todo("Implement", TodoEffectStatus.IN_PROGRESS),
        ],
    )

    widget.restore([earlier, latest])

    assert widget.state.completed == ("Inspect",)
    assert widget.state.in_progress == ("Implement",)


def test_status_bar_bounds_long_task_lists_and_labels() -> None:
    widget = TaskStatusBar()
    entry = _entry(
        "many",
        [
            _todo(f"Pending task {index} " + ("detail " * 20), TodoEffectStatus.PENDING)
            for index in range(5)
        ],
    )

    assert widget.observe(entry)
    widget.watch_state(widget.state)

    rendered = str(widget.render())
    assert "· +2" in rendered
    assert "…" in rendered


@pytest.mark.asyncio
async def test_app_updates_task_status_bar_from_live_todo_event(
    vibe_app: VibeApp,
) -> None:
    entry = _entry(
        "live-todos", [_todo("Live implementation", TodoEffectStatus.IN_PROGRESS)]
    )

    async with vibe_app.run_test():
        await vibe_app._handle_turn_event(HistoryEntryAdded(entry))

        widget = vibe_app.query_one(TaskStatusBar)
        assert widget.display
        assert widget.state.in_progress == ("Live implementation",)
