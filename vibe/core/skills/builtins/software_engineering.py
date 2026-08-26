from __future__ import annotations

from vibe.core.skills.models import SkillInfo, SkillSource

_PROMPT = """# Software Engineering

Use a repository-first, evidence-backed workflow for implementation, debugging,
refactoring, performance work, and code review.

1. Read repository instructions and trace the real execution path before editing.
   State the desired behavior, constraints, and observable acceptance criteria.
2. For non-trivial work, compare at least two plausible approaches. Prefer the
   smallest change that fixes the root cause and preserves public contracts.
3. Inspect types, callers, tests, configuration, persistence, concurrency, error
   paths, and platform boundaries affected by the change. Do not infer behavior
   from a function or file name.
4. Implement in dependency order. Keep changes focused, typed, reversible where
   practical, and consistent with the existing architecture. Never overwrite
   unrelated user changes or hide a failing check.
5. Validate in layers: reproduce or characterize the old behavior, run focused
   tests, formatter and type checks, then broader checks proportional to risk.
   For performance claims, measure a relevant baseline and the changed behavior.
6. Re-open the final diff and map every completion claim to direct evidence.
   Distinguish what was observed from what remains inferred or untested.

Load a more specific domain skill too when the task involves web engineering,
security, deployment, documents, or another specialized surface. Keep exploration
and test output bounded; retain compact facts, decisions, changed files, failed
routes, and verification results instead of copying full logs into the main context.
"""

SKILL = SkillInfo(
    name="software-engineering",
    description=(
        "Use for implementing, debugging, refactoring, optimizing, or reviewing "
        "non-trivial software changes. It adds root-cause analysis, alternative "
        "design comparison, repository-aware implementation, layered verification, "
        "and evidence-backed completion. Pair with a narrower domain skill when one "
        "matches."
    ),
    prompt=_PROMPT,
    source=SkillSource.BUILTIN,
    user_invocable=True,
)

__all__ = ["SKILL"]
