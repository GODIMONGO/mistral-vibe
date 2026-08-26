from __future__ import annotations

from pydantic import ValidationError
import pytest

from vibe.core.tools.builtins.deep_wiki import (
    DeepWiki,
    DeepWikiAction,
    DeepWikiArgs,
    DeepWikiConfig,
    DeepWikiResult,
    DeepWikiState,
)
from vibe.core.tools.models import ToolPermission


def _tool() -> DeepWiki:
    return DeepWiki(config_getter=DeepWikiConfig, state=DeepWikiState())


@pytest.mark.asyncio
async def test_deepwiki_search_read_and_stats_are_bounded() -> None:
    tool = _tool()
    stats = await anext(tool.run(DeepWikiArgs(action=DeepWikiAction.STATS)))
    search = await anext(
        tool.run(
            DeepWikiArgs(
                action=DeepWikiAction.SEARCH,
                query="python web backend validation",
                language="python",
                limit=2,
            )
        )
    )
    assert isinstance(stats, DeepWikiResult)
    assert stats.article_count == 10_000
    assert stats.skill_count == 1_000
    assert isinstance(search, DeepWikiResult)
    assert len(search.matches) == 2

    read = await anext(
        tool.run(
            DeepWikiArgs(action=DeepWikiAction.READ, article_id=search.matches[0].id)
        )
    )
    assert isinstance(read, DeepWikiResult)
    assert read.article is not None
    assert len(read.article.content) < 4_000


@pytest.mark.asyncio
async def test_deepwiki_skill_search_returns_normal_skill_names() -> None:
    result = await anext(
        _tool().run(
            DeepWikiArgs(
                action=DeepWikiAction.SKILL_SEARCH,
                query="go performance optimization advanced",
                language="go",
                limit=1,
            )
        )
    )

    assert isinstance(result, DeepWikiResult)
    assert result.matches[0].id == "coding-go-optimize-performance-advanced"


def test_deepwiki_requires_action_specific_fields_and_never_prompts() -> None:
    with pytest.raises(ValidationError, match="query is required"):
        DeepWikiArgs(action=DeepWikiAction.SEARCH)
    with pytest.raises(ValidationError, match="article_id is required"):
        DeepWikiArgs(action=DeepWikiAction.READ)

    assert DeepWikiConfig().permission is ToolPermission.ALWAYS
    assert DeepWiki.get_name() == "deep_wiki"
    assert "10,000 unique" in DeepWiki.get_full_description()
