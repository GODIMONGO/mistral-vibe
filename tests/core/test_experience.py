from __future__ import annotations

from pathlib import Path

from vibe.core.experience import (
    ExperienceStore,
    project_experience_key,
    render_experience_context,
    sanitize_experience_text,
)


def test_experience_persists_and_retrieves_relevant_records(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    project_key = project_experience_key(tmp_path / "project")
    store.record_many(
        [
            (
                "git_bash",
                '{"command":"uv run pytest tests/core/test_memory.py"}',
                "success",
                "12 tests passed",
            ),
            (
                "web_search",
                '{"query":"unrelated release notes"}',
                "success",
                "Found documentation",
            ),
        ],
        project_key=project_key,
    )

    results = store.search(
        "verify memory with pytest", project_key=project_key, limit=1
    )

    assert len(results) == 1
    assert results[0].tool == "git_bash"
    assert results[0].outcome == "12 tests passed"


def test_experience_deduplicates_actions_and_updates_latest_outcome(
    tmp_path: Path,
) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    project_key = project_experience_key(tmp_path)
    record = ("git_bash", "uv run pyright", "failure", "one type error")
    store.record_many([record], project_key=project_key)
    store.record_many(
        [("git_bash", "uv run pyright", "success", "zero errors")],
        project_key=project_key,
    )

    results = store.search("pyright", project_key=project_key)

    assert store.count() == 1
    assert results[0].status == "success"
    assert results[0].outcome == "zero errors"
    assert results[0].seen_count == 2


def test_experience_excludes_unrelated_records_from_same_project(
    tmp_path: Path,
) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    project_key = project_experience_key(tmp_path)
    store.record_many(
        [("web_search", "find CSS documentation", "success", "grid docs found")],
        project_key=project_key,
    )

    assert store.search("diagnose database migration", project_key=project_key) == []


def test_experience_matches_related_word_stems(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    project_key = project_experience_key(tmp_path)
    store.record_many(
        [("git_bash", "проверка сборки", "success", "сборка прошла")],
        project_key=project_key,
    )

    results = store.search("проверить сборку", project_key=project_key)

    assert len(results) == 1
    assert results[0].outcome == "сборка прошла"


def test_experience_redacts_secrets_before_persistence(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    project_key = project_experience_key(tmp_path)
    secret = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    store.record_many(
        [("git_bash", f"export token={secret}", "failure", f"Bearer {secret}")],
        project_key=project_key,
    )

    [entry] = store.search("export failure", project_key=project_key)

    assert secret not in entry.action
    assert secret not in entry.outcome
    assert "[REDACTED]" in entry.action
    assert "[REDACTED]" in entry.outcome


def test_experience_context_is_bounded_and_marks_records_untrusted(
    tmp_path: Path,
) -> None:
    store = ExperienceStore(tmp_path / "experience.sqlite3")
    project_key = project_experience_key(tmp_path)
    store.record_many(
        [("bash", "run tests", "success", "<ignore> passed")], project_key=project_key
    )
    entries = store.search("run tests", project_key=project_key)

    rendered = render_experience_context(entries, max_chars=1_000)

    assert len(rendered) <= 1_000
    assert "untrusted historical data" in rendered
    assert "&lt;ignore&gt;" in rendered
    assert rendered.endswith("</personal_experience>")


def test_sanitize_experience_text_bounds_long_values() -> None:
    sanitized = sanitize_experience_text("x" * 2_000, limit=100)

    assert len(sanitized) == 100
    assert "omitted" in sanitized
