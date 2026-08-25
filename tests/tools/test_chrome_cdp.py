from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from vibe.core.tools.base import BaseToolState, ToolError, ToolPermission
from vibe.core.tools.builtins.chrome_cdp import (
    ChromeCDP,
    ChromeCDPAction,
    ChromeCDPArgs,
    ChromeCDPConfig,
    ChromeCDPResult,
    _CDPSession,
    _Target,
)
from vibe.core.types import FileImageSource


class FakeSession:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        return self.responses.get(method, {})


def make_tool(tmp_path: Path, **config: Any) -> ChromeCDP:
    resolved = ChromeCDPConfig(**config)
    return ChromeCDP(
        config_getter=lambda: resolved, state=BaseToolState(), scratchpad_dir=tmp_path
    )


async def run_tool(tool: ChromeCDP, args: ChromeCDPArgs) -> ChromeCDPResult:
    results = [item async for item in tool.run(args)]
    assert len(results) == 1
    assert isinstance(results[0], ChromeCDPResult)
    return results[0]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:9222",
        "http://192.168.1.2:9222",
        "https://127.0.0.1:9222",
        "http://127.0.0.1:9222/devtools",
        "http://127.0.0.1",
        "http://user:pass@127.0.0.1:9222",
    ],
)
def test_endpoint_must_be_explicit_loopback_ip(endpoint: str) -> None:
    with pytest.raises(ValueError):
        ChromeCDPConfig(endpoint=endpoint)


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:9222", "http://[::1]:9222"])
def test_endpoint_accepts_ipv4_and_ipv6_loopback(endpoint: str) -> None:
    assert ChromeCDPConfig(endpoint=endpoint).endpoint == endpoint


@pytest.mark.parametrize(
    "action",
    [ChromeCDPAction.LIST_TABS, ChromeCDPAction.SNAPSHOT, ChromeCDPAction.SCREENSHOT],
)
def test_read_only_actions_are_always_allowed(action: ChromeCDPAction) -> None:
    tool = ChromeCDP(lambda: ChromeCDPConfig(), BaseToolState())
    permission = tool.resolve_permission(ChromeCDPArgs(action=action))
    assert permission is not None
    assert permission.permission is ToolPermission.ALWAYS


@pytest.mark.parametrize(
    "args",
    [
        ChromeCDPArgs(action=ChromeCDPAction.OPEN, url="https://example.com"),
        ChromeCDPArgs(action=ChromeCDPAction.NAVIGATE, url="https://example.com"),
        ChromeCDPArgs(action=ChromeCDPAction.CLICK, node_id=1),
        ChromeCDPArgs(action=ChromeCDPAction.TYPE, node_id=1, text="x"),
        ChromeCDPArgs(action=ChromeCDPAction.EVALUATE, expression="document.title"),
    ],
)
def test_mutating_actions_use_normal_tool_permission(args: ChromeCDPArgs) -> None:
    tool = ChromeCDP(
        lambda: ChromeCDPConfig(permission=ToolPermission.ALWAYS), BaseToolState()
    )
    assert tool.resolve_permission(args) is None
    assert tool.config.permission is ToolPermission.ALWAYS


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "open"},
        {"action": "navigate"},
        {"action": "click"},
        {"action": "type", "node_id": 1},
        {"action": "evaluate"},
    ],
)
def test_action_specific_arguments_are_required(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ChromeCDPArgs.model_validate(payload)


@pytest.mark.asyncio
async def test_list_tabs_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = make_tool(tmp_path, max_tabs=1)

    async def targets(_self: ChromeCDP) -> list[_Target]:
        return [
            _Target(id="1", title="one", url="https://one.test", type="page"),
            _Target(id="2", title="two", url="https://two.test", type="page"),
        ]

    monkeypatch.setattr(ChromeCDP, "_targets", targets)
    result = await run_tool(tool, ChromeCDPArgs(action=ChromeCDPAction.LIST_TABS))
    assert [tab.target_id for tab in result.tabs] == ["1"]
    assert result.truncated


@pytest.mark.asyncio
async def test_target_rejects_non_loopback_websocket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = make_tool(tmp_path)

    async def targets(_self: ChromeCDP) -> list[_Target]:
        return [
            _Target(
                id="1",
                type="page",
                webSocketDebuggerUrl="ws://10.0.0.2:9222/devtools/page/1",
            )
        ]

    monkeypatch.setattr(ChromeCDP, "_targets", targets)
    with pytest.raises(ValueError, match="loopback"):
        await tool._target(None)


@pytest.mark.asyncio
async def test_snapshot_is_bounded_by_node_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = make_tool(tmp_path, max_snapshot_nodes=1)
    session = FakeSession({
        "Accessibility.getFullAXTree": {
            "nodes": [
                {
                    "backendDOMNodeId": 10,
                    "role": {"value": "button"},
                    "name": {"value": "Save"},
                },
                {
                    "backendDOMNodeId": 11,
                    "role": {"value": "textbox"},
                    "name": {"value": "Email"},
                },
            ]
        }
    })

    @asynccontextmanager
    async def fake_session(
        _self: ChromeCDP, _target_id: str | None
    ) -> AsyncGenerator[tuple[_Target, Any], None]:
        yield _Target(id="tab", type="page"), session

    monkeypatch.setattr(ChromeCDP, "_session", fake_session)
    result = await run_tool(tool, ChromeCDPArgs(action=ChromeCDPAction.SNAPSHOT))
    assert [node.node_id for node in result.nodes] == [10]
    assert result.truncated


@pytest.mark.asyncio
async def test_click_uses_node_box_without_javascript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = make_tool(tmp_path)
    session = FakeSession({
        "DOM.getBoxModel": {"model": {"content": [0, 0, 10, 0, 10, 10, 0, 10]}}
    })

    @asynccontextmanager
    async def fake_session(
        _self: ChromeCDP, _target_id: str | None
    ) -> AsyncGenerator[tuple[_Target, Any], None]:
        yield _Target(id="tab", type="page"), session

    monkeypatch.setattr(ChromeCDP, "_session", fake_session)
    await run_tool(tool, ChromeCDPArgs(action=ChromeCDPAction.CLICK, node_id=42))
    methods = [method for method, _ in session.calls]
    assert methods == [
        "DOM.scrollIntoViewIfNeeded",
        "DOM.getBoxModel",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert "Runtime.evaluate" not in methods


@pytest.mark.asyncio
async def test_evaluate_bounds_expression_before_connection(tmp_path: Path) -> None:
    tool = make_tool(tmp_path, max_expression_chars=3)
    with pytest.raises(ToolError, match="expression exceeds"):
        await run_tool(
            tool, ChromeCDPArgs(action=ChromeCDPAction.EVALUATE, expression="1234")
        )


@pytest.mark.asyncio
async def test_evaluate_bounds_result_and_warns_about_session_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = make_tool(tmp_path, max_result_chars=100)
    session = FakeSession({"Runtime.evaluate": {"result": {"value": "x" * 200}}})

    @asynccontextmanager
    async def fake_session(
        _self: ChromeCDP, _target_id: str | None
    ) -> AsyncGenerator[tuple[_Target, Any], None]:
        yield _Target(id="tab", type="page"), session

    monkeypatch.setattr(ChromeCDP, "_session", fake_session)
    result = await run_tool(
        tool,
        ChromeCDPArgs(action=ChromeCDPAction.EVALUATE, expression="document.cookie"),
    )
    assert result.truncated
    assert result.value_json is not None and len(result.value_json) == 101
    assert "session data" in result.message


@pytest.mark.asyncio
async def test_screenshot_is_saved_and_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = make_tool(tmp_path)
    png = b"\x89PNG\r\n\x1a\n"
    session = FakeSession({
        "Page.captureScreenshot": {"data": base64.b64encode(png).decode()}
    })

    @asynccontextmanager
    async def fake_session(
        _self: ChromeCDP, _target_id: str | None
    ) -> AsyncGenerator[tuple[_Target, Any], None]:
        yield _Target(id="tab", type="page"), session

    monkeypatch.setattr(ChromeCDP, "_session", fake_session)
    result = await run_tool(tool, ChromeCDPArgs(action=ChromeCDPAction.SCREENSHOT))
    assert result.screenshot_path is not None
    assert Path(result.screenshot_path).read_bytes() == png
    images = tool.get_result_images(result)
    assert len(images) == 1
    assert isinstance(images[0].source, FileImageSource)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.responses = iter([
            '{"method":"Page.event"}',
            '{"id":1,"result":{"value":"ok"}}',
        ])

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        return next(self.responses)


@pytest.mark.asyncio
async def test_cdp_session_ignores_events_and_matches_response_id() -> None:
    websocket = FakeWebSocket()
    session = _CDPSession(websocket, 1)  # type: ignore[arg-type]
    result = await session.request("Runtime.evaluate", {"expression": "1"})
    assert result == {"value": "ok"}
    assert '"method": "Runtime.evaluate"' in websocket.sent[0]
