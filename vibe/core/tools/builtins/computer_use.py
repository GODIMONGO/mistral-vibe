from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import ctypes
from enum import StrEnum, auto
import importlib
from pathlib import Path
import struct
import tempfile
from typing import Any, ClassVar, Protocol
import zlib

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.permissions import PermissionContext
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import (
    FileImageSource,
    ImageAttachment,
    ToolResultEvent,
    ToolStreamEvent,
)
from vibe.core.utils import is_windows
from vibe.utils.tool_presentation import ToolEffectKind


class ComputerAction(StrEnum):
    OBSERVE = auto()
    SCREENSHOT = auto()
    FOCUS = auto()
    CLICK = auto()
    TYPE = auto()
    KEY = auto()
    SCROLL = auto()


class MouseButton(StrEnum):
    LEFT = auto()
    RIGHT = auto()
    MIDDLE = auto()


class ComputerUseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ComputerAction
    hwnd: int | None = Field(default=None, gt=0)
    x: int | None = None
    y: int | None = None
    button: MouseButton = MouseButton.LEFT
    clicks: int = Field(default=1, ge=1, le=3)
    text: str | None = None
    keys: list[str] = Field(default_factory=list, max_length=4)
    scroll_y: int | None = Field(default=None, ge=-100, le=100)
    capture_after: bool = True

    @model_validator(mode="after")
    def validate_action_arguments(self) -> ComputerUseArgs:
        match self.action:
            case ComputerAction.FOCUS if self.hwnd is None:
                raise ValueError("focus requires hwnd")
            case ComputerAction.CLICK if self.x is None or self.y is None:
                raise ValueError("click requires x and y")
            case ComputerAction.TYPE if self.text is None:
                raise ValueError("type requires text")
            case ComputerAction.KEY if not self.keys:
                raise ValueError("key requires at least one key")
            case ComputerAction.SCROLL if not self.scroll_y:
                raise ValueError("scroll requires a non-zero scroll_y")
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together")
        return self


class ScreenRect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: int
    top: int
    right: int
    bottom: int


class WindowInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hwnd: int
    title: str
    class_name: str
    rect: ScreenRect
    enabled: bool


class ComputerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen: ScreenRect
    cursor_x: int
    cursor_y: int
    foreground_hwnd: int | None
    foreground_title: str
    windows: list[WindowInfo]
    controls: list[WindowInfo]
    windows_truncated: bool = False
    controls_truncated: bool = False


class ComputerUseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ComputerAction
    message: str
    state: ComputerState
    screenshot_path: str | None = None


class ComputerUseConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    max_windows: int = Field(default=24, ge=1, le=100)
    max_controls: int = Field(default=80, ge=1, le=500)
    max_text_chars: int = Field(default=4000, ge=1, le=20000)


class ComputerBackend(Protocol):
    def observe(self, *, max_windows: int, max_controls: int) -> ComputerState: ...

    def screenshot(self, path: Path) -> None: ...

    def focus(self, hwnd: int) -> None: ...

    def click(self, x: int, y: int, button: MouseButton, clicks: int) -> None: ...

    def type_text(self, text: str) -> None: ...

    def press_keys(self, keys: list[str]) -> None: ...

    def scroll(self, amount: int, x: int | None, y: int | None) -> None: ...


def _win32_modules() -> tuple[Any, Any, Any, Any]:
    return (
        importlib.import_module("win32api"),
        importlib.import_module("win32con"),
        importlib.import_module("win32gui"),
        importlib.import_module("win32ui"),
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _parse_bmp_header(data: bytes) -> tuple[int, int, int, int, int, bool]:
    bitmap_header_size = 54
    dib_header_size = 40
    if len(data) < bitmap_header_size or data[:2] != b"BM":
        raise ToolError("Windows screenshot did not produce a valid BMP")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    dib_size = struct.unpack_from("<I", data, 14)[0]
    width, signed_height = struct.unpack_from("<ii", data, 18)
    planes, bits_per_pixel = struct.unpack_from("<HH", data, 26)
    compression = struct.unpack_from("<I", data, 30)[0]
    invalid_dimensions = dib_size < dib_header_size or width <= 0 or signed_height == 0
    invalid_encoding = planes != 1 or bits_per_pixel not in {24, 32} or compression != 0
    if invalid_dimensions or invalid_encoding:
        raise ToolError("Unsupported Windows screenshot bitmap format")
    height = abs(signed_height)
    source_stride = ((width * bits_per_pixel + 31) // 32) * 4
    if pixel_offset + source_stride * height > len(data):
        raise ToolError("Windows screenshot bitmap is truncated")
    return width, height, bits_per_pixel, pixel_offset, source_stride, signed_height < 0


def _bmp_scanlines(
    data: bytes,
    *,
    width: int,
    height: int,
    bits_per_pixel: int,
    pixel_offset: int,
    source_stride: int,
    top_down: bool,
) -> bytes:
    rows = range(height) if top_down else range(height - 1, -1, -1)
    channels = bits_per_pixel // 8
    scanlines = bytearray()
    for row in rows:
        start = pixel_offset + row * source_stride
        source = data[start : start + width * channels]
        scanlines.append(0)
        for offset in range(0, len(source), channels):
            blue, green, red = source[offset : offset + 3]
            scanlines.extend((red, green, blue))
    return bytes(scanlines)


def _bmp_to_png(data: bytes) -> bytes:
    """Convert an uncompressed 24/32-bit Win32 BMP capture to RGB PNG."""
    width, height, bits_per_pixel, pixel_offset, source_stride, top_down = (
        _parse_bmp_header(data)
    )

    scanlines = _bmp_scanlines(
        data,
        width=width,
        height=height,
        bits_per_pixel=bits_per_pixel,
        pixel_offset=pixel_offset,
        source_stride=source_stride,
        top_down=top_down,
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    signature = b"\x89PNG\r\n\x1a\n"
    return (
        signature
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=6))
        + _png_chunk(b"IEND", b"")
    )


class WindowsComputerBackend:
    _MAX_FUNCTION_KEY = 24
    _RESTRICTED_WINDOW_CLASSES = frozenset({
        "cascadia_hosting_window_class",
        "consolewindowclass",
        "mintty",
        "pseudoconsolewindow",
    })
    _KEY_NAMES: ClassVar[dict[str, int]] = {
        "alt": 0x12,
        "backspace": 0x08,
        "ctrl": 0x11,
        "delete": 0x2E,
        "down": 0x28,
        "end": 0x23,
        "enter": 0x0D,
        "escape": 0x1B,
        "home": 0x24,
        "insert": 0x2D,
        "left": 0x25,
        "pagedown": 0x22,
        "pageup": 0x21,
        "right": 0x27,
        "shift": 0x10,
        "space": 0x20,
        "tab": 0x09,
        "up": 0x26,
    }

    @staticmethod
    def _rect(raw: tuple[int, int, int, int]) -> ScreenRect:
        return ScreenRect(left=raw[0], top=raw[1], right=raw[2], bottom=raw[3])

    @staticmethod
    def _text(value: str, limit: int = 240) -> str:
        return value.replace("\x00", "").strip()[:limit]

    def _window_info(self, hwnd: int, win32gui: Any) -> WindowInfo | None:
        try:
            rect = win32gui.GetWindowRect(hwnd)
            return WindowInfo(
                hwnd=hwnd,
                title=self._text(win32gui.GetWindowText(hwnd)),
                class_name=self._text(win32gui.GetClassName(hwnd), 120),
                rect=self._rect(rect),
                enabled=bool(win32gui.IsWindowEnabled(hwnd)),
            )
        except Exception:
            return None

    def _ensure_allowed_window(self, hwnd: int, win32gui: Any) -> None:
        try:
            class_name = str(win32gui.GetClassName(hwnd)).strip().lower()
        except Exception as exc:
            raise ToolError(f"Cannot inspect target window {hwnd}") from exc
        if class_name in self._RESTRICTED_WINDOW_CLASSES:
            raise ToolError(
                "computer_use cannot control terminal windows; use a permissioned "
                "shell tool instead"
            )

    def _visible_windows(
        self, win32gui: Any, limit: int
    ) -> tuple[list[WindowInfo], bool]:
        windows: list[WindowInfo] = []
        found = 0

        def collect(hwnd: int, _extra: object) -> bool:
            nonlocal found
            if not win32gui.IsWindowVisible(hwnd):
                return True
            info = self._window_info(hwnd, win32gui)
            if info is None or not info.title:
                return True
            found += 1
            if len(windows) < limit:
                windows.append(info)
            return True

        win32gui.EnumWindows(collect, None)
        return windows, found > limit

    def _controls(
        self, win32gui: Any, foreground: int, limit: int
    ) -> tuple[list[WindowInfo], bool]:
        controls: list[WindowInfo] = []
        found = 0

        def collect(hwnd: int, _extra: object) -> bool:
            nonlocal found
            if not win32gui.IsWindowVisible(hwnd):
                return True
            info = self._window_info(hwnd, win32gui)
            if info is None:
                return True
            found += 1
            if len(controls) < limit:
                controls.append(info)
            return True

        if foreground:
            win32gui.EnumChildWindows(foreground, collect, None)
        return controls, found > limit

    @staticmethod
    def _screen_rect(win32api: Any, win32con: Any) -> ScreenRect:
        left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        width = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        height = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        return ScreenRect(left=left, top=top, right=left + width, bottom=top + height)

    def observe(self, *, max_windows: int, max_controls: int) -> ComputerState:
        win32api, win32con, win32gui, _ = _win32_modules()
        screen = self._screen_rect(win32api, win32con)
        cursor_x, cursor_y = win32api.GetCursorPos()
        foreground = int(win32gui.GetForegroundWindow())
        windows, windows_truncated = self._visible_windows(win32gui, max_windows)
        controls, controls_truncated = self._controls(
            win32gui, foreground, max_controls
        )
        foreground_title = ""
        if foreground:
            foreground_title = self._text(win32gui.GetWindowText(foreground))
        return ComputerState(
            screen=screen,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            foreground_hwnd=foreground or None,
            foreground_title=foreground_title,
            windows=windows,
            controls=controls,
            windows_truncated=windows_truncated,
            controls_truncated=controls_truncated,
        )

    def screenshot(self, path: Path) -> None:
        win32api, win32con, win32gui, win32ui = _win32_modules()
        left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        width = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        height = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        desktop = win32gui.GetDesktopWindow()
        desktop_dc = win32gui.GetWindowDC(desktop)
        source_dc = win32ui.CreateDCFromHandle(desktop_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            memory_dc.SelectObject(bitmap)
            memory_dc.BitBlt(
                (0, 0), (width, height), source_dc, (left, top), win32con.SRCCOPY
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            bmp_path = path.with_suffix(".capture.bmp")
            bitmap.SaveBitmapFile(memory_dc, str(bmp_path))
            try:
                path.write_bytes(_bmp_to_png(bmp_path.read_bytes()))
            finally:
                bmp_path.unlink(missing_ok=True)
        finally:
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(desktop, desktop_dc)
            win32gui.DeleteObject(bitmap.GetHandle())

    def focus(self, hwnd: int) -> None:
        _, win32con, win32gui, _ = _win32_modules()
        if not win32gui.IsWindow(hwnd):
            raise ToolError(f"Window {hwnd} no longer exists")
        self._ensure_allowed_window(hwnd, win32gui)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)

    def _foreground_window(self, win32gui: Any) -> int:
        hwnd = int(win32gui.GetForegroundWindow())
        if not hwnd:
            raise ToolError("No foreground window is available")
        self._ensure_allowed_window(hwnd, win32gui)
        return hwnd

    def _validate_point(self, x: int, y: int) -> None:
        state = self.observe(max_windows=1, max_controls=1)
        screen = state.screen
        if not (screen.left <= x < screen.right and screen.top <= y < screen.bottom):
            raise ToolError(
                f"Point ({x}, {y}) is outside the virtual screen "
                f"({screen.left}, {screen.top})-({screen.right}, {screen.bottom})"
            )

    def click(self, x: int, y: int, button: MouseButton, clicks: int) -> None:
        win32api, win32con, win32gui, _ = _win32_modules()
        self._validate_point(x, y)
        target = int(win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), 2))
        if target:
            self._ensure_allowed_window(target, win32gui)
        flags = {
            MouseButton.LEFT: (
                win32con.MOUSEEVENTF_LEFTDOWN,
                win32con.MOUSEEVENTF_LEFTUP,
            ),
            MouseButton.RIGHT: (
                win32con.MOUSEEVENTF_RIGHTDOWN,
                win32con.MOUSEEVENTF_RIGHTUP,
            ),
            MouseButton.MIDDLE: (
                win32con.MOUSEEVENTF_MIDDLEDOWN,
                win32con.MOUSEEVENTF_MIDDLEUP,
            ),
        }[button]
        win32api.SetCursorPos((x, y))
        for _ in range(clicks):
            win32api.mouse_event(flags[0], 0, 0, 0, 0)
            win32api.mouse_event(flags[1], 0, 0, 0, 0)

    @staticmethod
    def _unicode_units(text: str) -> list[int]:
        encoded = text.encode("utf-16-le", "surrogatepass")
        return [
            int.from_bytes(encoded[i : i + 2], "little")
            for i in range(0, len(encoded), 2)
        ]

    def type_text(self, text: str) -> None:
        from ctypes import wintypes

        _, _, win32gui, _ = _win32_modules()
        self._foreground_window(win32gui)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        ulong_ptr = getattr(wintypes, "ULONG_PTR", wintypes.WPARAM)

        class KeyInput(ctypes.Structure):
            _fields_ = (
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            )

        class InputUnion(ctypes.Union):
            _fields_ = (("ki", KeyInput),)

        class Input(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = (("type", wintypes.DWORD), ("value", InputUnion))

        events: list[Input] = []
        for unit in self._unicode_units(text):
            events.append(Input(type=1, ki=KeyInput(0, unit, 0x0004, 0, 0)))
            events.append(Input(type=1, ki=KeyInput(0, unit, 0x0006, 0, 0)))
        if not events:
            return
        array = (Input * len(events))(*events)
        sent = user32.SendInput(len(events), array, ctypes.sizeof(Input))
        if sent != len(events):
            raise ToolError(f"Windows accepted only {sent} of {len(events)} key events")

    def _virtual_key(self, key: str) -> int:
        normalized = key.strip().lower().replace("_", "").replace("-", "")
        if normalized in self._KEY_NAMES:
            return self._KEY_NAMES[normalized]
        if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
            return ord(normalized.upper())
        if normalized.startswith("f") and normalized[1:].isdigit():
            number = int(normalized[1:])
            if 1 <= number <= self._MAX_FUNCTION_KEY:
                return 0x70 + number - 1
        raise ToolError(f"Unsupported key: {key}")

    def press_keys(self, keys: list[str]) -> None:
        win32api, win32con, win32gui, _ = _win32_modules()
        self._foreground_window(win32gui)
        virtual_keys = [self._virtual_key(key) for key in keys]
        for key in virtual_keys:
            win32api.keybd_event(key, 0, 0, 0)
        for key in reversed(virtual_keys):
            win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)

    def scroll(self, amount: int, x: int | None, y: int | None) -> None:
        win32api, win32con, win32gui, _ = _win32_modules()
        self._foreground_window(win32gui)
        if x is not None and y is not None:
            self._validate_point(x, y)
            target = int(win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), 2))
            if target:
                self._ensure_allowed_window(target, win32gui)
            win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, amount * 120, 0)


def _create_backend() -> ComputerBackend:
    if not is_windows():
        raise ToolError("computer_use is available only on Windows")
    try:
        _win32_modules()
    except ImportError as exc:
        raise ToolError("computer_use requires the Windows pywin32 runtime") from exc
    return WindowsComputerBackend()


class ComputerUse(
    BaseTool[ComputerUseArgs, ComputerUseResult, ComputerUseConfig, BaseToolState],
    ToolUIData[ComputerUseArgs, ComputerUseResult],
):
    effect_kind = ToolEffectKind.TOOL

    @classmethod
    def is_available(cls, config: object | None = None) -> bool:
        return is_windows()

    def resolve_permission(self, args: ComputerUseArgs) -> PermissionContext | None:
        if args.action in {ComputerAction.OBSERVE, ComputerAction.SCREENSHOT}:
            return PermissionContext(permission=ToolPermission.ALWAYS)
        return None

    def _screenshot_path(self, ctx: InvokeContext | None) -> Path:
        base = self.scratchpad_dir
        if base is None and ctx is not None:
            base = ctx.scratchpad_dir or ctx.session_dir
        if base is None:
            base = Path(tempfile.gettempdir()) / "vibe-computer-use"
        return base.resolve() / "computer-use-latest.png"

    def _run_sync(
        self, backend: ComputerBackend, args: ComputerUseArgs, ctx: InvokeContext | None
    ) -> ComputerUseResult:
        screenshot_path: Path | None = None
        match args.action:
            case ComputerAction.OBSERVE:
                message = "Observed the Windows desktop"
            case ComputerAction.SCREENSHOT:
                message = "Captured the Windows desktop"
                screenshot_path = self._screenshot_path(ctx)
                backend.screenshot(screenshot_path)
            case ComputerAction.FOCUS:
                backend.focus(args.hwnd or 0)
                message = f"Focused window {args.hwnd}"
            case ComputerAction.CLICK:
                backend.click(args.x or 0, args.y or 0, args.button, args.clicks)
                message = f"Clicked {args.button.value} at ({args.x}, {args.y})"
            case ComputerAction.TYPE:
                text = args.text or ""
                if len(text) > self.config.max_text_chars:
                    raise ToolError(
                        f"Text has {len(text)} characters; limit is "
                        f"{self.config.max_text_chars}"
                    )
                backend.type_text(text)
                message = f"Typed {len(text)} characters"
            case ComputerAction.KEY:
                backend.press_keys(args.keys)
                message = f"Pressed {'+'.join(args.keys)}"
            case ComputerAction.SCROLL:
                backend.scroll(args.scroll_y or 0, args.x, args.y)
                message = f"Scrolled {args.scroll_y} steps"
        if args.capture_after and args.action not in {
            ComputerAction.OBSERVE,
            ComputerAction.SCREENSHOT,
        }:
            screenshot_path = self._screenshot_path(ctx)
            backend.screenshot(screenshot_path)
        state = backend.observe(
            max_windows=self.config.max_windows, max_controls=self.config.max_controls
        )
        return ComputerUseResult(
            action=args.action,
            message=message,
            state=state,
            screenshot_path=str(screenshot_path) if screenshot_path else None,
        )

    async def run(
        self, args: ComputerUseArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | ComputerUseResult, None]:
        backend = _create_backend()
        yield await asyncio.to_thread(self._run_sync, backend, args, ctx)

    def get_result_images(self, result: ComputerUseResult) -> list[ImageAttachment]:
        if result.screenshot_path is None:
            return []
        path = Path(result.screenshot_path)
        if not path.is_file():
            return []
        return [
            ImageAttachment(
                source=FileImageSource(path=path),
                alias=path.name,
                mime_type="image/png",
            )
        ]

    @classmethod
    def format_call_display(cls, args: ComputerUseArgs) -> ToolCallDisplay:
        message = args.action.value.replace("_", " ")
        return ToolCallDisplay(
            summary=f"Using computer: {message}",
            verb="Using",
            message=f"computer ({message})",
            settled_verb="Used",
            settled_message=f"computer ({message})",
            status_text="Computer use running",
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        if not isinstance(event.result, ComputerUseResult):
            return ToolResultDisplay(
                success=False, message=event.error or event.skip_reason or "No result"
            )
        return ToolResultDisplay(
            success=True, verb="Completed", message=event.result.action.value
        )

    @classmethod
    def get_status_text(cls) -> str:
        return "Computer use running"
