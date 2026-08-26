from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import build_test_vibe_config
from tests.skills.conftest import create_skill
from vibe.core.skills.builtins import BUILTIN_SKILLS
from vibe.core.skills.manager import SkillManager


class TestBuiltinSkills:
    def test_vibe_skill_is_registered(self) -> None:
        assert "vibe" in BUILTIN_SKILLS

    def test_vibe_skill_has_no_path(self) -> None:
        assert BUILTIN_SKILLS["vibe"].skill_path is None

    def test_vibe_skill_has_inline_prompt(self) -> None:
        assert BUILTIN_SKILLS["vibe"].prompt

    def test_vibe_skill_references_powerstral_repository(self) -> None:
        prompt = BUILTIN_SKILLS["vibe"].prompt
        assert "__VIBE_VERSION__" not in prompt
        assert "https://github.com/GODIMONGO/powerstral" in prompt

    def test_vibe_skill_marks_upstream_docs_as_non_authoritative(self) -> None:
        assert "not authoritative for fork-only capabilities" in (
            BUILTIN_SKILLS["vibe"].prompt
        )

    def test_web_engineering_skill_is_registered_and_invocable(self) -> None:
        skill = BUILTIN_SKILLS["web-engineering"]

        assert skill.user_invocable is True
        assert "primary documentation" in skill.prompt
        assert "chrome_cdp" in skill.prompt

    def test_software_engineering_skill_is_registered_and_invocable(self) -> None:
        skill = BUILTIN_SKILLS["software-engineering"]

        assert skill.user_invocable is True
        assert "root cause" in skill.prompt
        assert "map every completion claim to direct evidence" in skill.prompt

    def test_coding_deepwiki_router_is_registered_without_eager_catalog(self) -> None:
        skill = BUILTIN_SKILLS["coding-deepwiki"]
        prompt = " ".join(skill.prompt.split())

        assert skill.user_invocable is True
        assert "1,000 specialized coding skills" in prompt
        assert "10,000 stable DeepWiki articles" in prompt

    def test_discovers_builtin_skills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("vibe.core.skills.manager.BUILTIN_SKILLS", BUILTIN_SKILLS)
        config = build_test_vibe_config()
        manager = SkillManager(lambda: config)

        assert "vibe" in manager.available_skills

    def test_user_skill_cannot_override_builtin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vibe.core.skills.manager.BUILTIN_SKILLS", BUILTIN_SKILLS)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        create_skill(skills_dir, "vibe", "Custom vibe override")

        config = build_test_vibe_config(skill_paths=[skills_dir])
        manager = SkillManager(lambda: config)

        assert "vibe" in manager.available_skills
        assert (
            manager.available_skills["vibe"].description
            == BUILTIN_SKILLS["vibe"].description
        )
