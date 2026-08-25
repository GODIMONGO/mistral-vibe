from __future__ import annotations

from vibe.core.skills.models import SkillInfo, SkillSource

_PROMPT = """# Web Engineering

Use an evidence-first workflow for production web applications, APIs, browser
automation, and deployment.

1. Inspect repository instructions, the framework, lockfiles, scripts, existing
   architecture, and tests before choosing an implementation.
2. For unstable APIs, security guidance, browser behavior, or unfamiliar
   libraries, search the web and prefer primary documentation. Fetch the actual
   source page before relying on a search snippet.
3. Make client, server, data, authentication, and deployment boundaries explicit.
   Preserve project conventions and avoid introducing a second framework or
   state-management pattern without evidence that it is needed.
4. Implement typed contracts, validation, bounded errors, loading/empty/error
   states, responsive layout, keyboard access, semantic HTML, and accessible
   labels. Never expose credentials to client bundles, logs, URLs, or tool output.
5. Check security at trust boundaries: authentication versus authorization,
   injection, CSRF/CORS, SSRF, unsafe redirects, uploads, dependency risk, and
   secret handling. Check performance where measurements justify it: bundle size,
   request waterfalls, caching, rendering, database queries, and memory lifetime.
6. Validate with the repository's formatter, type checker, tests, and build.
   Exercise important flows in a real browser through `chrome_cdp` or the
   available computer tool. Inspect console/network/DOM state when relevant.
7. Do not claim that UI, browser, network, accessibility, or deployment behavior
   works unless you observed it or ran a check that directly supports the claim.
   Report remaining uncertainty and blockers plainly.

When the task is small, keep this process proportional: inspect, change, run the
smallest decisive checks, and answer briefly.
"""

SKILL = SkillInfo(
    name="web-engineering",
    description=(
        "Use for building, debugging, reviewing, or deploying web frontends, "
        "backends, APIs, browser UX, and full-stack applications. It adds "
        "primary-source research, architecture, security, accessibility, "
        "performance, and real-browser verification. Do not load for generic "
        "non-web coding tasks."
    ),
    prompt=_PROMPT,
    source=SkillSource.BUILTIN,
    user_invocable=True,
)

__all__ = ["SKILL"]
