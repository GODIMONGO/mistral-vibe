from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from textual.content import Content
from textual.reactive import reactive
from textual.widgets import Static

from vibe.app_server.models import (
    CompletedEffectState,
    PublicEffectEntry,
    PublicHistoryEntry,
    TodoEffectDetail,
    TodoEffectItem,
    TodoEffectOutput,
    TodoEffectStatus,
)
from vibe.utils.cache_store import CacheStore, FileSystemCacheStore

_MAX_VISIBLE_TASKS = 3
_MAX_TASK_LABEL_CHARS = 64
_MAX_DISMISSED_PLANS = 100
_CACHE_SECTION = "task_status"
_DISMISSED_PLANS_KEY = "dismissed_plans"


@dataclass(frozen=True, slots=True)
class TaskStatusState:
    in_progress: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return sum(
            len(items)
            for items in (
                self.in_progress,
                self.completed,
                self.pending,
                self.cancelled,
            )
        )


def _compact_label(label: str) -> str:
    normalized = " ".join(label.split())
    if len(normalized) <= _MAX_TASK_LABEL_CHARS:
        return normalized
    return normalized[: _MAX_TASK_LABEL_CHARS - 1].rstrip() + "…"


def _summarize(labels: tuple[str, ...]) -> str:
    visible = labels[:_MAX_VISIBLE_TASKS]
    summary = " · ".join(visible)
    omitted = len(labels) - len(visible)
    return f"{summary} · +{omitted}" if omitted else summary


def _plain_state(state: TaskStatusState) -> str:
    if state.total == 0:
        return "No active task plan."
    lines = [f"Tasks {len(state.completed)}/{state.total} done"]
    categories = (
        ("Working", state.in_progress),
        ("Done", state.completed),
        ("Waiting", state.pending),
        ("Cancelled", state.cancelled),
    )
    lines.extend(
        f"{label}: {_summarize(items)}" for label, items in categories if items
    )
    return "\n".join(lines)


def _state_from_todos(todos: list[TodoEffectItem]) -> TaskStatusState:
    groups: dict[TodoEffectStatus, list[str]] = {
        status: [] for status in TodoEffectStatus
    }
    for todo in todos:
        groups[todo.status].append(_compact_label(todo.content))
    return TaskStatusState(
        in_progress=tuple(groups[TodoEffectStatus.IN_PROGRESS]),
        completed=tuple(groups[TodoEffectStatus.COMPLETED]),
        pending=tuple(groups[TodoEffectStatus.PENDING]),
        cancelled=tuple(groups[TodoEffectStatus.CANCELLED]),
    )


def _todos_from_entry(entry: PublicEffectEntry) -> list[TodoEffectItem] | None:
    if not isinstance(entry.detail, TodoEffectDetail):
        return None
    todos = entry.detail.input.todos if entry.detail.input is not None else None
    if not isinstance(entry.state, CompletedEffectState):
        return todos
    try:
        return TodoEffectOutput.model_validate(entry.state.output).todos
    except ValidationError:
        return todos


def _dismissed_plans(data: dict[str, Any]) -> dict[str, str]:
    value = data.get(_DISMISSED_PLANS_KEY)
    if not isinstance(value, dict):
        return {}
    return {
        session_id: entry_id
        for session_id, entry_id in value.items()
        if isinstance(session_id, str) and isinstance(entry_id, str)
    }


class TaskStatusBar(Static):
    state = reactive(TaskStatusState())

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.display = False
        self._latest_entry_id: str | None = None
        self._dismissed_entry_id: str | None = None

    async def restore_persisted(
        self,
        session_id: str,
        history: list[PublicHistoryEntry],
        *,
        cache_store: CacheStore | None = None,
    ) -> None:
        store = cache_store or FileSystemCacheStore()
        data = await asyncio.to_thread(store.read_section, _CACHE_SECTION)
        self._dismissed_entry_id = _dismissed_plans(data).get(session_id)
        self.restore(history)

    async def dismiss_persisted(
        self, session_id: str, *, cache_store: CacheStore | None = None
    ) -> bool:
        had_tasks = self.state.total > 0
        self.state = TaskStatusState()
        self._dismissed_entry_id = self._latest_entry_id
        if self._latest_entry_id is None:
            return had_tasks
        store = cache_store or FileSystemCacheStore()
        data = await asyncio.to_thread(store.read_section, _CACHE_SECTION)
        dismissed = _dismissed_plans(data)
        dismissed[session_id] = self._latest_entry_id
        dismissed = dict(list(dismissed.items())[-_MAX_DISMISSED_PLANS:])
        await asyncio.to_thread(
            store.write_section, _CACHE_SECTION, {_DISMISSED_PLANS_KEY: dismissed}
        )
        return had_tasks

    def restore(self, history: list[PublicHistoryEntry]) -> None:
        self._latest_entry_id = None
        for entry in reversed(history):
            if isinstance(entry, PublicEffectEntry) and self.observe(entry):
                return
        self.state = TaskStatusState()
        self.state = TaskStatusState()

    def observe(self, entry: PublicEffectEntry) -> bool:
        todos = _todos_from_entry(entry)
        if todos is None:
            return False
        self._latest_entry_id = entry.id
        if entry.id == self._dismissed_entry_id:
            self.state = TaskStatusState()
            return True
        self._dismissed_entry_id = None
        self.state = _state_from_todos(todos)
        return True

    def watch_state(self, state: TaskStatusState) -> None:
        self.display = state.total > 0
        if not self.display:
            self.update("")
            return
        self.update(self._render_state(state))

    def plain_status(self) -> str:
        """Return the current plan status for the `/tasks` command."""
        return _plain_state(self.state)

    @staticmethod
    def _render_state(state: TaskStatusState) -> Content:
        lines = [
            Content.assemble(
                ("Tasks ", "$text-muted"),
                (f"{len(state.completed)}/{state.total} done", "$foreground bold"),
            )
        ]
        categories = (
            ("▶ Working: ", state.in_progress, "$warning"),
            ("✓ Done: ", state.completed, "$success"),
            ("○ Waiting: ", state.pending, "$foreground"),
            ("× Cancelled: ", state.cancelled, "$text-muted"),
        )
        for prefix, labels, style in categories:
            if labels:
                lines.append(Content.styled(prefix + _summarize(labels), style))
        return Content("\n").join(lines)


__all__ = ["TaskStatusBar", "TaskStatusState"]
