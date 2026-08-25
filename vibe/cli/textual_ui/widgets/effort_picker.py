from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from vibe.app_server.config import (
    MAX_PARALLEL_SUBAGENTS,
    MIN_PARALLEL_SUBAGENTS,
    THINKING_LEVELS,
    ThinkingLevel,
)
from vibe.cli.textual_ui.shortcut_hints import shortcut, shortcut_hint
from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic

_ROW_COUNT = 3
_ULTRACODE_ROW = 2
_THINKING_BAR_WIDTH = 12
_SUBAGENT_BAR_WIDTH = MAX_PARALLEL_SUBAGENTS


def _slider_bar(filled: int, width: int) -> Content:
    return Content.assemble(
        ("━" * filled, "$primary"), ("━" * (width - filled), "$foreground-muted")
    )


def _row_prefix(selected: bool) -> Content:
    return Content.styled("› " if selected else "  ", "$primary bold")


class EffortPickerApp(Container):
    can_focus = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up,k", "previous_row", "Previous", show=False),
        Binding("down,j", "next_row", "Next", show=False),
        Binding("left,h", "decrease", "Decrease", show=False),
        Binding("right,l", "increase", "Increase", show=False),
        Binding("enter", "apply", "Apply", show=False),
        Binding("u", "ultracode", "UltraCode", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    class Applied(Message):
        thinking: ThinkingLevel
        max_parallel_subagents: int
        ultracode: bool

        def __init__(
            self,
            thinking: ThinkingLevel,
            max_parallel_subagents: int,
            *,
            ultracode: bool,
        ) -> None:
            self.thinking = thinking
            self.max_parallel_subagents = max_parallel_subagents
            self.ultracode = ultracode
            super().__init__()

    class Cancelled(Message):
        pass

    def __init__(
        self,
        *,
        current_thinking: ThinkingLevel,
        current_max_parallel_subagents: int,
        initial_row: int = 0,
    ) -> None:
        super().__init__(id="effortpicker-app")
        self._thinking_index = THINKING_LEVELS.index(current_thinking)
        self._max_parallel_subagents = max(
            MIN_PARALLEL_SUBAGENTS,
            min(current_max_parallel_subagents, MAX_PARALLEL_SUBAGENTS),
        )
        self._selected_row = max(0, min(initial_row, _ROW_COUNT - 1))

    def compose(self) -> ComposeResult:
        with Vertical(id="effortpicker-content"):
            yield NoMarkupStatic("Effort", classes="effortpicker-title")
            yield NoMarkupStatic(
                "Tune reasoning and parallel agents independently",
                classes="effortpicker-subtitle",
            )
            yield Static(id="effortpicker-thinking", classes="effortpicker-row")
            yield Static(id="effortpicker-subagents", classes="effortpicker-row")
            yield Static(id="effortpicker-ultracode", classes="effortpicker-row")
            yield NoMarkupStatic(
                shortcut_hint(
                    f"{shortcut('↑↓/jk')} Select  {shortcut('←→/hl')} Adjust  "
                    f"{shortcut('Enter')} Apply  {shortcut('u')} UltraCode  "
                    f"{shortcut('Esc')} Cancel"
                ),
                classes="effortpicker-help",
            )

    def on_mount(self) -> None:
        self._refresh_rows()
        self.focus()

    def action_previous_row(self) -> None:
        self._selected_row = (self._selected_row - 1) % _ROW_COUNT
        self._refresh_rows()

    def action_next_row(self) -> None:
        self._selected_row = (self._selected_row + 1) % _ROW_COUNT
        self._refresh_rows()

    def action_decrease(self) -> None:
        match self._selected_row:
            case 0:
                self._thinking_index = max(0, self._thinking_index - 1)
            case 1:
                self._max_parallel_subagents = max(
                    MIN_PARALLEL_SUBAGENTS, self._max_parallel_subagents - 1
                )
            case _:
                return
        self._refresh_rows()

    def action_increase(self) -> None:
        match self._selected_row:
            case 0:
                self._thinking_index = min(
                    len(THINKING_LEVELS) - 1, self._thinking_index + 1
                )
            case 1:
                self._max_parallel_subagents = min(
                    MAX_PARALLEL_SUBAGENTS, self._max_parallel_subagents + 1
                )
            case _:
                return
        self._refresh_rows()

    def action_apply(self) -> None:
        if self._selected_row == _ULTRACODE_ROW:
            self.action_ultracode()
            return
        self.post_message(
            self.Applied(
                THINKING_LEVELS[self._thinking_index],
                self._max_parallel_subagents,
                ultracode=False,
            )
        )

    def action_ultracode(self) -> None:
        self.post_message(self.Applied("max", MAX_PARALLEL_SUBAGENTS, ultracode=True))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())

    def _refresh_rows(self) -> None:
        thinking = THINKING_LEVELS[self._thinking_index]
        thinking_filled = round(
            self._thinking_index / (len(THINKING_LEVELS) - 1) * _THINKING_BAR_WIDTH
        )
        thinking_row = Content.assemble(
            _row_prefix(self._selected_row == 0),
            ("Thinking   ", "bold"),
            "Min ",
            _slider_bar(thinking_filled, _THINKING_BAR_WIDTH),
            " Max  ",
            (thinking.upper(), "$primary bold"),
        )
        subagents_row = Content.assemble(
            _row_prefix(self._selected_row == 1),
            ("Subagents  ", "bold"),
            _slider_bar(self._max_parallel_subagents, _SUBAGENT_BAR_WIDTH),
            f"  {self._max_parallel_subagents}/{MAX_PARALLEL_SUBAGENTS}",
            "  (0 = disabled)" if self._max_parallel_subagents == 0 else "",
        )
        ultracode_style = (
            "$primary bold" if self._selected_row == _ULTRACODE_ROW else "bold"
        )
        ultracode_row = Content.assemble(
            _row_prefix(self._selected_row == _ULTRACODE_ROW),
            ("UltraCode  ", ultracode_style),
            ("MAX THINKING · MAX AGENTS", "$primary bold"),
            "\n  Hardest tasks: plan, swarm, review, and verify at maximum effort",
        )
        self.query_one("#effortpicker-thinking", Static).update(thinking_row)
        self.query_one("#effortpicker-subagents", Static).update(subagents_row)
        self.query_one("#effortpicker-ultracode", Static).update(ultracode_row)


__all__ = ["EffortPickerApp"]
