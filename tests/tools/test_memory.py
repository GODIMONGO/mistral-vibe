from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
import pytest

from vibe.core.memory import GlobalMemoryStore
from vibe.core.tools.builtins.memory import (
    Memory,
    MemoryAction,
    MemoryArgs,
    MemoryConfig,
    MemoryResult,
    MemoryState,
)
from vibe.core.tools.models import ToolPermission


def _tool() -> Memory:
    return Memory(config_getter=MemoryConfig, state=MemoryState())


@pytest.mark.asyncio
async def test_memory_tool_remember_list_and_forget(config_dir: Path) -> None:
    tool = _tool()

    saved = await anext(
        tool.run(MemoryArgs(action=MemoryAction.REMEMBER, content="Prefer uv"))
    )
    assert isinstance(saved, MemoryResult)
    assert saved.changed is True

    listed = await anext(tool.run(MemoryArgs(action=MemoryAction.LIST)))
    assert isinstance(listed, MemoryResult)
    assert listed.entries == saved.entries

    removed = await anext(
        tool.run(MemoryArgs(action=MemoryAction.FORGET, memory_id=saved.entries[0].id))
    )
    assert isinstance(removed, MemoryResult)
    assert removed.changed is True
    assert GlobalMemoryStore().load().entries == []


def test_memory_args_require_action_specific_fields() -> None:
    with pytest.raises(ValidationError, match="content is required"):
        MemoryArgs(action=MemoryAction.REMEMBER)

    with pytest.raises(ValidationError, match="memory_id is required"):
        MemoryArgs(action=MemoryAction.FORGET)


def test_memory_writes_require_permission_by_default() -> None:
    assert MemoryConfig().permission is ToolPermission.ASK
    assert MemoryConfig().auto_remember is False
