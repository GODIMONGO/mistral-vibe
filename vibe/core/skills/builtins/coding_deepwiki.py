from __future__ import annotations

from vibe.core.skills.models import SkillInfo, SkillSource

_PROMPT = """# Coding DeepWiki Router

Use Powerstral's coding knowledge progressively instead of loading a broad manual.

1. Identify the language, engineering workflow, domain, and concrete uncertainty.
2. Call `deep_wiki` with `action="skill_search"`; load the best returned id with
   the normal `skill` tool. Load one virtual skill, not a stack of near-duplicates.
3. Call `deep_wiki` with `action="search"`; read only one to three articles whose
   contents can change the plan or verification strategy.
4. Treat DeepWiki as durable engineering guidance. Use web search and primary
   documentation for current APIs, versions, vulnerabilities, and framework rules.
5. Keep the main context lean: retain selected decisions, constraints, article ids,
   and verification gates, not the full search results.

The virtual catalog contains exactly 1,000 specialized coding skills and 10,000
stable DeepWiki articles. A virtual skill returned by search is a normal skill-tool
target even though all virtual names are intentionally omitted from the startup
prompt and slash menu.
"""

SKILL = SkillInfo(
    name="coding-deepwiki",
    description=(
        "Use for non-trivial coding work that benefits from targeted language, "
        "workflow, architecture, debugging, testing, security, performance, or "
        "deployment guidance. Routes lazily into 1,000 virtual coding skills and "
        "10,000 DeepWiki articles without loading the catalog into startup context."
    ),
    prompt=_PROMPT,
    source=SkillSource.BUILTIN,
    user_invocable=True,
)

__all__ = ["SKILL"]
