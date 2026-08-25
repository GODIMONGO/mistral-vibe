from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import build_test_vibe_app
from vibe.cli.textual_ui.widgets.chat_input import ChatInputContainer
from vibe.cli.textual_ui.widgets.messages import ErrorMessage


@pytest.mark.asyncio
async def test_goal_switches_to_autonomous_agent_and_submits_objective() -> None:
    app = build_test_vibe_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._handle_user_message = AsyncMock()

        handled = await app._handle_command("/goal finish the migration")

        assert handled
        assert app.app_server.resources.agents.active.name == "autonomous"
        app._handle_user_message.assert_awaited_once_with("finish the migration")


@pytest.mark.asyncio
async def test_goal_requires_an_objective() -> None:
    app = build_test_vibe_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._handle_user_message = AsyncMock()

        with patch.object(app, "_switch_to_agent", AsyncMock()) as switch:
            handled = await app._handle_command("/goal")

        assert handled
        switch.assert_not_awaited()
        app._handle_user_message.assert_not_awaited()
        assert len(app.query(ErrorMessage)) == 1


@pytest.mark.asyncio
async def test_goal_waits_in_main_queue_while_agent_is_busy() -> None:
    app = build_test_vibe_app()
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        blocker = asyncio.create_task(asyncio.Event().wait())
        app._agent_task = blocker

        try:
            await app.on_chat_input_container_submitted(
                ChatInputContainer.Submitted("/goal finish the migration")
            )

            assert len(app._input_queue) == 1
            queued = app._input_queue.items[0]
            assert queued.kind.value == "command"
            assert queued.content == "/goal finish the migration"
        finally:
            blocker.cancel()
            with suppress(asyncio.CancelledError):
                await blocker
            app._agent_task = None
