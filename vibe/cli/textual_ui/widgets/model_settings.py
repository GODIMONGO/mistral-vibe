from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from vibe.cli.textual_ui.shortcut_hints import shortcut, shortcut_hint
from vibe.cli.textual_ui.widgets.navigable_option_list import NavigableOptionList
from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic


def _option(label: str, value: str, option_id: str) -> Option:
    text = Text(no_wrap=True)
    text.append(f"  {label:<18}", style="bold")
    text.append(value, style="dim")
    return Option(text, id=option_id)


class ModelSettingsApp(Container):
    """Compact hub for model roles and related reasoning settings."""

    can_focus_children = True
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    class Selected(Message):
        def __init__(self, action: str) -> None:
            self.action = action
            super().__init__()

    class Cancelled(Message):
        pass

    def __init__(
        self,
        *,
        main_model: str,
        advisor_model: str,
        reviewer_model: str,
        thinking: str,
        vibe_thinking: str,
        max_subagents: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(id="modelsettings-app", **kwargs)
        self._options = (
            _option("Main", main_model, "main"),
            _option("Goal advisor", advisor_model, "advisor"),
            _option("Reviewer", reviewer_model, "reviewer"),
            _option("Model thinking", thinking, "thinking"),
            _option(
                "Effort & autonomy",
                f"Vibe {vibe_thinking} · {max_subagents} subagents",
                "effort",
            ),
            _option("OpenCode Go", "connect / replace API key", "opencode-go"),
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="modelsettings-content"):
            yield NoMarkupStatic("Model Control Center", classes="modelsettings-title")
            yield NoMarkupStatic(
                "Assign different models to work, planning, and independent review.",
                classes="modelsettings-subtitle",
            )
            yield NavigableOptionList(*self._options, id="modelsettings-options")
            yield NoMarkupStatic(
                shortcut_hint(
                    f"{shortcut('↑↓/jk')} Navigate  {shortcut('Enter')} Open  "
                    f"{shortcut('Esc')} Close"
                ),
                classes="modelsettings-help",
            )

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if not option_id:
            return
        event.stop()
        event.prevent_default()
        self.call_after_refresh(self.post_message, self.Selected(option_id))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())


__all__ = ["ModelSettingsApp"]
