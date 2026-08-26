from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from vibe.app_server.config import (
    ACCURACY_LEVELS,
    ACCURACY_TEMPERATURES,
    MAX_PARALLEL_SUBAGENTS,
    MIN_PARALLEL_SUBAGENTS,
    THINKING_LEVELS,
    WEB_SEARCH_ACTIVITY_LEVELS,
    AccuracyLevel,
    ThinkingLevel,
    WebSearchActivity,
)
from vibe.cli.textual_ui.shortcut_hints import shortcut, shortcut_hint
from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic

_ROW_COUNT = 9
_VIBE_THINKING_ROW = 1
_SUBAGENT_ROW = 2
_ACCURACY_ROW = 3
_WEB_SEARCH_ROW = 4
_EXPERIENCE_ROW = 5
_GAUNTLET_ROW = 6
_BOOST_ROW = 7
_ULTRACODE_ROW = 8
_THINKING_BAR_WIDTH = 12
_SUBAGENT_BAR_WIDTH = MAX_PARALLEL_SUBAGENTS


def _slider_bar(filled: int, width: int) -> Content:
    return Content.assemble(
        ("━" * filled, "$primary"), ("━" * (width - filled), "$foreground-muted")
    )


def _row_prefix(selected: bool) -> Content:
    return Content.styled("› " if selected else "  ", "$primary bold")


def _gauntlet_row(*, selected: bool, enabled: bool) -> Content:
    return Content.assemble(
        _row_prefix(selected),
        ("Gauntlet   ", "$primary bold" if selected else "bold"),
        ("ON" if enabled else "OFF", "$primary bold" if enabled else "$text-muted"),
        "  Real quality bar · builder ↔ harsh critic · repeat until win",
    )


def _boost_row(*, selected: bool, enabled: bool) -> Content:
    return Content.assemble(
        _row_prefix(selected),
        ("BOOST      ", "$primary bold" if selected else "bold"),
        ("ON" if enabled else "OFF", "$primary bold" if enabled else "$text-muted"),
        "  Sonnet 5-class target · plan · research · workers · evidence review",
    )


def _experience_row(*, selected: bool, enabled: bool) -> Content:
    return Content.assemble(
        _row_prefix(selected),
        ("Experience ", "$primary bold" if selected else "bold"),
        ("ON" if enabled else "OFF", "$primary bold" if enabled else "$text-muted"),
        "  Local SQLite RAG · consult each decision · learn from tool outcomes",
    )


def _ultracode_row(*, selected: bool) -> Content:
    return Content.assemble(
        _row_prefix(selected),
        ("UltraCode  ", "$primary bold" if selected else "bold"),
        ("BOOST + MAX SWARM", "$primary bold"),
        "  Hardest engineering tasks with maximum parallel execution",
    )


class EffortPickerApp(Container):
    can_focus = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up,k", "previous_row", "Previous", show=False),
        Binding("down,j", "next_row", "Next", show=False),
        Binding("left,h", "decrease", "Decrease", show=False),
        Binding("right,l", "increase", "Increase", show=False),
        Binding("enter", "apply", "Apply", show=False),
        Binding("b", "boost", "BOOST", show=False),
        Binding("u", "ultracode", "UltraCode", show=False),
        Binding("g", "gauntlet", "Gauntlet", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    class Applied(Message):
        thinking: ThinkingLevel
        vibe_thinking: ThinkingLevel
        max_parallel_subagents: int
        accuracy: AccuracyLevel
        web_search_activity: WebSearchActivity
        boost_mode: bool
        ultracode: bool
        gauntlet_loop: bool
        personal_experience: bool

        def __init__(
            self,
            thinking: ThinkingLevel,
            vibe_thinking: ThinkingLevel,
            max_parallel_subagents: int,
            accuracy: AccuracyLevel,
            web_search_activity: WebSearchActivity,
            gauntlet_loop: bool,
            personal_experience: bool = True,
            *,
            boost_mode: bool,
            ultracode: bool = False,
        ) -> None:
            self.thinking = thinking
            self.vibe_thinking = vibe_thinking
            self.max_parallel_subagents = max_parallel_subagents
            self.accuracy = accuracy
            self.web_search_activity = web_search_activity
            self.gauntlet_loop = gauntlet_loop
            self.personal_experience = personal_experience
            self.boost_mode = boost_mode
            self.ultracode = ultracode
            super().__init__()

    class Cancelled(Message):
        pass

    def __init__(
        self,
        *,
        current_thinking: ThinkingLevel,
        current_vibe_thinking: ThinkingLevel,
        current_max_parallel_subagents: int,
        current_temperature: float,
        current_web_search_activity: WebSearchActivity,
        current_gauntlet_loop: bool,
        current_boost_mode: bool,
        current_personal_experience: bool = True,
        initial_row: int = 0,
    ) -> None:
        super().__init__(id="effortpicker-app")
        self._thinking_index = THINKING_LEVELS.index(current_thinking)
        self._vibe_thinking_index = THINKING_LEVELS.index(current_vibe_thinking)
        self._max_parallel_subagents = max(
            MIN_PARALLEL_SUBAGENTS,
            min(current_max_parallel_subagents, MAX_PARALLEL_SUBAGENTS),
        )
        current_accuracy = min(
            ACCURACY_LEVELS,
            key=lambda level: abs(ACCURACY_TEMPERATURES[level] - current_temperature),
        )
        self._accuracy_index = ACCURACY_LEVELS.index(current_accuracy)
        self._web_search_index = WEB_SEARCH_ACTIVITY_LEVELS.index(
            current_web_search_activity
        )
        self._gauntlet_loop = current_gauntlet_loop
        self._boost_mode = current_boost_mode
        self._personal_experience = current_personal_experience
        self._selected_row = max(0, min(initial_row, _ROW_COUNT - 1))

    def compose(self) -> ComposeResult:
        with Vertical(id="effortpicker-content"):
            yield NoMarkupStatic("Effort", classes="effortpicker-title")
            yield NoMarkupStatic(
                "Model thinking uses provider reasoning. Vibe thinking adds independent "
                "strategic reasoning with its own effort and context: challenge direction, "
                "compare alternatives, define proof, and pivot when evidence disagrees. "
                "A fast self-check runs between full cycles; the selected 1-4 pass cycle "
                "runs at task start, every 10 turns, and after tool failures.",
                classes="effortpicker-subtitle",
            )
            yield Static(id="effortpicker-thinking", classes="effortpicker-row")
            yield Static(id="effortpicker-vibe-thinking", classes="effortpicker-row")
            yield Static(id="effortpicker-subagents", classes="effortpicker-row")
            yield Static(id="effortpicker-accuracy", classes="effortpicker-row")
            yield Static(id="effortpicker-web-search", classes="effortpicker-row")
            yield Static(id="effortpicker-experience", classes="effortpicker-row")
            yield Static(id="effortpicker-gauntlet", classes="effortpicker-row")
            yield Static(id="effortpicker-boost", classes="effortpicker-row")
            yield Static(id="effortpicker-ultracode", classes="effortpicker-row")
            yield NoMarkupStatic(
                shortcut_hint(
                    f"{shortcut('↑↓/jk')} Select  {shortcut('←→/hl')} Adjust  "
                    f"{shortcut('Enter')} Apply  {shortcut('g')} Gauntlet  "
                    f"{shortcut('b')} BOOST  "
                    f"{shortcut('u')} UltraCode  "
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
                self._vibe_thinking_index = max(0, self._vibe_thinking_index - 1)
            case 2:
                self._max_parallel_subagents = max(
                    MIN_PARALLEL_SUBAGENTS, self._max_parallel_subagents - 1
                )
            case 3:
                self._accuracy_index = max(0, self._accuracy_index - 1)
            case 4:
                self._web_search_index = max(0, self._web_search_index - 1)
            case 5:
                self._personal_experience = not self._personal_experience
            case 6:
                self._gauntlet_loop = not self._gauntlet_loop
            case 7:
                self._boost_mode = not self._boost_mode
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
                self._vibe_thinking_index = min(
                    len(THINKING_LEVELS) - 1, self._vibe_thinking_index + 1
                )
            case 2:
                self._max_parallel_subagents = min(
                    MAX_PARALLEL_SUBAGENTS, self._max_parallel_subagents + 1
                )
            case 3:
                self._accuracy_index = min(
                    len(ACCURACY_LEVELS) - 1, self._accuracy_index + 1
                )
            case 4:
                self._web_search_index = min(
                    len(WEB_SEARCH_ACTIVITY_LEVELS) - 1, self._web_search_index + 1
                )
            case 5:
                self._personal_experience = not self._personal_experience
            case 6:
                self._gauntlet_loop = not self._gauntlet_loop
            case 7:
                self._boost_mode = not self._boost_mode
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
                THINKING_LEVELS[self._vibe_thinking_index],
                self._max_parallel_subagents,
                ACCURACY_LEVELS[self._accuracy_index],
                WEB_SEARCH_ACTIVITY_LEVELS[self._web_search_index],
                self._gauntlet_loop,
                self._personal_experience,
                boost_mode=self._boost_mode,
                ultracode=False,
            )
        )

    def action_boost(self) -> None:
        self._boost_mode = not self._boost_mode
        self._refresh_rows()

    def action_ultracode(self) -> None:
        self.post_message(
            self.Applied(
                "max",
                "max",
                MAX_PARALLEL_SUBAGENTS,
                "max",
                "max",
                True,
                True,
                boost_mode=True,
                ultracode=True,
            )
        )

    def action_gauntlet(self) -> None:
        self._gauntlet_loop = not self._gauntlet_loop
        if self._gauntlet_loop and self._max_parallel_subagents == 0:
            self._max_parallel_subagents = 1
        self._refresh_rows()

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())

    def _refresh_rows(self) -> None:
        thinking = THINKING_LEVELS[self._thinking_index]
        thinking_filled = round(
            self._thinking_index / (len(THINKING_LEVELS) - 1) * _THINKING_BAR_WIDTH
        )
        thinking_row = Content.assemble(
            _row_prefix(self._selected_row == 0),
            ("Model think ", "bold"),
            "Min ",
            _slider_bar(thinking_filled, _THINKING_BAR_WIDTH),
            " Max  ",
            (thinking.upper(), "$primary bold"),
        )
        vibe_thinking = THINKING_LEVELS[self._vibe_thinking_index]
        vibe_thinking_filled = round(
            self._vibe_thinking_index / (len(THINKING_LEVELS) - 1) * _THINKING_BAR_WIDTH
        )
        vibe_thinking_row = Content.assemble(
            _row_prefix(self._selected_row == _VIBE_THINKING_ROW),
            ("Vibe think ", "bold"),
            "Off ",
            _slider_bar(vibe_thinking_filled, _THINKING_BAR_WIDTH),
            " Max  ",
            (vibe_thinking.upper(), "$primary bold"),
            f"  ({self._vibe_thinking_index} extra passes)",
        )
        subagents_row = Content.assemble(
            _row_prefix(self._selected_row == _SUBAGENT_ROW),
            ("Subagents  ", "bold"),
            _slider_bar(self._max_parallel_subagents, _SUBAGENT_BAR_WIDTH),
            f"  {self._max_parallel_subagents}/{MAX_PARALLEL_SUBAGENTS}",
            "  (0 = disabled)" if self._max_parallel_subagents == 0 else "",
        )
        accuracy = ACCURACY_LEVELS[self._accuracy_index]
        accuracy_filled = round(
            self._accuracy_index / (len(ACCURACY_LEVELS) - 1) * _THINKING_BAR_WIDTH
        )
        accuracy_row = Content.assemble(
            _row_prefix(self._selected_row == _ACCURACY_ROW),
            ("Accuracy   ", "bold"),
            "Min ",
            _slider_bar(accuracy_filled, _THINKING_BAR_WIDTH),
            " Max  ",
            (accuracy.upper(), "$primary bold"),
        )
        web_search = WEB_SEARCH_ACTIVITY_LEVELS[self._web_search_index]
        web_search_filled = round(
            self._web_search_index
            / (len(WEB_SEARCH_ACTIVITY_LEVELS) - 1)
            * _THINKING_BAR_WIDTH
        )
        web_search_row = Content.assemble(
            _row_prefix(self._selected_row == _WEB_SEARCH_ROW),
            ("Web search ", "bold"),
            "Off ",
            _slider_bar(web_search_filled, _THINKING_BAR_WIDTH),
            " Max  ",
            (web_search.upper(), "$primary bold"),
        )
        self.query_one("#effortpicker-thinking", Static).update(thinking_row)
        self.query_one("#effortpicker-vibe-thinking", Static).update(vibe_thinking_row)
        self.query_one("#effortpicker-subagents", Static).update(subagents_row)
        self.query_one("#effortpicker-accuracy", Static).update(accuracy_row)
        self.query_one("#effortpicker-web-search", Static).update(web_search_row)
        self.query_one("#effortpicker-experience", Static).update(
            _experience_row(
                selected=self._selected_row == _EXPERIENCE_ROW,
                enabled=self._personal_experience,
            )
        )
        self.query_one("#effortpicker-gauntlet", Static).update(
            _gauntlet_row(
                selected=self._selected_row == _GAUNTLET_ROW,
                enabled=self._gauntlet_loop,
            )
        )
        self.query_one("#effortpicker-boost", Static).update(
            _boost_row(
                selected=self._selected_row == _BOOST_ROW, enabled=self._boost_mode
            )
        )
        self.query_one("#effortpicker-ultracode", Static).update(
            _ultracode_row(selected=self._selected_row == _ULTRACODE_ROW)
        )


__all__ = ["EffortPickerApp"]
