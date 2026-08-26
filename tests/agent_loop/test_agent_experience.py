from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from vibe.core.config import AutonomyConfig
from vibe.core.experience import ExperienceStore
from vibe.core.types import LLMMessage, MemoryStatusEvent, Role


@pytest.mark.asyncio
async def test_agent_records_and_retrieves_personal_experience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    monkeypatch.setattr("vibe.core.agent_loop._loop.ExperienceStore", lambda: store)
    loop = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(personal_experience=True)
        ),
        cwd=tmp_path / "project",
    )
    records: list[tuple[str, str, Literal["success", "failure", "skipped"], str]] = [
        (
            "git_bash",
            '{"command":"uv run pytest tests/core/test_memory.py"}',
            "success",
            "12 tests passed",
        )
    ]

    saved_events = [event async for event in loop._persist_personal_experience(records)]
    loaded_events = [
        event
        async for event in loop._refresh_personal_experience(
            "verify memory changes with pytest"
        )
    ]

    assert any(
        isinstance(event, MemoryStatusEvent) and event.status == "experience_saved"
        for event in saved_events
    )
    saved_notice = next(
        event for event in saved_events if isinstance(event, MemoryStatusEvent)
    )
    assert "1 new" in saved_notice.message
    assert "git_bash success: 12 tests passed" in saved_notice.message
    assert any(
        isinstance(event, MemoryStatusEvent) and event.status == "experience_loaded"
        for event in loaded_events
    )
    assert loop._active_experience_message is not None
    assert "12 tests passed" in (loop._active_experience_message.content or "")

    repeated_save_events = [
        event async for event in loop._persist_personal_experience(records)
    ]
    repeated_load_events = [
        event
        async for event in loop._refresh_personal_experience(
            "verify memory changes with pytest"
        )
    ]

    assert repeated_save_events == []
    assert repeated_load_events == []

    changed_records: list[
        tuple[str, str, Literal["success", "failure", "skipped"], str]
    ] = [
        (
            "git_bash",
            '{"command":"uv run pytest tests/core/test_memory.py"}',
            "failure",
            "one regression remains",
        )
    ]
    changed_events = [
        event async for event in loop._persist_personal_experience(changed_records)
    ]
    refreshed_events = [
        event
        async for event in loop._refresh_personal_experience(
            "verify memory changes with pytest"
        )
    ]

    assert len(changed_events) == 1
    assert isinstance(changed_events[0], MemoryStatusEvent)
    assert "1 changed" in changed_events[0].message
    assert refreshed_events == []
    assert "one regression remains" in (loop._active_experience_message.content or "")

    loop._clear_personal_experience_context()

    assert loop._active_experience_message is None
    assert all(
        "<personal_experience>" not in (message.content or "")
        for message in loop.messages
    )


@pytest.mark.asyncio
async def test_disabled_personal_experience_does_not_create_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "experience.sqlite3"
    store = ExperienceStore(database)
    monkeypatch.setattr("vibe.core.agent_loop._loop.ExperienceStore", lambda: store)
    loop = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(personal_experience=False)
        ),
        cwd=tmp_path / "project",
    )

    events = [
        event
        async for event in loop._persist_personal_experience([
            ("git_bash", "uv run pytest", "success", "passed")
        ])
    ]

    assert events == []
    assert not database.exists()


def test_personal_experience_query_ignores_agent_and_tool_chatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    monkeypatch.setattr("vibe.core.agent_loop._loop.ExperienceStore", lambda: store)
    loop = build_test_agent_loop(
        config=build_test_vibe_config(
            autonomy=AutonomyConfig(personal_experience=True)
        ),
        cwd=tmp_path / "project",
    )
    loop.messages.extend([
        LLMMessage(role=Role.user, content="redesign fleet monitoring"),
        LLMMessage(role=Role.assistant, content="scp deployment output"),
        LLMMessage(role=Role.tool, content="same repeated terminal result"),
    ])

    query = loop._personal_experience_query("billing dashboard")

    assert "billing dashboard" in query
    assert "redesign fleet monitoring" in query
    assert "scp deployment output" not in query
    assert "same repeated terminal result" not in query
