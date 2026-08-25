from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.config import ModelConfig, ProviderConfig
from vibe.core.llm.exceptions import BackendError, PayloadSummary
from vibe.core.types import Backend, LLMChunk, LLMMessage, RateLimitError, Role


def _backend_error(
    *, status: int = 402, detail: str = "subscription exhausted"
) -> BackendError:
    return BackendError(
        provider="mistral",
        endpoint="https://api.mistral.ai/v1/chat/completions",
        status=status,
        reason="Payment Required" if status == 402 else "Too Many Requests",
        headers={},
        body_text=f'{{"detail":"{detail}"}}',
        parsed_error=detail,
        model="enhanced-model",
        payload_summary=PayloadSummary(
            model="enhanced-model",
            message_count=1,
            approx_chars=4,
            temperature=0.2,
            has_tools=False,
            tool_choice=None,
        ),
    )


def _config():
    primary = ModelConfig(name="enhanced-model", provider="mistral", alias="primary")
    fallback = ModelConfig(
        name="user-model",
        provider="user",
        alias="user",
        input_price=9.0,
        output_price=18.0,
    )
    config = build_test_vibe_config(
        active_model="primary",
        fallback_model="user",
        models=[primary, fallback],
        providers=[
            ProviderConfig(
                name="mistral",
                api_base="https://api.mistral.ai/v1",
                backend=Backend.MISTRAL,
            ),
            ProviderConfig(name="user", api_base="https://user.example/v1"),
        ],
    )
    return config, primary


class _RoutingBackend(FakeBackend):
    def __init__(self, *, partial_primary: bool = False) -> None:
        super().__init__()
        self.requested_aliases: list[str] = []
        self.partial_primary = partial_primary
        self.closed = False

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.closed = True

    async def complete(self, *, model: ModelConfig, **kwargs: Any) -> LLMChunk:
        self.requested_aliases.append(model.alias)
        if model.alias == "primary":
            raise _backend_error()
        return mock_llm_chunk(content="fallback response")

    async def complete_streaming(
        self, *, model: ModelConfig, **kwargs: Any
    ) -> AsyncGenerator[LLMChunk, None]:
        self.requested_aliases.append(model.alias)
        if model.alias == "primary":
            if self.partial_primary:
                yield mock_llm_chunk(content="partial")
            raise _backend_error()
        yield mock_llm_chunk(content="fallback response")


@pytest.mark.asyncio
async def test_non_streaming_payment_error_uses_fallback_once() -> None:
    config, primary = _config()
    backend = _RoutingBackend()
    agent = build_test_agent_loop(config=config, backend=backend)

    result = await agent._complete(
        model=primary,
        messages=[LLMMessage(role=Role.user, content="work")],
        tools=None,
        tool_choice=None,
        call_type=None,
    )

    assert result.message.content == "fallback response"
    assert backend.requested_aliases == ["primary", "user"]

    second = await agent._complete(
        model=primary,
        messages=[LLMMessage(role=Role.user, content="continue")],
        tools=None,
        tool_choice=None,
        call_type=None,
    )

    assert second.message.content == "fallback response"
    assert backend.requested_aliases == ["primary", "user", "user"]
    assert agent.stats.input_price_per_million == 9.0
    assert agent.stats.output_price_per_million == 18.0


@pytest.mark.asyncio
async def test_streaming_payment_error_before_first_chunk_uses_fallback() -> None:
    config, _ = _config()
    backend = _RoutingBackend()
    agent = build_test_agent_loop(config=config, backend=backend, enable_streaming=True)
    agent.messages.append(LLMMessage(role=Role.user, content="work"))

    chunks = [chunk async for chunk in agent._chat_streaming()]

    assert [chunk.message.content for chunk in chunks] == ["fallback response"]
    assert backend.requested_aliases == ["primary", "user"]


@pytest.mark.asyncio
async def test_streaming_payment_error_after_first_chunk_does_not_fail_over() -> None:
    config, _ = _config()
    backend = _RoutingBackend(partial_primary=True)
    agent = build_test_agent_loop(config=config, backend=backend, enable_streaming=True)
    agent.messages.append(LLMMessage(role=Role.user, content="work"))

    with pytest.raises(RuntimeError, match="API error from mistral"):
        _ = [chunk async for chunk in agent._chat_streaming()]

    assert backend.requested_aliases == ["primary"]


@pytest.mark.asyncio
async def test_ordinary_rate_limit_does_not_use_fallback() -> None:
    config, primary = _config()
    backend = _RoutingBackend()

    async def rate_limited(*, model: ModelConfig, **kwargs: Any) -> LLMChunk:
        backend.requested_aliases.append(model.alias)
        raise _backend_error(status=429, detail="rate limit exceeded")

    backend.complete = rate_limited  # type: ignore[method-assign]
    agent = build_test_agent_loop(config=config, backend=backend)

    with pytest.raises(RateLimitError, match="Rate limits exceeded"):
        await agent._complete(
            model=primary,
            messages=[LLMMessage(role=Role.user, content="work")],
            tools=None,
            tool_choice=None,
            call_type=None,
        )

    assert backend.requested_aliases == ["primary"]


@pytest.mark.asyncio
async def test_fallback_uses_and_closes_provider_specific_backend() -> None:
    config, primary = _config()
    primary_backend = _RoutingBackend()
    fallback_backend = _RoutingBackend()
    agent = build_test_agent_loop(config=config, backend=primary_backend)
    agent._injected_backend = None

    with patch(
        "vibe.core.agent_loop._loop.create_backend", return_value=fallback_backend
    ) as factory:
        result = await agent._complete(
            model=primary,
            messages=[LLMMessage(role=Role.user, content="work")],
            tools=None,
            tool_choice=None,
            call_type=None,
        )

    assert result.message.content == "fallback response"
    assert primary_backend.requested_aliases == ["primary"]
    assert fallback_backend.requested_aliases == ["user"]
    assert fallback_backend.closed
    assert factory.call_args.kwargs["provider"].name == "user"


@pytest.mark.asyncio
async def test_clear_history_resets_sticky_fallback() -> None:
    config, primary = _config()
    backend = _RoutingBackend()
    agent = build_test_agent_loop(config=config, backend=backend)

    await agent._complete(
        model=primary,
        messages=[LLMMessage(role=Role.user, content="work")],
        tools=None,
        tool_choice=None,
        call_type=None,
    )
    assert agent._model_failover_state.active(agent._failover_key(primary))

    await agent.clear_history()

    assert not agent._model_failover_state.active(agent._failover_key(primary))
