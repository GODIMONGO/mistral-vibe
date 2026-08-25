from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from enum import StrEnum, auto
import ipaddress
import json
from pathlib import Path
import tempfile
from urllib.parse import quote, urlparse
from uuid import uuid4

import anyio
import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidURI

from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.permissions import PermissionContext
from vibe.core.types import FileImageSource, ImageAttachment, ToolStreamEvent
from vibe.utils.http import VibeAsyncHTTPClient
from vibe.utils.tool_presentation import ToolEffectKind

_BOX_COORDINATE_COUNT = 8


class ChromeCDPAction(StrEnum):
    LIST_TABS = auto()
    SNAPSHOT = auto()
    OPEN = auto()
    NAVIGATE = auto()
    CLICK = auto()
    TYPE = auto()
    SCREENSHOT = auto()
    EVALUATE = auto()


class ChromeCDPArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ChromeCDPAction
    target_id: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=8192)
    node_id: int | None = Field(default=None, gt=0)
    text: str | None = None
    expression: str | None = None
    clear: bool = True

    @model_validator(mode="after")
    def validate_action_arguments(self) -> ChromeCDPArgs:
        match self.action:
            case ChromeCDPAction.OPEN | ChromeCDPAction.NAVIGATE if self.url is None:
                raise ValueError(f"{self.action.value} requires url")
            case ChromeCDPAction.CLICK if self.node_id is None:
                raise ValueError("click requires node_id from snapshot")
            case ChromeCDPAction.TYPE if self.node_id is None or self.text is None:
                raise ValueError("type requires node_id from snapshot and text")
            case ChromeCDPAction.EVALUATE if self.expression is None:
                raise ValueError("evaluate requires expression")
        return self


class ChromeTab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    title: str
    url: str
    type: str


class ChromeAXNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: int
    role: str
    name: str
    value: str = ""
    description: str = ""
    disabled: bool = False
    focused: bool = False


class ChromeCDPResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ChromeCDPAction
    message: str
    target_id: str | None = None
    tabs: list[ChromeTab] = Field(default_factory=list)
    nodes: list[ChromeAXNode] = Field(default_factory=list)
    value_json: str | None = None
    truncated: bool = False
    screenshot_path: str | None = None


class ChromeCDPConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    endpoint: str = "http://127.0.0.1:9222"
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_tabs: int = Field(default=30, ge=1, le=200)
    max_snapshot_nodes: int = Field(default=250, ge=1, le=2000)
    max_snapshot_chars: int = Field(default=30_000, ge=1000, le=200_000)
    max_text_chars: int = Field(default=4000, ge=1, le=50_000)
    max_expression_chars: int = Field(default=20_000, ge=1, le=100_000)
    max_result_chars: int = Field(default=30_000, ge=100, le=200_000)
    max_screenshot_bytes: int = Field(default=8_000_000, ge=100_000, le=25_000_000)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _validate_loopback_url(value, websocket=False).rstrip("/")


class _Target(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""
    url: str = ""
    type: str = ""
    web_socket_debugger_url: str | None = Field(
        default=None, alias="webSocketDebuggerUrl"
    )


class _CDPError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: int
    message: str


class _CDPResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    result: dict[str, JsonValue] = Field(default_factory=dict)
    error: _CDPError | None = None


class _AXValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: JsonValue = None


class _AXProperty(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    value: _AXValue


class _AXNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    backend_dom_node_id: int | None = Field(default=None, alias="backendDOMNodeId")
    ignored: bool = False
    role: _AXValue | None = None
    name: _AXValue | None = None
    value: _AXValue | None = None
    description: _AXValue | None = None
    properties: list[_AXProperty] = Field(default_factory=list)


def _validate_loopback_url(value: str, *, websocket: bool) -> str:
    parsed = urlparse(value)
    allowed_schemes = {"ws"} if websocket else {"http"}
    if parsed.scheme not in allowed_schemes:
        raise ValueError(f"URL scheme must be {next(iter(allowed_schemes))}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL must not contain credentials, query, or fragment")
    if not websocket and parsed.path not in {"", "/"}:
        raise ValueError("CDP endpoint must not contain a path")
    if parsed.hostname is None:
        raise ValueError("URL must include a loopback IP address")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL must include a valid loopback IP and port") from exc
    if not address.is_loopback:
        raise ValueError("Chrome CDP is restricted to loopback IP addresses")
    if port is None:
        raise ValueError("URL must include an explicit port")
    return value


def _validate_page_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError("Page URL must be an absolute http or https URL")
    return value


def _text(value: _AXValue | None, *, limit: int = 500) -> str:
    if value is None or value.value is None:
        return ""
    rendered = str(value.value)
    return rendered if len(rendered) <= limit else rendered[:limit] + "…"


def _coordinate(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ToolError("Chrome returned invalid clickable coordinates")
    try:
        return float(value)
    except ValueError as exc:
        raise ToolError("Chrome returned invalid clickable coordinates") from exc


class _CDPSession:
    def __init__(self, websocket: ClientConnection, timeout_seconds: float) -> None:
        self._websocket = websocket
        self._timeout_seconds = timeout_seconds
        self._next_id = 1

    async def request(
        self, method: str, params: dict[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        request_id = self._next_id
        self._next_id += 1
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._websocket.send(
                    json.dumps({
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    })
                )
                while True:
                    payload = await self._websocket.recv()
                    if not isinstance(payload, str):
                        raise ToolError(
                            "Chrome CDP returned an unexpected binary message"
                        )
                    try:
                        decoded = json.loads(payload)
                        if isinstance(decoded, dict) and "id" not in decoded:
                            continue
                        response = _CDPResponse.model_validate(decoded)
                    except (json.JSONDecodeError, ValidationError) as exc:
                        raise ToolError(
                            "Chrome CDP returned an invalid response"
                        ) from exc
                    if response.id != request_id:
                        continue
                    if response.error is not None:
                        raise ToolError(
                            f"Chrome CDP error {response.error.code}: "
                            f"{response.error.message}"
                        )
                    return response.result
        except TimeoutError as exc:
            raise ToolError(
                f"Chrome CDP command timed out after {self._timeout_seconds:g} seconds"
            ) from exc


class ChromeCDP(
    BaseTool[ChromeCDPArgs, ChromeCDPResult, ChromeCDPConfig, BaseToolState]
):
    effect_kind = ToolEffectKind.TOOL

    def resolve_permission(self, args: ChromeCDPArgs) -> PermissionContext | None:
        if args.action in {
            ChromeCDPAction.LIST_TABS,
            ChromeCDPAction.SNAPSHOT,
            ChromeCDPAction.SCREENSHOT,
        }:
            return PermissionContext(permission=ToolPermission.ALWAYS)
        return None

    async def _http_json(self, path: str, *, method: str = "GET") -> JsonValue:
        url = f"{self.config.endpoint}{path}"
        limit = 1_000_000
        try:
            async with VibeAsyncHTTPClient(
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(),
            ) as client:
                async with client.stream(method, url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > limit:
                            raise ToolError("Chrome CDP HTTP response exceeded 1 MB")
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not reach local Chrome CDP: {exc}") from exc
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolError("Chrome CDP returned invalid JSON") from exc

    async def _targets(self) -> list[_Target]:
        payload = await self._http_json("/json/list")
        if not isinstance(payload, list):
            raise ToolError("Chrome CDP target list is invalid")
        try:
            return [_Target.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise ToolError("Chrome CDP target list is invalid") from exc

    async def _target(self, target_id: str | None) -> _Target:
        targets = await self._targets()
        if target_id is not None:
            target = next((item for item in targets if item.id == target_id), None)
        else:
            target = next((item for item in targets if item.type == "page"), None)
        if target is None:
            raise ToolError("Chrome target not found; call list_tabs first")
        if target.web_socket_debugger_url is None:
            raise ToolError("Chrome target has no debugger WebSocket URL")
        _validate_loopback_url(target.web_socket_debugger_url, websocket=True)
        return target

    @asynccontextmanager
    async def _session(
        self, target_id: str | None
    ) -> AsyncGenerator[tuple[_Target, _CDPSession], None]:
        target = await self._target(target_id)
        websocket_url = target.web_socket_debugger_url or ""
        max_message = max(
            self.config.max_screenshot_bytes * 2,
            self.config.max_snapshot_chars * 8,
            self.config.max_result_chars * 8,
        )
        try:
            async with connect(
                websocket_url,
                proxy=None,
                compression=None,
                open_timeout=self.config.timeout_seconds,
                close_timeout=min(self.config.timeout_seconds, 5),
                max_size=max_message,
                max_queue=8,
            ) as websocket:
                yield target, _CDPSession(websocket, self.config.timeout_seconds)
        except (ConnectionClosed, InvalidURI, OSError, TimeoutError) as exc:
            raise ToolError(f"Chrome CDP WebSocket failed: {exc}") from exc

    async def _snapshot(self, session: _CDPSession) -> tuple[list[ChromeAXNode], bool]:
        result = await session.request("Accessibility.getFullAXTree")
        raw_nodes = result.get("nodes", [])
        if not isinstance(raw_nodes, list):
            raise ToolError("Chrome accessibility snapshot is invalid")
        nodes: list[ChromeAXNode] = []
        used_chars = 0
        truncated = False
        for raw_node in raw_nodes:
            try:
                node = _AXNode.model_validate(raw_node)
            except ValidationError:
                continue
            if node.ignored or node.backend_dom_node_id is None:
                continue
            properties = {item.name: item.value.value for item in node.properties}
            output = ChromeAXNode(
                node_id=node.backend_dom_node_id,
                role=_text(node.role),
                name=_text(node.name),
                value=_text(node.value),
                description=_text(node.description),
                disabled=properties.get("disabled") is True,
                focused=properties.get("focused") is True,
            )
            output_chars = len(output.model_dump_json())
            if (
                len(nodes) >= self.config.max_snapshot_nodes
                or used_chars + output_chars > self.config.max_snapshot_chars
            ):
                truncated = True
                break
            nodes.append(output)
            used_chars += output_chars
        return nodes, truncated

    async def _screenshot_path(self, ctx: InvokeContext | None) -> Path:
        base = self.scratchpad_dir
        if base is None and ctx is not None:
            base = ctx.scratchpad_dir or ctx.session_dir
        if base is None:
            base = Path(tempfile.gettempdir()) / "vibe-chrome-cdp"
        path = base.resolve() / f"chrome-cdp-{uuid4().hex}.png"
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        return path

    async def _list_tabs(self, args: ChromeCDPArgs) -> ChromeCDPResult:
        targets = await self._targets()
        tabs = [
            ChromeTab(
                target_id=item.id,
                title=item.title[:500],
                url=item.url[:8192],
                type=item.type[:100],
            )
            for item in targets[: self.config.max_tabs]
        ]
        return ChromeCDPResult(
            action=args.action,
            message=f"Listed {len(tabs)} Chrome targets",
            tabs=tabs,
            truncated=len(targets) > len(tabs),
        )

    async def _open(self, args: ChromeCDPArgs) -> ChromeCDPResult:
        page_url = _validate_page_url(args.url or "")
        payload = await self._http_json(
            f"/json/new?{quote(page_url, safe='')}", method="PUT"
        )
        try:
            target = _Target.model_validate(payload)
        except ValidationError as exc:
            raise ToolError("Chrome returned an invalid new target") from exc
        return ChromeCDPResult(
            action=args.action, target_id=target.id, message=f"Opened {page_url}"
        )

    async def _click(self, session: _CDPSession, node_id: int) -> str:
        await session.request("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node_id})
        box = await session.request("DOM.getBoxModel", {"backendNodeId": node_id})
        model = box.get("model")
        content = model.get("content") if isinstance(model, dict) else None
        if not isinstance(content, list) or len(content) < _BOX_COORDINATE_COUNT:
            raise ToolError("Chrome did not return a clickable box for this node")
        coordinates = [_coordinate(item) for item in content[:_BOX_COORDINATE_COUNT]]
        pointer: dict[str, JsonValue] = {
            "x": sum(coordinates[0::2]) / 4,
            "y": sum(coordinates[1::2]) / 4,
            "button": "left",
            "clickCount": 1,
        }
        await session.request(
            "Input.dispatchMouseEvent", {"type": "mousePressed", **pointer}
        )
        await session.request(
            "Input.dispatchMouseEvent", {"type": "mouseReleased", **pointer}
        )
        return f"Clicked node {node_id}"

    async def _type(self, session: _CDPSession, args: ChromeCDPArgs) -> str:
        node_id = args.node_id or 0
        await session.request("DOM.focus", {"backendNodeId": node_id})
        if args.clear:
            select_all: dict[str, JsonValue] = {
                "key": "a",
                "code": "KeyA",
                "modifiers": 2,
            }
            await session.request(
                "Input.dispatchKeyEvent", {"type": "keyDown", **select_all}
            )
            await session.request(
                "Input.dispatchKeyEvent", {"type": "keyUp", **select_all}
            )
            for key_type in ("keyDown", "keyUp"):
                await session.request(
                    "Input.dispatchKeyEvent",
                    {"type": key_type, "key": "Backspace", "code": "Backspace"},
                )
        await session.request("Input.insertText", {"text": args.text or ""})
        return f"Typed {len(args.text or '')} characters into node {node_id}"

    async def _capture_screenshot(
        self,
        args: ChromeCDPArgs,
        target: _Target,
        session: _CDPSession,
        ctx: InvokeContext | None,
    ) -> ChromeCDPResult:
        captured = await session.request(
            "Page.captureScreenshot", {"format": "png", "fromSurface": True}
        )
        encoded = captured.get("data")
        if not isinstance(encoded, str):
            raise ToolError("Chrome screenshot response is invalid")
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ToolError("Chrome screenshot is not valid base64") from exc
        if len(image) > self.config.max_screenshot_bytes:
            raise ToolError(
                f"Chrome screenshot exceeds {self.config.max_screenshot_bytes} bytes"
            )
        path = await self._screenshot_path(ctx)
        await anyio.Path(path).write_bytes(image)
        return ChromeCDPResult(
            action=args.action,
            target_id=target.id,
            message=f"Captured Chrome screenshot ({len(image)} bytes)",
            screenshot_path=str(path),
        )

    async def _evaluate(
        self, args: ChromeCDPArgs, target: _Target, session: _CDPSession
    ) -> ChromeCDPResult:
        evaluated = await session.request(
            "Runtime.evaluate",
            {
                "expression": args.expression or "",
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if evaluated.get("exceptionDetails") is not None:
            raise ToolError("JavaScript evaluation failed")
        remote = evaluated.get("result")
        value = remote.get("value") if isinstance(remote, dict) else None
        rendered = json.dumps(value, ensure_ascii=False, default=str)
        truncated = len(rendered) > self.config.max_result_chars
        if truncated:
            rendered = rendered[: self.config.max_result_chars] + "…"
        return ChromeCDPResult(
            action=args.action,
            target_id=target.id,
            message=(
                "Evaluated JavaScript. The expression can read or change page and "
                "signed-in session data."
            ),
            value_json=rendered,
            truncated=truncated,
        )

    async def _run_on_target(
        self, args: ChromeCDPArgs, ctx: InvokeContext | None
    ) -> ChromeCDPResult:
        async with self._session(args.target_id) as (target, session):
            match args.action:
                case ChromeCDPAction.SNAPSHOT:
                    nodes, truncated = await self._snapshot(session)
                    return ChromeCDPResult(
                        action=args.action,
                        target_id=target.id,
                        message=f"Captured {len(nodes)} accessibility nodes",
                        nodes=nodes,
                        truncated=truncated,
                    )
                case ChromeCDPAction.NAVIGATE:
                    page_url = _validate_page_url(args.url or "")
                    await session.request("Page.navigate", {"url": page_url})
                    message = f"Navigated to {page_url}"
                case ChromeCDPAction.CLICK:
                    message = await self._click(session, args.node_id or 0)
                case ChromeCDPAction.TYPE:
                    message = await self._type(session, args)
                case ChromeCDPAction.SCREENSHOT:
                    return await self._capture_screenshot(args, target, session, ctx)
                case ChromeCDPAction.EVALUATE:
                    return await self._evaluate(args, target, session)
                case _:
                    raise ToolError(f"Unsupported Chrome CDP action: {args.action}")
            return ChromeCDPResult(
                action=args.action, target_id=target.id, message=message
            )

    async def run(
        self, args: ChromeCDPArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | ChromeCDPResult, None]:
        if args.text is not None and len(args.text) > self.config.max_text_chars:
            raise ToolError(f"Text exceeds {self.config.max_text_chars} characters")
        if (
            args.expression is not None
            and len(args.expression) > self.config.max_expression_chars
        ):
            raise ToolError(
                f"JavaScript expression exceeds {self.config.max_expression_chars} characters"
            )
        if args.action is ChromeCDPAction.LIST_TABS:
            yield await self._list_tabs(args)
        elif args.action is ChromeCDPAction.OPEN:
            yield await self._open(args)
        else:
            yield await self._run_on_target(args, ctx)

    def get_result_images(self, result: ChromeCDPResult) -> list[ImageAttachment]:
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
