from __future__ import annotations

from collections.abc import Sequence

from vibe.app_server.models import (
    BlockedEffectState,
    CancelledEffectState,
    CompletedEffectState,
    FailedEffectState,
    PendingEffectState,
    PublicEffectEntry,
    PublicHistoryEntry,
    RunningEffectState,
    SubagentEffectDetail,
    SubagentEffectOutput,
)

DEFAULT_SUBAGENT_STATUS_LIMIT = 8
DEFAULT_SUBAGENT_TASK_MAX_CHARS = 120
_MIN_TASK_MAX_CHARS = 8
_MILLISECONDS_PER_SECOND = 1_000


def format_subagent_status(
    history: Sequence[PublicHistoryEntry],
    *,
    limit: int = DEFAULT_SUBAGENT_STATUS_LIMIT,
    task_max_chars: int = DEFAULT_SUBAGENT_TASK_MAX_CHARS,
) -> str:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if task_max_chars < _MIN_TASK_MAX_CHARS:
        raise ValueError(f"task_max_chars must be at least {_MIN_TASK_MAX_CHARS}")

    entries = [
        entry
        for entry in history
        if isinstance(entry, PublicEffectEntry)
        and isinstance(entry.detail, SubagentEffectDetail)
    ]
    if not entries:
        return "No subagents found."

    active = [entry for entry in entries if _is_active(entry)]
    recent = [entry for entry in entries if not _is_active(entry)]
    selected_active = active[-limit:]
    remaining = limit - len(selected_active)
    selected_recent = recent[-remaining:] if remaining else []
    selected = [*selected_active, *reversed(selected_recent)]

    header = f"Subagents: {len(active)} active, {len(recent)} recent"
    if len(selected) < len(entries):
        header += f" (showing {len(selected)}/{len(entries)})"
    lines = [f"{header}:"]
    lines.extend(
        f"- {_format_entry(entry, task_max_chars=task_max_chars)}" for entry in selected
    )
    return "\n".join(lines)


def _is_active(entry: PublicEffectEntry) -> bool:
    return isinstance(
        entry.state, PendingEffectState | RunningEffectState | BlockedEffectState
    )


def _format_entry(entry: PublicEffectEntry, *, task_max_chars: int) -> str:
    detail = entry.detail
    if not isinstance(detail, SubagentEffectDetail):
        raise ValueError("entry is not a subagent effect")
    args = detail.input
    agent = args.agent if args is not None else "unknown"
    task = _bounded_text(args.task if args is not None else "unknown", task_max_chars)
    child_session = detail.child_session_id or "pending"
    parts = [
        entry.state.status,
        f"agent={agent}",
        f"task={task}",
        f"child={child_session}",
    ]
    if output := _completed_output(entry):
        parts[0] = "completed" if output.completed else "interrupted"
        parts.append(f"turns={output.turns_used}")
    if duration := _duration(entry):
        parts.append(f"duration={_format_duration(duration)}")
    return " | ".join(parts)


def _completed_output(entry: PublicEffectEntry) -> SubagentEffectOutput | None:
    state = entry.state
    if not isinstance(state, CompletedEffectState) or state.output is None:
        return None
    try:
        return SubagentEffectOutput.model_validate(state.output)
    except ValueError:
        return None


def _duration(entry: PublicEffectEntry) -> float:
    state = entry.state
    if not isinstance(
        state, CompletedEffectState | FailedEffectState | CancelledEffectState
    ):
        return 0.0
    duration = state.duration_ms
    return duration if duration > 0 else 0.0


def _format_duration(duration_ms: float) -> str:
    if duration_ms < _MILLISECONDS_PER_SECOND:
        return f"{duration_ms:g}ms"
    return f"{duration_ms / _MILLISECONDS_PER_SECOND:.1f}s"


def _bounded_text(text: str, limit: int) -> str:
    compact = " ".join(text.split()) or "unknown"
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


__all__ = [
    "DEFAULT_SUBAGENT_STATUS_LIMIT",
    "DEFAULT_SUBAGENT_TASK_MAX_CHARS",
    "format_subagent_status",
]
