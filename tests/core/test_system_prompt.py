from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from tests.conftest import build_test_vibe_config
from vibe.config_values import WebSearchActivity
from vibe.core.config import AutonomyConfig, ProjectContextConfig
from vibe.core.system_prompt import (
    ProjectContextProvider,
    _get_boost_policy,
    _get_gauntlet_loop_policy,
    _get_local_reference_policy,
    _get_web_search_policy,
)


@pytest.mark.parametrize(
    ("activity", "expected"),
    [
        ("off", "Web search is disabled"),
        ("low", "only when the user explicitly asks"),
        ("medium", "time-sensitive facts"),
        ("high", "Proactively use web_search"),
        ("max", "Aggressively use web_search"),
    ],
)
def test_web_search_activity_changes_system_policy(
    activity: WebSearchActivity, expected: str
) -> None:
    config = build_test_vibe_config(
        autonomy=AutonomyConfig(web_search_activity=activity)
    )

    assert expected in _get_web_search_policy(config)


def test_local_codex_thread_policy_prefers_read_only_local_history() -> None:
    policy = _get_local_reference_policy()

    assert "not a web URL and not a Vibe session ID" in policy
    assert "~/.codex/sessions" in policy
    assert "do not invoke Vibe `/resume`" in policy
    assert "Never copy credentials" in policy


def test_gauntlet_loop_policy_requires_real_independent_comparison() -> None:
    config = build_test_vibe_config(autonomy=AutonomyConfig(gauntlet_loop=True))

    policy = _get_gauntlet_loop_policy(config)

    assert "named, fetchable" in policy
    assert "separate harsh critic" in policy
    assert "binary blind choice" in policy
    assert "robonuggets/gauntlet-loop" in policy


def test_gauntlet_loop_policy_is_absent_when_disabled() -> None:
    config = build_test_vibe_config(autonomy=AutonomyConfig(gauntlet_loop=False))

    assert _get_gauntlet_loop_policy(config) == ""


def test_boost_policy_requires_evidence_and_keeps_trivial_requests_direct() -> None:
    config = build_test_vibe_config(autonomy=AutonomyConfig(boost_mode=True))

    policy = _get_boost_policy(config)

    assert "enforced quality profile" in policy
    assert "require a fresh evidence-based reviewer verdict" in policy
    assert "Trivial conversation remains direct" in policy


@pytest.mark.skipif(os.name == "nt", reason="fake git shell script is POSIX-only")
def test_run_git_survives_non_utf8_output(tmp_path: Path, monkeypatch) -> None:
    # Fake git that prints bytes 0x80 0x81 (invalid UTF-8, and invalid gbk here)
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/sh\nprintf "commit \\200\\201 msg\\n"\n')
    fake_git.chmod(0o755)
    # Put the fake first on PATH so _run_git executes it instead of real git
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    provider = ProjectContextProvider(ProjectContextConfig(), root_path=tmp_path)

    # Without encoding="utf-8", errors="replace" this raises UnicodeDecodeError
    result = provider._run_git(["log"], timeout=5.0)

    # The bad bytes are replaced with U+FFFD instead of crashing
    assert "\ufffd" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="fake git shell script is POSIX-only")
def test_run_git_disables_fsmonitor_hook(tmp_path: Path, monkeypatch) -> None:
    # Fake git that records the argv it was invoked with, one arg per line.
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    provider = ProjectContextProvider(ProjectContextConfig(), root_path=tmp_path)
    result = provider._run_git(["status", "--porcelain"], timeout=5.0)

    argv = result.stdout.splitlines()
    # -c core.fsmonitor= must come before any positional git subcommand so it
    # actually overrides the repo's own config, and must not be overridable by
    # anything the invoked repo could inject via its own .git/config.
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == "core.fsmonitor="
    assert argv.index("-c") < argv.index("status")


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell payload")
def test_fetch_git_status_does_not_execute_malicious_fsmonitor_hook(
    tmp_path: Path,
) -> None:
    # Regression test for the RCE reported in #942: a repo's own .git/config
    # can declare core.fsmonitor as an arbitrary command, which git runs on
    # `status` (and other worktree-refreshing commands) with the invoking
    # user's full privileges -- and this runs on every session start, before
    # any trust dialog is shown to the user.
    repo = tmp_path / "malicious_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# README\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    payload_marker = tmp_path / "PWNED"
    subprocess.run(
        ["git", "config", "core.fsmonitor", f"touch {payload_marker}"],
        cwd=repo,
        check=True,
    )

    provider = ProjectContextProvider(ProjectContextConfig(), root_path=repo)
    status = provider.get_git_status()

    assert not payload_marker.exists()
    # The fix must not break normal status reporting.
    assert "Current branch:" in status
    assert "Git operations timed out" not in status
    assert "Not a git repository" not in status
