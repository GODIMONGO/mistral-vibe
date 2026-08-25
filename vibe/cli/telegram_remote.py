from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import os
import re
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vibe.observability.logging import logger
from vibe.utils.http import VibeAsyncHTTPClient, build_ssl_context

TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_ALLOWED_CHAT_IDS_ENV = "VIBE_TELEGRAM_ALLOWED_CHAT_IDS"
TELEGRAM_MESSAGE_MAX_CHARS = 4_096
TELEGRAM_RESPONSE_MAX_CHARS = 16_384
TELEGRAM_ACTIVE_CHAT_LIMIT = 64
TELEGRAM_SUMMARY_MAX_CHARS = 160
TELEGRAM_POLL_TIMEOUT_MAX_SECONDS = 50
TELEGRAM_RESPONSE_HARD_MAX_CHARS = 65_536
TELEGRAM_ACTIVE_CHAT_HARD_LIMIT = 1_024
_CHAT_ID_PATTERN = re.compile(r"-?[1-9][0-9]*")


class TelegramRemoteError(RuntimeError):
    pass


class TelegramRemoteConfigurationError(TelegramRemoteError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramRemoteConfig:
    token: str = field(repr=False)
    allowed_chat_ids: frozenset[int]
    poll_timeout_seconds: int = 30
    retry_delay_seconds: float = 1.0
    max_incoming_chars: int = TELEGRAM_MESSAGE_MAX_CHARS
    max_response_chars: int = TELEGRAM_RESPONSE_MAX_CHARS
    max_active_chats: int = TELEGRAM_ACTIVE_CHAT_LIMIT

    def __post_init__(self) -> None:
        if not self.token or self.token != self.token.strip():
            raise TelegramRemoteConfigurationError(
                f"{TELEGRAM_BOT_TOKEN_ENV} must be a non-empty value"
            )
        if not self.allowed_chat_ids:
            raise TelegramRemoteConfigurationError(
                f"{TELEGRAM_ALLOWED_CHAT_IDS_ENV} must contain at least one chat ID"
            )
        if (
            self.poll_timeout_seconds < 1
            or self.poll_timeout_seconds > TELEGRAM_POLL_TIMEOUT_MAX_SECONDS
        ):
            raise TelegramRemoteConfigurationError(
                "poll_timeout_seconds must be between 1 and 50"
            )
        if self.retry_delay_seconds <= 0:
            raise TelegramRemoteConfigurationError(
                "retry_delay_seconds must be positive"
            )
        if (
            self.max_incoming_chars < 1
            or self.max_incoming_chars > TELEGRAM_MESSAGE_MAX_CHARS
        ):
            raise TelegramRemoteConfigurationError(
                "max_incoming_chars must be between 1 and 4096"
            )
        if (
            self.max_response_chars < 1
            or self.max_response_chars > TELEGRAM_RESPONSE_HARD_MAX_CHARS
        ):
            raise TelegramRemoteConfigurationError(
                "max_response_chars must be between 1 and 65536"
            )
        if (
            self.max_active_chats < 1
            or self.max_active_chats > TELEGRAM_ACTIVE_CHAT_HARD_LIMIT
        ):
            raise TelegramRemoteConfigurationError(
                "max_active_chats must be between 1 and 1024"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TelegramRemoteConfig:
        source = os.environ if environ is None else environ
        token = source.get(TELEGRAM_BOT_TOKEN_ENV, "")
        raw_chat_ids = source.get(TELEGRAM_ALLOWED_CHAT_IDS_ENV, "")
        return cls(token=token, allowed_chat_ids=parse_allowed_chat_ids(raw_chat_ids))


def parse_allowed_chat_ids(value: str) -> frozenset[int]:
    if not value:
        raise TelegramRemoteConfigurationError(
            f"{TELEGRAM_ALLOWED_CHAT_IDS_ENV} is required"
        )
    parts = value.split(",")
    if any(not _CHAT_ID_PATTERN.fullmatch(part) for part in parts):
        raise TelegramRemoteConfigurationError(
            f"{TELEGRAM_ALLOWED_CHAT_IDS_ENV} must be a comma-separated list of "
            "non-zero decimal integers without whitespace"
        )
    chat_ids = [int(part) for part in parts]
    if len(chat_ids) != len(set(chat_ids)):
        raise TelegramRemoteConfigurationError(
            f"{TELEGRAM_ALLOWED_CHAT_IDS_ENV} must not contain duplicates"
        )
    return frozenset(chat_ids)


class _TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: int


class _TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    message_id: int
    chat: _TelegramChat
    text: str | None = None


class _TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    update_id: int
    message: _TelegramMessage | None = None


class _TelegramUpdatesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    ok: bool
    result: list[_TelegramUpdate] = Field(default_factory=list)


class _TelegramSendResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    ok: bool


@dataclass(frozen=True, slots=True)
class TelegramIncomingMessage:
    chat_id: int
    message_id: int
    update_id: int
    text: str


@dataclass(frozen=True, slots=True)
class TelegramDelivery:
    chat_id: int
    chars: int
    chunks: int


@dataclass(frozen=True, slots=True)
class TelegramActiveChatSummary:
    chat_id: int
    message_count: int
    last_message_at: float
    last_text: str


@dataclass(frozen=True, slots=True)
class TelegramRemoteStatus:
    running: bool
    allowed_chat_count: int
    active_chats: tuple[TelegramActiveChatSummary, ...]
    last_error: str | None


IncomingTextCallback = Callable[[TelegramIncomingMessage], Awaitable[str | None]]
ResponseDeliveredCallback = Callable[[TelegramDelivery], Awaitable[None]]


class TelegramRemoteController:
    def __init__(
        self,
        config: TelegramRemoteConfig,
        on_text: IncomingTextCallback,
        *,
        on_response_delivered: ResponseDeliveredCallback | None = None,
        client: VibeAsyncHTTPClient | None = None,
    ) -> None:
        self._config = config
        self._on_text = on_text
        self._on_response_delivered = on_response_delivered
        self._injected_client = client
        self._client: VibeAsyncHTTPClient | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._offset = 0
        self._active_chats: dict[int, TelegramActiveChatSummary] = {}
        self._last_error: str | None = None

    async def start(self) -> TelegramRemoteStatus:
        async with self._lifecycle_lock:
            if self._poll_task is not None and not self._poll_task.done():
                return self.status()
            self._last_error = None
            self._client = self._injected_client or VibeAsyncHTTPClient(
                verify=build_ssl_context(),
                timeout=httpx.Timeout(self._config.poll_timeout_seconds + 5),
            )
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name="vibe-telegram-remote"
            )
            return self.status()

    async def stop(self) -> TelegramRemoteStatus:
        async with self._lifecycle_lock:
            task = self._poll_task
            self._poll_task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            client = self._client
            self._client = None
            if client is not None and client is not self._injected_client:
                await client.aclose()
            return self.status()

    def status(self) -> TelegramRemoteStatus:
        task = self._poll_task
        active_chats = tuple(
            sorted(
                self._active_chats.values(),
                key=lambda summary: summary.last_message_at,
                reverse=True,
            )
        )
        return TelegramRemoteStatus(
            running=task is not None and not task.done(),
            allowed_chat_count=len(self._config.allowed_chat_ids),
            active_chats=active_chats,
            last_error=self._last_error,
        )

    async def send_response(self, chat_id: int, text: str) -> TelegramDelivery:
        if chat_id not in self._config.allowed_chat_ids:
            raise TelegramRemoteError(
                "Refusing delivery to a chat outside the allowlist"
            )
        if not text:
            raise TelegramRemoteError("Telegram response must not be empty")
        if len(text) > self._config.max_response_chars:
            raise TelegramRemoteError(
                f"Telegram response exceeds {self._config.max_response_chars} characters"
            )
        client = self._require_client()
        chunks = tuple(
            text[index : index + TELEGRAM_MESSAGE_MAX_CHARS]
            for index in range(0, len(text), TELEGRAM_MESSAGE_MAX_CHARS)
        )
        for chunk in chunks:
            response = await client.post(
                self._method_url("sendMessage"),
                json={"chat_id": chat_id, "text": chunk},
            )
            self._validate_send_response(response)
        delivery = TelegramDelivery(
            chat_id=chat_id, chars=len(text), chunks=len(chunks)
        )
        if self._on_response_delivered is not None:
            await self._on_response_delivered(delivery)
        return delivery

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
                self._last_error = None
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, TelegramRemoteError, ValidationError):
                self._last_error = "Telegram polling failed"
                logger.warning("Telegram remote polling failed; retrying")
                await asyncio.sleep(self._config.retry_delay_seconds)
            except Exception:
                self._last_error = "Telegram remote callback failed"
                logger.warning("Telegram remote callback failed; retrying")
                await asyncio.sleep(self._config.retry_delay_seconds)

    async def _poll_once(self) -> None:
        client = self._require_client()
        response = await client.post(
            self._method_url("getUpdates"),
            json={
                "offset": self._offset,
                "timeout": self._config.poll_timeout_seconds,
                "allowed_updates": ["message"],
            },
        )
        response.raise_for_status()
        try:
            payload = _TelegramUpdatesResponse.model_validate(response.json())
        except ValueError as exc:
            raise TelegramRemoteError("Telegram returned an invalid response") from exc
        if not payload.ok:
            raise TelegramRemoteError("Telegram rejected the polling request")
        for update in payload.result:
            self._offset = max(self._offset, update.update_id + 1)
            await self._handle_update(update)

    async def _handle_update(self, update: _TelegramUpdate) -> None:
        message = update.message
        if message is None or message.text is None:
            return
        chat_id = message.chat.id
        if chat_id not in self._config.allowed_chat_ids:
            return
        if len(message.text) > self._config.max_incoming_chars:
            self._last_error = "Authorized Telegram message exceeded the size limit"
            return
        incoming = TelegramIncomingMessage(
            chat_id=chat_id,
            message_id=message.message_id,
            update_id=update.update_id,
            text=message.text,
        )
        self._record_active_chat(incoming)
        response = await self._on_text(incoming)
        if response is not None:
            await self.send_response(chat_id, response)

    def _record_active_chat(self, incoming: TelegramIncomingMessage) -> None:
        previous = self._active_chats.get(incoming.chat_id)
        summary = TelegramActiveChatSummary(
            chat_id=incoming.chat_id,
            message_count=1 if previous is None else previous.message_count + 1,
            last_message_at=time.time(),
            last_text=incoming.text[:TELEGRAM_SUMMARY_MAX_CHARS],
        )
        self._active_chats[incoming.chat_id] = summary
        if len(self._active_chats) <= self._config.max_active_chats:
            return
        oldest = min(self._active_chats.values(), key=lambda item: item.last_message_at)
        self._active_chats.pop(oldest.chat_id, None)

    def _require_client(self) -> VibeAsyncHTTPClient:
        if self._client is None:
            raise TelegramRemoteError("Telegram remote is not running")
        return self._client

    def _method_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._config.token}/{method}"

    @staticmethod
    def _validate_send_response(response: httpx.Response) -> None:
        response.raise_for_status()
        try:
            payload = _TelegramSendResponse.model_validate(response.json())
        except ValueError as exc:
            raise TelegramRemoteError("Telegram returned an invalid response") from exc
        if not payload.ok:
            raise TelegramRemoteError("Telegram rejected response delivery")


__all__ = [
    "IncomingTextCallback",
    "ResponseDeliveredCallback",
    "TelegramActiveChatSummary",
    "TelegramDelivery",
    "TelegramIncomingMessage",
    "TelegramRemoteConfig",
    "TelegramRemoteConfigurationError",
    "TelegramRemoteController",
    "TelegramRemoteError",
    "TelegramRemoteStatus",
    "parse_allowed_chat_ids",
]
