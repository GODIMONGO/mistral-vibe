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


def _context(
    steps: int, context_tokens: int = 0, auto_compact_threshold: int = 200_000
) -> ConversationContext:
    stats = AgentStats()
    stats.steps = steps
    stats.context_tokens = context_tokens
    return ConversationContext(
        messages=MessageList(),
        stats=stats,
        config=build_test_vibe_config(auto_compact_threshold=auto_compact_threshold),
    )


@pytest.mark.asyncio
async def test_refresh_compacts_only_near_context_capacity() -> None:
    middleware = AutonomyRefreshMiddleware(turn_interval=4)

    assert (
        await middleware.before_turn(_context(5, context_tokens=100_000))
    ).action is MiddlewareAction.CONTINUE
    assert (
        await middleware.before_turn(_context(5, context_tokens=180_000))
    ).action is MiddlewareAction.COMPACT
    middleware.reset(ResetReason.COMPACT)
    assert (
        await middleware.before_turn(_context(6, context_tokens=190_000))
    ).action is MiddlewareAction.CONTINUE
    assert (
        await middleware.before_turn(_context(9, context_tokens=190_000))
    ).action is MiddlewareAction.COMPACT


@pytest.mark.asyncio
async def test_stop_reset_restarts_refresh_budget() -> None:
    middleware = AutonomyRefreshMiddleware(turn_interval=4)
    assert (
        await middleware.before_turn(_context(5, context_tokens=180_000))
    ).action is MiddlewareAction.COMPACT

    middleware.reset(ResetReason.STOP)

    assert (
        await middleware.before_turn(_context(4))
    ).action is MiddlewareAction.CONTINUE


@pytest.mark.asyncio
async def test_refresh_stays_disabled_when_compaction_threshold_is_disabled() -> None:
    context = _context(100, context_tokens=1_000_000, auto_compact_threshold=0)

    result = await AutonomyRefreshMiddleware(turn_interval=1).before_turn(context)

    assert result.action is MiddlewareAction.CONTINUE
