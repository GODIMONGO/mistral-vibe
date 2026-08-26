from __future__ import annotations

from collections.abc import AsyncGenerator
from enum import StrEnum, auto
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibe.core.knowledge import get_coding_knowledge_catalog
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.permissions import PermissionContext
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.types import ToolStreamEvent


class DeepWikiAction(StrEnum):
    STATS = auto()
    SEARCH = auto()
    READ = auto()
    SKILL_SEARCH = auto()


class DeepWikiArgs(BaseModel):
    action: DeepWikiAction
    query: str | None = Field(default=None, max_length=500, description="Search terms")
    article_id: str | None = Field(
        default=None, max_length=160, description="Stable id returned by search"
    )
    language: str | None = Field(
        default=None,
        max_length=40,
        description="Optional exact language slug, for example python or rust",
    )
    limit: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action in {DeepWikiAction.SEARCH, DeepWikiAction.SKILL_SEARCH}:
            if not self.query:
                raise ValueError(f"query is required for action='{self.action}'")
        if self.action is DeepWikiAction.READ and not self.article_id:
            raise ValueError("article_id is required for action='read'")
        return self


class DeepWikiMatch(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    score: int
    language: str
    domain: str | None = None
    concern: str | None = None
    workflow: str | None = None
    level: str | None = None


class DeepWikiArticle(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    language: str
    domain: str
    concern: str
    content: str


class DeepWikiResult(BaseModel):
    action: DeepWikiAction
    article_count: int
    skill_count: int
    matches: list[DeepWikiMatch] = Field(default_factory=list)
    article: DeepWikiArticle | None = None
    message: str


class DeepWikiConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS


class DeepWikiState(BaseToolState):
    pass


class DeepWiki(
    BaseTool[DeepWikiArgs, DeepWikiResult, DeepWikiConfig, DeepWikiState],
    ToolUIData[DeepWikiArgs, DeepWikiResult],
):
    @classmethod
    def format_call_display(cls, args: DeepWikiArgs) -> ToolCallDisplay:
        target = args.article_id or args.query or "catalog"
        return ToolCallDisplay(
            summary=f"DeepWiki {args.action}: {target}",
            verb="Searching" if "search" in args.action else "Reading",
            message=target,
            settled_verb="Found" if "search" in args.action else "Read",
            settled_message=target,
        )

    @classmethod
    def format_result_display(cls, result: DeepWikiResult) -> ToolResultDisplay:
        return ToolResultDisplay(success=True, message=result.message)

    @classmethod
    def get_status_text(cls) -> str:
        return "Searching coding DeepWiki"

    def resolve_permission(self, args: DeepWikiArgs) -> PermissionContext | None:
        return PermissionContext(permission=ToolPermission.ALWAYS)

    async def run(
        self, args: DeepWikiArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | DeepWikiResult, None]:
        catalog = get_coding_knowledge_catalog()
        common = {
            "action": args.action,
            "article_count": catalog.article_count,
            "skill_count": catalog.skill_count,
        }
        match args.action:
            case DeepWikiAction.STATS:
                yield DeepWikiResult(
                    **common,
                    message=(
                        f"{catalog.article_count} articles; "
                        f"{catalog.skill_count} virtual coding skills"
                    ),
                )
            case DeepWikiAction.SEARCH:
                matches = catalog.search_articles(
                    args.query or "", language=args.language, limit=args.limit
                )
                yield DeepWikiResult(
                    **common,
                    matches=[DeepWikiMatch.model_validate(item) for item in matches],
                    message=f"{len(matches)} relevant articles",
                )
            case DeepWikiAction.READ:
                article = catalog.get_article(args.article_id or "")
                if article is None:
                    raise ToolError(f"DeepWiki article not found: {args.article_id}")
                yield DeepWikiResult(
                    **common,
                    article=DeepWikiArticle.model_validate(article),
                    message=article.title,
                )
            case DeepWikiAction.SKILL_SEARCH:
                matches = catalog.search_skills(
                    args.query or "", language=args.language, limit=args.limit
                )
                yield DeepWikiResult(
                    **common,
                    matches=[DeepWikiMatch.model_validate(item) for item in matches],
                    message=(
                        f"{len(matches)} virtual skills; load a returned id with "
                        "the skill tool"
                    ),
                )


__all__ = [
    "DeepWiki",
    "DeepWikiAction",
    "DeepWikiArgs",
    "DeepWikiArticle",
    "DeepWikiConfig",
    "DeepWikiMatch",
    "DeepWikiResult",
    "DeepWikiState",
]
