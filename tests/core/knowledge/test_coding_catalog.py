from __future__ import annotations

from hashlib import sha256

from vibe.core.knowledge import (
    ARTICLE_COUNT,
    VIRTUAL_SKILL_COUNT,
    get_coding_knowledge_catalog,
)


def test_catalog_has_required_unique_article_and_skill_counts() -> None:
    catalog = get_coding_knowledge_catalog()
    article_ids = list(catalog.iter_article_ids())
    skill_names = list(catalog.iter_skill_names())

    assert ARTICLE_COUNT == 10_000
    assert VIRTUAL_SKILL_COUNT == 1_000
    assert len(article_ids) == len(set(article_ids)) == 10_000
    assert len(skill_names) == len(set(skill_names)) == 1_000


def test_all_deepwiki_articles_have_unique_content() -> None:
    catalog = get_coding_knowledge_catalog()
    articles = [
        article
        for article_id in catalog.iter_article_ids()
        if (article := catalog.get_article(article_id)) is not None
    ]
    digests = {sha256(article.content.encode()).digest() for article in articles}

    assert max(len(article.content) for article in articles) < 4_000
    assert len(digests) == 10_000


def test_all_virtual_skills_have_unique_prompts() -> None:
    catalog = get_coding_knowledge_catalog()
    skills = [
        skill
        for name in catalog.iter_skill_names()
        if (skill := catalog.get_skill(name)) is not None
    ]
    digests = {sha256(skill.prompt.encode()).digest() for skill in skills}

    assert len(digests) == 1_000


def test_catalog_search_prioritizes_language_domain_and_concern() -> None:
    catalog = get_coding_knowledge_catalog()

    articles = catalog.search_articles(
        "python async control flow web backend", language="python", limit=3
    )
    skills = catalog.search_skills(
        "rust debug production failure", language="rust", limit=3
    )

    assert articles[0].id == "dw:python:web-backend:async-control-flow"
    assert skills[0].id == "coding-rust-debug-failure-production"

    russian = catalog.search_skills(
        "python отладка продакшен", language="python", limit=1
    )
    assert russian[0].id == "coding-python-debug-failure-production"


def test_virtual_skill_prompt_routes_to_bounded_deepwiki_retrieval() -> None:
    skill = get_coding_knowledge_catalog().get_skill(
        "coding-typescript-review-code-audit"
    )

    assert skill is not None
    assert "typescript" in skill.prompt
    assert "one to three directly relevant DeepWiki articles" in skill.prompt
