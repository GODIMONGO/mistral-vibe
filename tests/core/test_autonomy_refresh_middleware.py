from __future__ import annotations

import pytest

from tests.conftest import build_test_vibe_config
from vibe.core.middleware import (
    AutonomyRefreshMiddleware,
    ConversationContext,
    MiddlewareAction,
    ResetReason,
)
from vibe.core.types import AgentStats, MessageList


def _context(steps: int) -> ConversationContext:
    stats = AgentStats()
    stats.steps = steps
    return ConversationContext(
        messages=MessageList(), stats=stats, config=build_test_vibe_config()
    )


@pytest.mark.asyncio
async def test_refresh_compacts_once_per_interval() -> None:
    middleware = AutonomyRefreshMiddleware(turn_interval=4)

    assert (
        await middleware.before_turn(_context(4))
    ).action is MiddlewareAction.CONTINUE
    assert (
        await middleware.before_turn(_context(5))
    ).action is MiddlewareAction.COMPACT
    middleware.reset(ResetReason.COMPACT)
    assert (
        await middleware.before_turn(_context(6))
    ).action is MiddlewareAction.CONTINUE
    assert (
        await middleware.before_turn(_context(9))
    ).action is MiddlewareAction.COMPACT


@pytest.mark.asyncio
async def test_stop_reset_restarts_refresh_budget() -> None:
    middleware = AutonomyRefreshMiddleware(turn_interval=4)
    assert (
        await middleware.before_turn(_context(5))
    ).action is MiddlewareAction.COMPACT

    middleware.reset(ResetReason.STOP)

    assert (
        await middleware.before_turn(_context(4))
    ).action is MiddlewareAction.CONTINUE
