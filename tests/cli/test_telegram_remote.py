from __future__ import annotations

import asyncio

import httpx
import pytest

from vibe.cli.telegram_remote import (
    TelegramDelivery,
    TelegramIncomingMessage,
    TelegramRemoteConfig,
    TelegramRemoteConfigurationError,
    TelegramRemoteController,
    TelegramRemoteError,
    parse_allowed_chat_ids,
)
from vibe.utils.http import VibeAsyncHTTPClient


def test_config_reads_token_and_strict_allowlist_from_environment() -> None:
    config = TelegramRemoteConfig.from_env({
        "TELEGRAM_BOT_TOKEN": "secret-token",
        "VIBE_TELEGRAM_ALLOWED_CHAT_IDS": "123,-456",
    })

    assert config.token == "secret-token"
    assert config.allowed_chat_ids == frozenset({123, -456})
    assert "secret-token" not in repr(config)


@pytest.mark.parametrize(
    "value", ["", "123, 456", "123,", "+123", "0", "01", "123,123", "chat"]
)
def test_allowlist_parser_rejects_ambiguous_values(value: str) -> None:
    with pytest.raises(TelegramRemoteConfigurationError):
        parse_allowed_chat_ids(value)


def test_config_requires_token() -> None:
    with pytest.raises(TelegramRemoteConfigurationError):
        TelegramRemoteConfig.from_env({"VIBE_TELEGRAM_ALLOWED_CHAT_IDS": "123"})


def _update(update_id: int, chat_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "chat": {"id": chat_id},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_authorized_text_is_delivered_and_response_callback_runs() -> None:
    requests: list[httpx.Request] = []
    poll_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        requests.append(request)
        if request.url.path.endswith("/getUpdates"):
            poll_count += 1
            updates = [_update(1, 123, "run status")] if poll_count == 1 else []
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": {}})

    incoming: list[TelegramIncomingMessage] = []
    delivered: list[TelegramDelivery] = []

    async def on_text(message: TelegramIncomingMessage) -> str:
        incoming.append(message)
        return "accepted"

    async def on_delivered(delivery: TelegramDelivery) -> None:
        delivered.append(delivery)

    async with VibeAsyncHTTPClient(transport=httpx.MockTransport(handler)) as client:
        controller = TelegramRemoteController(
            TelegramRemoteConfig(
                token="secret-token",
                allowed_chat_ids=frozenset({123}),
                poll_timeout_seconds=1,
                retry_delay_seconds=0.01,
            ),
            on_text,
            on_response_delivered=on_delivered,
            client=client,
        )
        assert controller.status().running is False
        await controller.start()
        for _ in range(100):
            if delivered:
                break
            await asyncio.sleep(0.001)
        status = await controller.stop()

    assert [item.text for item in incoming] == ["run status"]
    assert delivered == [TelegramDelivery(chat_id=123, chars=8, chunks=1)]
    assert status.running is False
    assert status.active_chats[0].chat_id == 123
    assert status.active_chats[0].last_text == "run status"
    assert any(request.url.path.endswith("/sendMessage") for request in requests)


@pytest.mark.asyncio
async def test_unauthorized_chat_is_ignored_and_offset_advances() -> None:
    request_bodies: list[str] = []
    poll_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        request_bodies.append(request.content.decode())
        poll_count += 1
        updates = [_update(7, 999, "ignore me")] if poll_count == 1 else []
        return httpx.Response(200, json={"ok": True, "result": updates})

    incoming: list[TelegramIncomingMessage] = []

    async def on_text(message: TelegramIncomingMessage) -> None:
        incoming.append(message)

    async with VibeAsyncHTTPClient(transport=httpx.MockTransport(handler)) as client:
        controller = TelegramRemoteController(
            TelegramRemoteConfig(
                token="secret-token",
                allowed_chat_ids=frozenset({123}),
                poll_timeout_seconds=1,
                retry_delay_seconds=0.01,
            ),
            on_text,
            client=client,
        )
        await controller.start()
        for _ in range(100):
            if len(request_bodies) >= 2:
                break
            await asyncio.sleep(0.001)
        await controller.stop()

    assert incoming == []
    assert '"offset":8' in request_bodies[1]


@pytest.mark.asyncio
async def test_response_is_bounded_and_allowlisted() -> None:
    sent_chunks: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            payload = request.content.decode()
            sent_chunks.append(payload)
        return httpx.Response(200, json={"ok": True, "result": []})

    async def on_text(_message: TelegramIncomingMessage) -> None:
        return None

    async with VibeAsyncHTTPClient(transport=httpx.MockTransport(handler)) as client:
        controller = TelegramRemoteController(
            TelegramRemoteConfig(
                token="secret-token",
                allowed_chat_ids=frozenset({123}),
                poll_timeout_seconds=1,
                max_response_chars=5_000,
            ),
            on_text,
            client=client,
        )
        await controller.start()
        delivery = await controller.send_response(123, "x" * 5_000)
        with pytest.raises(TelegramRemoteError, match="outside the allowlist"):
            await controller.send_response(999, "no")
        with pytest.raises(TelegramRemoteError, match="exceeds"):
            await controller.send_response(123, "x" * 5_001)
        await controller.stop()

    assert delivery.chunks == 2
    assert len(sent_chunks) == 2


@pytest.mark.asyncio
async def test_stop_cancels_long_poll_and_does_not_close_injected_client() -> None:
    entered = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def on_text(_message: TelegramIncomingMessage) -> None:
        return None

    async with VibeAsyncHTTPClient(transport=httpx.MockTransport(handler)) as client:
        controller = TelegramRemoteController(
            TelegramRemoteConfig(
                token="secret-token",
                allowed_chat_ids=frozenset({123}),
                poll_timeout_seconds=1,
            ),
            on_text,
            client=client,
        )
        await controller.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        status = await asyncio.wait_for(controller.stop(), timeout=1)
        assert client.is_closed is False

    assert status.running is False


@pytest.mark.asyncio
async def test_polling_errors_are_sanitized_without_token() -> None:
    attempted = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        attempted.set()
        return httpx.Response(401, text="token rejected")

    async def on_text(_message: TelegramIncomingMessage) -> None:
        return None

    token = "never-log-this-token"
    async with VibeAsyncHTTPClient(transport=httpx.MockTransport(handler)) as client:
        controller = TelegramRemoteController(
            TelegramRemoteConfig(
                token=token,
                allowed_chat_ids=frozenset({123}),
                poll_timeout_seconds=1,
                retry_delay_seconds=0.01,
            ),
            on_text,
            client=client,
        )
        await controller.start()
        await asyncio.wait_for(attempted.wait(), timeout=1)
        for _ in range(100):
            if controller.status().last_error:
                break
            await asyncio.sleep(0.001)
        status = await controller.stop()

    assert status.last_error == "Telegram polling failed"
    assert token not in (status.last_error or "")


@pytest.mark.asyncio
async def test_callback_failure_is_sanitized_and_polling_continues() -> None:
    poll_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path.endswith("/getUpdates"):
            poll_count += 1
            updates = [_update(1, 123, "fail")] if poll_count == 1 else []
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": {}})

    async def on_text(_message: TelegramIncomingMessage) -> None:
        raise RuntimeError("secret callback detail")

    async with VibeAsyncHTTPClient(transport=httpx.MockTransport(handler)) as client:
        controller = TelegramRemoteController(
            TelegramRemoteConfig(
                token="secret-token",
                allowed_chat_ids=frozenset({123}),
                poll_timeout_seconds=1,
                retry_delay_seconds=0.01,
            ),
            on_text,
            client=client,
        )
        await controller.start()
        for _ in range(100):
            if poll_count >= 2:
                break
            await asyncio.sleep(0.001)
        status = controller.status()
        await controller.stop()

    assert poll_count >= 2
    assert status.running is True
