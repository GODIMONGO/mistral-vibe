from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from vibe.core.tools.base import BaseToolState, InvokeContext, ToolError, ToolPermission
from vibe.core.tools.builtins import computer_use
from vibe.core.tools.builtins.computer_use import (
    ComputerAction,
    ComputerState,
    ComputerUse,
    ComputerUseArgs,
    ComputerUseConfig,
    ComputerUseResult,
    MouseButton,
    ScreenRect,
    WindowInfo,
)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def observe(self, *, max_windows: int, max_controls: int) -> ComputerState:
        self.calls.append(("observe", max_windows, max_controls))
        window = WindowInfo(
            hwnd=10,
            title="Editor",
            class_name="TestWindow",
            rect=ScreenRect(left=0, top=0, right=800, bottom=600),
            enabled=True,
        )
        return ComputerState(
            screen=ScreenRect(left=0, top=0, right=1920, bottom=1080),
            cursor_x=100,
            cursor_y=200,
            foreground_hwnd=10,
            foreground_title="Editor",
            windows=[window],
            controls=[],
        )

    def screenshot(self, path: Path) -> None:
        self.calls.append(("screenshot", path))

    def focus(self, hwnd: int) -> None:
        self.calls.append(("focus", hwnd))

    def click(self, x: int, y: int, button: MouseButton, clicks: int) -> None:
        self.calls.append(("click", x, y, button, clicks))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def press_keys(self, keys: list[str]) -> None:
        self.calls.append(("key", keys))

    def scroll(self, amount: int, x: int | None, y: int | None) -> None:
        self.calls.append(("scroll", amount, x, y))


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr(computer_use, "_create_backend", lambda: fake)
    return fake


@pytest.fixture
def tool(tmp_path: Path) -> ComputerUse:
    return ComputerUse(
        config_getter=lambda: ComputerUseConfig(),
        state=BaseToolState(),
        scratchpad_dir=tmp_path,
    )


async def invoke(tool: ComputerUse, **kwargs: Any) -> ComputerUseResult:
    items = [item async for item in tool.invoke(**kwargs)]
    assert len(items) == 1
    assert isinstance(items[0], ComputerUseResult)
    return items[0]


def test_available_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(computer_use, "is_windows", lambda: False)
    assert not ComputerUse.is_available()
    monkeypatch.setattr(computer_use, "is_windows", lambda: True)
    assert ComputerUse.is_available()


@pytest.mark.parametrize("action", [ComputerAction.OBSERVE, ComputerAction.SCREENSHOT])
def test_read_only_actions_are_always_allowed(action: ComputerAction) -> None:
    tool = ComputerUse(lambda: ComputerUseConfig(), BaseToolState())
    permission = tool.resolve_permission(ComputerUseArgs(action=action))
    assert permission is not None
    assert permission.permission is ToolPermission.ALWAYS


def test_mutating_actions_use_config_permission() -> None:
    tool = ComputerUse(lambda: ComputerUseConfig(), BaseToolState())
    assert (
        tool.resolve_permission(
            ComputerUseArgs(action=ComputerAction.TYPE, text="hello")
        )
        is None
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"action": "focus"}, "focus requires hwnd"),
        ({"action": "click", "x": 1}, "click requires x and y"),
        ({"action": "type"}, "type requires text"),
        ({"action": "key"}, "key requires at least one key"),
        ({"action": "scroll", "scroll_y": 0}, "scroll requires a non-zero"),
    ],
)
def test_action_validation(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ComputerUseArgs.model_validate(payload)


class FakeWindowApi:
    def __init__(self, class_name: str) -> None:
        self.class_name = class_name

    def GetClassName(self, _hwnd: int) -> str:
        return self.class_name


def test_terminal_windows_are_rejected() -> None:
    backend = computer_use.WindowsComputerBackend()
    with pytest.raises(ToolError, match="cannot control terminal windows"):
        backend._ensure_allowed_window(10, FakeWindowApi("ConsoleWindowClass"))


def test_ordinary_windows_are_allowed() -> None:
    backend = computer_use.WindowsComputerBackend()
    backend._ensure_allowed_window(10, FakeWindowApi("Notepad"))


def test_windows_key_is_rejected() -> None:
    backend = computer_use.WindowsComputerBackend()
    with pytest.raises(ToolError, match="Unsupported key"):
        backend._virtual_key("win")


@pytest.mark.asyncio
async def test_observe_returns_structured_state(
    tool: ComputerUse, backend: FakeBackend
) -> None:
    result = await invoke(tool, action="observe")
    assert result.state.foreground_title == "Editor"
    assert result.screenshot_path is None
    assert backend.calls == [("observe", 24, 80)]


@pytest.mark.asyncio
async def test_click_captures_and_observes_after_action(
    tool: ComputerUse, backend: FakeBackend, tmp_path: Path
) -> None:
    result = await invoke(tool, action="click", x=25, y=50, clicks=2)
    expected_path = tmp_path / "computer-use-latest.bmp"
    assert backend.calls == [
        ("click", 25, 50, MouseButton.LEFT, 2),
        ("screenshot", expected_path),
        ("observe", 24, 80),
    ]
    assert result.screenshot_path == str(expected_path)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"action": "focus", "hwnd": 10}, ("focus", 10)),
        ({"action": "type", "text": "Привет"}, ("type", "Привет")),
        ({"action": "key", "keys": ["ctrl", "s"]}, ("key", ["ctrl", "s"])),
        (
            {"action": "scroll", "scroll_y": -3, "x": 100, "y": 200},
            ("scroll", -3, 100, 200),
        ),
    ],
)
@pytest.mark.asyncio
async def test_mutating_actions(
    tool: ComputerUse,
    backend: FakeBackend,
    kwargs: dict[str, object],
    expected: tuple[object, ...],
) -> None:
    await invoke(tool, **kwargs)
    assert backend.calls[0] == expected


@pytest.mark.asyncio
async def test_capture_after_can_be_disabled(
    tool: ComputerUse, backend: FakeBackend
) -> None:
    result = await invoke(tool, action="key", keys=["escape"], capture_after=False)
    assert result.screenshot_path is None
    assert backend.calls == [("key", ["escape"]), ("observe", 24, 80)]


@pytest.mark.asyncio
async def test_text_limit_is_enforced(backend: FakeBackend, tmp_path: Path) -> None:
    tool = ComputerUse(
        lambda: ComputerUseConfig(max_text_chars=4),
        BaseToolState(),
        scratchpad_dir=tmp_path,
    )
    with pytest.raises(ToolError, match="limit is 4"):
        await invoke(tool, action="type", text="12345")
    assert backend.calls == []


@pytest.mark.asyncio
async def test_context_scratchpad_is_used_when_tool_has_no_path(
    backend: FakeBackend, tmp_path: Path
) -> None:
    tool = ComputerUse(lambda: ComputerUseConfig(), BaseToolState())
    ctx = InvokeContext(tool_call_id="call", scratchpad_dir=tmp_path)
    items: AsyncIterator[object] = tool.invoke(ctx, action="screenshot")
    result_items = [item async for item in items]
    assert isinstance(result_items[0], ComputerUseResult)
    assert result_items[0].screenshot_path == str(tmp_path / "computer-use-latest.bmp")
