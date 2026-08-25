from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import Input, OptionList, Static

from tests.conftest import build_test_vibe_app, build_test_vibe_config
from vibe.cli.textual_ui.app import BottomApp
from vibe.cli.textual_ui.widgets.api_key_app import ApiKeyApp
from vibe.cli.textual_ui.widgets.messages import ErrorMessage
from vibe.core.config import ModelConfig


def _config():
    return build_test_vibe_config(
        models=[
            ModelConfig(
                name="model-a",
                provider="mistral",
                alias="alpha",
                display_name="Alpha Smart",
            ),
            ModelConfig(name="model-b", provider="mistral", alias="beta"),
        ],
        active_model="alpha",
    )


@pytest.mark.asyncio
async def test_api_key_without_alias_opens_model_picker() -> None:
    app = build_test_vibe_app(config=_config())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)

        handled = await app._handle_command("/api-key")
        await pilot.pause(0.2)

        assert handled
        assert app._current_bottom_app is BottomApp.ApiKey
        assert [option.id for option in app.query_one(OptionList).options] == [
            "alpha",
            "beta",
        ]
        assert len(app.query(Input)) == 0


@pytest.mark.asyncio
async def test_api_key_alias_opens_masked_input_directly() -> None:
    app = build_test_vibe_app(config=_config())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)

        handled = await app._handle_command("/api-key alpha")
        await pilot.pause(0.2)

        input_widget = app.query_one("#apikey-input", Input)
        assert handled
        assert input_widget.password is True


@pytest.mark.asyncio
async def test_api_key_rejects_unknown_alias() -> None:
    app = build_test_vibe_app(config=_config())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)

        handled = await app._handle_command("/api-key missing")
        await pilot.pause(0.2)

        assert handled
        assert app._current_bottom_app is BottomApp.Input
        assert len(app.query(ErrorMessage)) == 1


@pytest.mark.asyncio
async def test_api_key_picker_selects_model_then_saves_without_echoing_key() -> None:
    app = build_test_vibe_app(config=_config())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_api_key()
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)

        input_widget = app.query_one("#apikey-input", Input)
        input_widget.value = "super-secret-key"
        with patch.object(
            app.app_server.resources.config,
            "write_api_key",
            new=AsyncMock(return_value=0),
        ) as write_api_key:
            await pilot.press("enter")
            await pilot.pause(0.3)

        write_api_key.assert_awaited_once_with("alpha", "super-secret-key")
        assert input_widget.value == ""
        assert app._current_bottom_app is BottomApp.Input
        assert all(
            "super-secret-key" not in str(getattr(widget, "renderable", ""))
            for widget in app.query(Static)
        )


@pytest.mark.asyncio
async def test_api_key_escape_clears_masked_input() -> None:
    app = build_test_vibe_app(config=_config())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_api_key(cmd_args="alpha")
        await pilot.pause(0.1)
        input_widget = app.query_one("#apikey-input", Input)
        input_widget.value = "discard-me"

        await pilot.press("escape")
        await pilot.pause(0.2)

        assert input_widget.value == ""
        assert app._current_bottom_app is BottomApp.Input
        assert len(app.query(ApiKeyApp)) == 0


@pytest.mark.asyncio
async def test_api_key_save_waits_for_idle_without_putting_secret_in_queue_text() -> (
    None
):
    app = build_test_vibe_app(config=_config())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        blocker = asyncio.create_task(asyncio.Event().wait())
        app._agent_task = blocker
        write_api_key = AsyncMock(return_value=0)

        try:
            with patch.object(
                app.app_server.resources.config, "write_api_key", new=write_api_key
            ):
                await app.on_api_key_app_submitted(
                    ApiKeyApp.Submitted("alpha", "super-secret-key")
                )

            assert len(app._input_queue) == 1
            queued = app._input_queue.items[0]
            assert queued.content == "api key alpha"
            assert "super-secret-key" not in queued.content
            write_api_key.assert_not_awaited()
        finally:
            blocker.cancel()
            with suppress(asyncio.CancelledError):
                await blocker
            app._agent_task = None
