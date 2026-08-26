from __future__ import annotations

from vibe.core.skills.builtins.coding_deepwiki import SKILL as CODING_DEEPWIKI_SKILL
from vibe.core.skills.builtins.skill_creator import SKILL as SKILL_CREATOR_SKILL
from vibe.core.skills.builtins.software_engineering import (
    SKILL as SOFTWARE_ENGINEERING_SKILL,
)
from vibe.core.skills.builtins.vibe import SKILL as VIBE_SKILL
from vibe.core.skills.builtins.web_engineering import SKILL as WEB_ENGINEERING_SKILL
from vibe.core.skills.models import SkillInfo

BUILTIN_SKILLS: dict[str, SkillInfo] = {
    skill.name: skill
    for skill in [
        VIBE_SKILL,
        SKILL_CREATOR_SKILL,
        CODING_DEEPWIKI_SKILL,
        SOFTWARE_ENGINEERING_SKILL,
        WEB_ENGINEERING_SKILL,
    ]
}
