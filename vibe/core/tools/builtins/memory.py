from __future__ import annotations

from collections.abc import AsyncGenerator
from enum import StrEnum, auto
from typing import Self

from pydantic import BaseModel, Field, model_validator

from vibe.core.checkpoints import FileSnapshot
from vibe.core.memory import GlobalMemoryStore, MemoryEntry, MemoryStoreError
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
)
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import ToolStreamEvent
from vibe.utils.tool_presentation import ToolEffectKind


class MemoryAction(StrEnum):
    REMEMBER = auto()
    LIST = auto()
    FORGET = auto()


class MemoryArgs(BaseModel):
    action: MemoryAction = Field(description="Action: 'remember', 'list', or 'forget'")
    content: str | None = Field(
        default=None,
        description="Durable user-approved note; required for action='remember'",
    )
    memory_id: str | None = Field(
        default=None,
        description="Stable entry id returned by list; required for action='forget'",
    )

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action is MemoryAction.REMEMBER and not self.content:
            raise ValueError("content is required for action='remember'")
        if self.action is MemoryAction.FORGET and not self.memory_id:
            raise ValueError("memory_id is required for action='forget'")
        return self


class MemoryResult(BaseModel):
    verb: str
    entries: list[MemoryEntry]
    total_count: int
    omitted_count: int = 0
    changed: bool = False
    message: str


class MemoryConfig(BaseToolConfig):
    auto_remember: bool = Field(
        default=False,
        description=(
            "Allow the root model to save durable user preferences and confirmed "
            "long-lived conventions without a separate /memory command."
        ),
    )


class MemoryState(BaseToolState):
    pass


class Memory(
    BaseTool[MemoryArgs, MemoryResult, MemoryConfig, MemoryState],
    ToolUIData[MemoryArgs, MemoryResult],
):
    effect_kind = ToolEffectKind.TOOL

    @classmethod
    def format_call_display(cls, args: MemoryArgs) -> ToolCallDisplay:
        match args.action:
            case MemoryAction.REMEMBER:
                return ToolCallDisplay(
                    summary="Saving global memory",
                    verb="Saving",
                    message="global memory",
                    settled_verb="Saved",
                    settled_message="global memory",
                )
            case MemoryAction.LIST:
                return ToolCallDisplay(
                    summary="Reading global memory",
                    verb="Reading",
                    message="global memory",
                    settled_verb="Read",
                    settled_message="global memory",
                )
            case MemoryAction.FORGET:
                return ToolCallDisplay(
                    summary="Removing global memory",
                    verb="Removing",
                    message=args.memory_id or "global memory",
                    settled_verb="Removed",
                    settled_message=args.memory_id or "global memory",
                )

    @classmethod
    def format_result_display(cls, result: MemoryResult) -> ToolResultDisplay:
        return ToolResultDisplay(success=True, verb=result.verb, message=result.message)

    @classmethod
    def get_status_text(cls) -> str:
        return "Managing global memory"

    def get_file_snapshot(self, args: MemoryArgs) -> FileSnapshot | None:
        if args.action is MemoryAction.LIST:
            return None
        return self.get_file_snapshot_for_path(str(GlobalMemoryStore().path))

    async def run(
        self, args: MemoryArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | MemoryResult, None]:
        store = GlobalMemoryStore()
        try:
            match args.action:
                case MemoryAction.REMEMBER:
                    remembered = await store.remember(args.content or "")
                    yield MemoryResult(
                        verb="Saved" if remembered.created else "Kept",
                        entries=[remembered.entry],
                        total_count=remembered.total_count,
                        changed=remembered.created,
                        message=(
                            f"entry {remembered.entry.id}"
                            if remembered.created
                            else f"existing entry {remembered.entry.id}"
                        ),
                    )
                case MemoryAction.LIST:
                    entries, omitted_count = store.bounded_entries()
                    yield MemoryResult(
                        verb="Read",
                        entries=entries,
                        total_count=len(entries) + omitted_count,
                        omitted_count=omitted_count,
                        message=f"{len(entries) + omitted_count} entries",
                    )
                case MemoryAction.FORGET:
                    forgotten = await store.forget(args.memory_id or "")
                    yield MemoryResult(
                        verb="Removed",
                        entries=[forgotten.entry],
                        total_count=forgotten.total_count,
                        changed=True,
                        message=f"entry {forgotten.entry.id}",
                    )
        except MemoryStoreError as exc:
            raise ToolError(str(exc)) from exc


__all__ = [
    "Memory",
    "MemoryAction",
    "MemoryArgs",
    "MemoryConfig",
    "MemoryResult",
    "MemoryState",
]
