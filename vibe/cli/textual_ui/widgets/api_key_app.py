from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.message import Message
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from vibe.cli.textual_ui.shortcut_hints import shortcut, shortcut_hint
from vibe.cli.textual_ui.widgets.navigable_option_list import NavigableOptionList
from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic
from vibe.cli.textual_ui.widgets.vscode_compat import VscodeCompatInput


@dataclass(frozen=True, slots=True)
class ApiKeyModelOption:
    alias: str
    display_name: str


class ApiKeyApp(Container):
    can_focus = True
    can_focus_children = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Cancel", show=False)
    ]

    class ModelSelected(Message):
        def __init__(self, alias: str) -> None:
            self.alias = alias
            super().__init__()

    class Submitted(Message):
        def __init__(self, alias: str, api_key: str) -> None:
            self.alias = alias
            self.api_key = api_key
            super().__init__()

    class Cancelled(Message):
        pass

    def __init__(
        self,
        models: list[ApiKeyModelOption],
        *,
        selected_alias: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(id="apikey-app", **kwargs)
        self._models = models
        self._selected_alias = selected_alias

    def compose(self) -> ComposeResult:
        with Vertical(id="apikey-content"):
            yield NoMarkupStatic("Add API Key", classes="apikey-title")
            if self._selected_alias is None:
                yield NavigableOptionList(
                    *(
                        Option(self._model_label(model), id=model.alias)
                        for model in self._models
                    ),
                    id="apikey-models",
                )
                help_text = (
                    f"{shortcut('↑↓/jk')} Navigate  {shortcut('Enter')} Select  "
                    f"{shortcut('Esc')} Cancel"
                )
            else:
                display_name = next(
                    (
                        model.display_name
                        for model in self._models
                        if model.alias == self._selected_alias
                    ),
                    self._selected_alias,
                )
                yield NoMarkupStatic(
                    f"Model: {display_name} ({self._selected_alias})",
                    classes="apikey-model-label",
                )
                yield VscodeCompatInput(
                    password=True, placeholder="Paste API key", id="apikey-input"
                )
                help_text = (
                    f"{shortcut('Enter')} Save securely  {shortcut('Esc')} Cancel"
                )
            yield NoMarkupStatic(shortcut_hint(help_text), classes="apikey-help")

    @staticmethod
    def _model_label(model: ApiKeyModelOption) -> Text:
        label = Text(no_wrap=True)
        label.append(model.display_name)
        label.append(f"  {model.alias}", style="dim")
        return label

    def focus(self, scroll_visible: bool = True) -> ApiKeyApp:
        if self._selected_alias is None:
            self.query_one(NavigableOptionList).focus(scroll_visible=scroll_visible)
        else:
            self.query_one("#apikey-input", Input).focus(scroll_visible=scroll_visible)
        return self

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.post_message(self.ModelSelected(event.option.id))

    async def select_model(self, alias: str) -> None:
        self._selected_alias = alias
        display_name = next(
            (model.display_name for model in self._models if model.alias == alias),
            alias,
        )
        content = self.query_one("#apikey-content", Vertical)
        await content.remove_children()
        await content.mount(
            NoMarkupStatic("Add API Key", classes="apikey-title"),
            NoMarkupStatic(
                f"Model: {display_name} ({alias})", classes="apikey-model-label"
            ),
            VscodeCompatInput(
                password=True, placeholder="Paste API key", id="apikey-input"
            ),
            NoMarkupStatic(
                shortcut_hint(
                    f"{shortcut('Enter')} Save securely  {shortcut('Esc')} Cancel"
                ),
                classes="apikey-help",
            ),
        )
        self.call_after_refresh(self.focus)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        api_key = event.value.strip()
        if not api_key or self._selected_alias is None:
            return
        event.input.value = ""
        self.post_message(self.Submitted(self._selected_alias, api_key))

    def action_close(self) -> None:
        input_widget = self.query("#apikey-input").first(Input)
        if input_widget is not None:
            input_widget.value = ""
        self.post_message(self.Cancelled())
