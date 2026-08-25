from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import (
    build_test_agent_loop,
    build_test_vibe_app,
    build_test_vibe_config,
)
from vibe.app_server.protocol import AppServerResponseError, ProtocolErrorCode
from vibe.core.config import ModelConfig


def _agent_loop():
    config = build_test_vibe_config(
        models=[ModelConfig(name="smart", provider="mistral", alias="advisor-smart")],
        active_model="advisor-smart",
    )
    return build_test_agent_loop(config=config)


@pytest.mark.asyncio
async def test_api_key_write_persists_server_side_and_reloads_runtime() -> None:
    agent_loop = _agent_loop()
    app = build_test_vibe_app(agent_loop=agent_loop)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with (
            patch(
                "vibe.app_server._resources.persist_api_key", return_value="completed"
            ) as persist_api_key,
            patch.object(
                agent_loop, "reload_with_initial_messages", new=AsyncMock()
            ) as reload_runtime,
        ):
            await app.app_server.resources.config.write_api_key(
                "advisor-smart", "secret-value"
            )

        provider, api_key = persist_api_key.call_args.args
        assert provider.name == "mistral"
        assert api_key == "secret-value"
        reload_runtime.assert_awaited_once_with(reload_hooks=True)


@pytest.mark.asyncio
async def test_api_key_write_rejects_unknown_model_before_persistence() -> None:
    agent_loop = _agent_loop()
    app = build_test_vibe_app(agent_loop=agent_loop)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with patch("vibe.app_server._resources.persist_api_key") as persist_api_key:
            with pytest.raises(AppServerResponseError) as exc_info:
                await app.app_server.resources.config.write_api_key(
                    "missing", "secret-value"
                )

        assert exc_info.value.error.code is ProtocolErrorCode.INVALID_PARAMS
        persist_api_key.assert_not_called()


@pytest.mark.asyncio
async def test_api_key_write_does_not_reload_when_secure_save_fails() -> None:
    agent_loop = _agent_loop()
    app = build_test_vibe_app(agent_loop=agent_loop)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with (
            patch(
                "vibe.app_server._resources.persist_api_key",
                return_value="save_error:keyring unavailable",
            ),
            patch.object(
                agent_loop, "reload_with_initial_messages", new=AsyncMock()
            ) as reload_runtime,
        ):
            with pytest.raises(AppServerResponseError) as exc_info:
                await app.app_server.resources.config.write_api_key(
                    "advisor-smart", "secret-value"
                )

        assert exc_info.value.error.code is ProtocolErrorCode.INTERNAL_ERROR
        reload_runtime.assert_not_awaited()
