from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Literal, final
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, Field

from vibe.core.config import DEFAULT_MISTRAL_API_ENV_KEY, VibeConfigSchema
from vibe.core.config.models import Backend
from vibe.core.telemetry.build_metadata import build_request_metadata
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import ToolStreamEvent
from vibe.utils.api_keys import resolve_api_key
from vibe.utils.http import (
    VibeAsyncHTTPClient,
    build_ssl_context,
    get_server_url_from_api_base,
    get_user_agent,
)
from vibe.utils.tool_presentation import ToolEffectKind

if TYPE_CHECKING:
    from mistralai.client.models import ConversationResponse

    from vibe.core.types import ToolCallEvent, ToolResultEvent


_PUBLIC_SEARCH_URL = "https://html.duckduckgo.com/html/"
_MAX_PUBLIC_RESPONSE_BYTES = 512_000
_MAX_TITLE_CHARS = 240
_MAX_SNIPPET_CHARS = 600


@dataclass
class _PublicSearchHit:
    title: str
    url: str
    snippet: str = ""


class _PublicSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[_PublicSearchHit] = []
        self._kind = ""
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._kind:
            return
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if "result__a" in classes:
            self._kind = "result"
        elif "result__snippet" in classes:
            self._kind = "snippet"
        else:
            return
        self._href = values.get("href") or ""
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._kind:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._kind:
            return
        text = " ".join("".join(self._parts).split())
        if self._kind == "result":
            url = _unwrap_public_result_url(self._href)
            if text and url:
                self.hits.append(
                    _PublicSearchHit(title=text[:_MAX_TITLE_CHARS], url=url)
                )
        elif text and self.hits:
            self.hits[-1].snippet = text[:_MAX_SNIPPET_CHARS]
        self._kind = ""
        self._href = ""
        self._parts = []


def _unwrap_public_result_url(raw_url: str) -> str:
    url = f"https:{raw_url}" if raw_url.startswith("//") else raw_url
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [])
        if target:
            url = target[0]
            parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


class WebSearchSource(BaseModel):
    title: str
    url: str


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="The search query")


class WebSearchResult(BaseModel):
    query: str
    answer: str
    sources: list[WebSearchSource] = Field(default_factory=list)
    engine: Literal["mistral", "public"] = "mistral"


class WebSearchConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    timeout: int = Field(
        default=120, ge=1, le=120, description="HTTP timeout in seconds."
    )
    model: str = Field(
        default="mistral-vibe-cli-with-tools",
        description="Mistral model to use for web search.",
    )
    engine: Literal["auto", "mistral", "public"] = Field(
        default="auto",
        description="Search engine: Mistral, public no-key fallback, or automatic.",
    )
    max_results: int = Field(default=5, ge=1, le=10)


class WebSearch(
    BaseTool[WebSearchArgs, WebSearchResult, WebSearchConfig, BaseToolState],
    ToolUIData[WebSearchArgs, WebSearchResult],
):
    effect_kind = ToolEffectKind.WEB_SEARCH

    @classmethod
    def is_available(cls, config: VibeConfigSchema | None = None) -> bool:
        return True

    @final
    async def run(
        self, args: WebSearchArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | WebSearchResult, None]:
        if self.config.engine == "public":
            yield await self._run_public_search(args.query)
            return

        config = self._resolve_config(ctx)
        api_key_env_var = self._api_key_env_var(config)
        api_key = resolve_api_key(api_key_env_var)
        if self.config.engine == "mistral":
            if not api_key:
                raise ToolError(f"{api_key_env_var} credential not available.")
            yield await self._run_mistral_search(args.query, api_key, ctx)
            return

        mistral_error: ToolError | None = None
        if api_key:
            try:
                yield await self._run_mistral_search(args.query, api_key, ctx)
                return
            except ToolError as exc:
                mistral_error = exc

        try:
            yield await self._run_public_search(args.query)
        except ToolError as public_error:
            if mistral_error is None:
                raise
            raise ToolError(
                f"Mistral search failed ({mistral_error}); public fallback failed "
                f"({public_error})."
            ) from public_error

    async def _run_mistral_search(
        self, query: str, api_key: str, ctx: InvokeContext | None
    ) -> WebSearchResult:
        # Imported on first use: the mistralai SDK is heavy and would
        # otherwise load at CLI startup when the tool registry imports us.
        from mistralai.client import Mistral
        from mistralai.client.errors import SDKError

        ssl_context = build_ssl_context()
        async_http_client = VibeAsyncHTTPClient(
            follow_redirects=True, verify=ssl_context
        )

        try:
            client = Mistral(
                api_key=api_key,
                server_url=self._resolve_server_url(ctx),
                timeout_ms=self.config.timeout * 1000,
                async_client=async_http_client,
            )
            metadata = build_request_metadata(
                launch_context=ctx.launch_context if ctx else None,
                session_id=ctx.session_id if ctx else None,
                call_type="secondary_call",
            ).model_dump(exclude_none=True)
            async with async_http_client, client:
                response = await client.beta.conversations.start_async(
                    model=self.config.model,
                    instructions="Always use the web_search tool to answer queries. Never answer from memory alone.",
                    tools=[{"type": "web_search"}],
                    inputs=query,
                    store=False,
                    metadata=metadata,
                    http_headers={"user-agent": get_user_agent(Backend.MISTRAL)},
                )

                return self._parse_response(response, query)

        except SDKError as exc:
            raise ToolError(f"Mistral API error: {exc}") from exc
        finally:
            await async_http_client.aclose()

    async def _run_public_search(self, query: str) -> WebSearchResult:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            async with VibeAsyncHTTPClient(
                follow_redirects=True,
                timeout=httpx.Timeout(self.config.timeout),
                verify=build_ssl_context(),
            ) as client:
                async with client.stream(
                    "GET", _PUBLIC_SEARCH_URL, params={"q": query}, headers=headers
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        remaining = _MAX_PUBLIC_RESPONSE_BYTES - len(body)
                        if remaining <= 0:
                            break
                        body.extend(chunk[:remaining])
        except httpx.TimeoutException as exc:
            raise ToolError(
                f"Public web search timed out after {self.config.timeout} seconds."
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"Public web search failed: {exc}") from exc

        parser = _PublicSearchParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        hits = parser.hits[: self.config.max_results]
        if not hits:
            raise ToolError("Public web search returned no results.")

        sources = [WebSearchSource(title=hit.title, url=hit.url) for hit in hits]
        answer = "\n\n".join(
            f"{index}. {hit.title}\nURL: {hit.url}"
            + (f"\nSnippet: {hit.snippet}" if hit.snippet else "")
            for index, hit in enumerate(hits, start=1)
        )
        return WebSearchResult(
            query=query, answer=answer, sources=sources, engine="public"
        )

    def _resolve_server_url(self, ctx: InvokeContext | None) -> str | None:
        config = self._resolve_config(ctx)
        if config is None:
            return None
        provider = config.get_mistral_provider()
        if provider is None:
            return None
        return get_server_url_from_api_base(provider.api_base)

    def _resolve_config(self, ctx: InvokeContext | None) -> VibeConfigSchema | None:
        if not ctx or not ctx.agent_manager:
            return None
        return ctx.agent_manager.config

    @classmethod
    def _api_key_env_var(cls, config: VibeConfigSchema | None) -> str:
        if config is None:
            return DEFAULT_MISTRAL_API_ENV_KEY
        provider = config.get_mistral_provider()
        if provider is None:
            return DEFAULT_MISTRAL_API_ENV_KEY
        return provider.api_key_env_var or DEFAULT_MISTRAL_API_ENV_KEY

    def _parse_response(
        self, response: ConversationResponse, query: str
    ) -> WebSearchResult:
        from mistralai.client.models import (
            MessageOutputEntry,
            TextChunk,
            ToolReferenceChunk,
        )

        text_parts: list[str] = []
        sources: dict[str, WebSearchSource] = {}

        for entry in response.outputs:
            if not isinstance(entry, MessageOutputEntry):
                continue
            # content is a plain string for short answers, else a list of chunks.
            if isinstance(entry.content, str):
                text_parts.append(entry.content)
                continue
            for chunk in entry.content:
                if isinstance(chunk, TextChunk):
                    text_parts.append(chunk.text)
                elif isinstance(chunk, ToolReferenceChunk) and chunk.url:
                    if chunk.url not in sources:
                        sources[chunk.url] = WebSearchSource(
                            title=chunk.title, url=chunk.url
                        )

        answer = "".join(text_parts).strip()
        if not answer:
            raise ToolError("No text in agent response.")

        return WebSearchResult(
            query=query, answer=answer, sources=list(sources.values())
        )

    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        if event.args is None:
            return ToolCallDisplay(
                summary="web_search",
                verb="Running",
                message="web_search",
                settled_verb="Ran",
                settled_message="web_search",
            )
        if not isinstance(event.args, WebSearchArgs):
            return ToolCallDisplay(
                summary="web_search",
                verb="Running",
                message="web_search",
                settled_verb="Ran",
                settled_message="web_search",
            )
        return ToolCallDisplay(
            summary=f"Searching the web: {event.args.query!r}",
            verb="Searching",
            message=f"the web: {event.args.query!r}",
            settled_verb="Searched",
            settled_message=f"the web: {event.args.query!r}",
        )

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        if not isinstance(event.result, WebSearchResult):
            return ToolResultDisplay(
                success=False, message=event.error or event.skip_reason or "No result"
            )
        source_count = len(event.result.sources)
        plural = "" if source_count == 1 else "s"
        message = f"{event.result.query!r} ({source_count} source{plural})"
        return ToolResultDisplay(success=True, verb="Searched", message=message)

    @classmethod
    def get_status_text(cls) -> str:
        return "Searching the web"
