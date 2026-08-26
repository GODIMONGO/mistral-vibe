from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from itertools import product
import re

_LANGUAGES = {
    "python": "Prefer explicit types, context managers, pathlib, isolated environments, and measured async boundaries.",
    "typescript": "Keep strict types at external boundaries, narrow unknown values, and preserve runtime validation beside static contracts.",
    "javascript": "Make runtime shapes, module semantics, promise ownership, and browser or Node compatibility explicit.",
    "rust": "Model ownership and error variants directly; minimize cloning, unsafe blocks, and hidden allocation.",
    "go": "Keep interfaces consumer-owned, contexts bounded, goroutine lifetime visible, and errors wrapped with actionable context.",
    "java": "Preserve package contracts, explicit resource lifetime, checked concurrency assumptions, and build-tool reproducibility.",
    "kotlin": "Use nullability and sealed types deliberately; keep coroutine scope, cancellation, and Java interop visible.",
    "csharp": "Respect nullable annotations, disposal, async propagation, LINQ allocation, and framework lifecycle contracts.",
    "cpp": "Use RAII, value semantics, sanitizers, explicit ownership, and compiler diagnostics across supported standards.",
    "c": "Make allocation, bounds, integer width, ownership, errno, ABI, and cleanup paths mechanically auditable.",
    "ruby": "Keep object protocols small, mutation visible, exceptions bounded, and gem/runtime compatibility tested.",
    "php": "Use strict types, validate request data, isolate framework globals, and verify Composer plus runtime compatibility.",
    "swift": "Model value/reference semantics, actor isolation, optionals, Sendable boundaries, and platform lifecycle explicitly.",
    "objective-c": "Audit ARC ownership, nullability, selectors, bridging, runtime dispatch, and Swift interoperability.",
    "dart": "Preserve sound null safety, isolate boundaries, Future cancellation expectations, and Flutter widget lifecycle.",
    "scala": "Control implicit resolution, effect boundaries, collection cost, binary compatibility, and JVM interop.",
    "elixir": "Design supervision, message contracts, process state, backpressure, and OTP restart semantics first.",
    "erlang": "Use explicit process protocols, supervision trees, mailbox bounds, hot-code assumptions, and dialyzer contracts.",
    "haskell": "Separate pure decisions from effects, keep laziness and space use observable, and make partiality explicit.",
    "ocaml": "Use exhaustive variants, module signatures, explicit effects, allocation awareness, and stable serialization.",
    "fsharp": "Prefer total discriminated-union flows, isolate .NET effects, and test units-of-measure and interop edges.",
    "lua": "Define table shapes and metatable behavior, bound globals, and account for host runtime and GC differences.",
    "r": "Separate vectorized data transforms from side effects, preserve NA semantics, and pin package/data assumptions.",
    "julia": "Keep type stability, multiple-dispatch intent, allocation, world-age, and environment reproducibility measurable.",
    "zig": "Expose allocators, error unions, comptime choices, ABI assumptions, and cleanup through defer or errdefer.",
}

_DOMAINS = {
    "cli": "Treat parsing, exit codes, stdout/stderr, cancellation, terminals, and cross-platform paths as public API.",
    "web-frontend": "Verify DOM semantics, accessibility, responsive states, hydration, network failures, and bundle cost in a browser.",
    "web-backend": "Define request limits, validation, authorization, transaction scope, concurrency, and failure responses.",
    "api": "Version schemas, distinguish authentication from authorization, bound retries, and preserve idempotent semantics.",
    "database": "Measure queries, make transaction and isolation boundaries explicit, and rehearse migration rollback.",
    "distributed-systems": "Assume partial failure, duplication, reordering, clock skew, split brain, and delayed observation.",
    "concurrency": "Name owners, cancellation, synchronization, fairness, backpressure, and shutdown order.",
    "testing": "Choose tests by risk, keep fixtures deterministic, and distinguish contract, integration, property, and UI evidence.",
    "security": "Identify assets, trust boundaries, attacker control, privilege changes, secret flow, and auditable denials.",
    "performance": "Measure a representative baseline, attribute cost, change one bottleneck, and guard against regression.",
    "observability": "Emit bounded structured signals that connect user impact to traces, metrics, logs, and actionable ownership.",
    "build-systems": "Keep dependency graphs deterministic, caches sound, artifacts reproducible, and platform matrices explicit.",
    "package-management": "Pin intent, verify provenance and compatibility, minimize transitive risk, and test clean installation.",
    "mobile": "Account for lifecycle suspension, offline state, constrained resources, permissions, upgrades, and device variation.",
    "desktop": "Handle window lifecycle, native integration, updates, input, accessibility, persistence, and process cleanup.",
    "embedded": "Budget memory, timing, power, interrupts, hardware state, watchdogs, and recoverable update paths.",
    "data-engineering": "Preserve schema, lineage, partitioning, replay, late data, data quality, and bounded backfills.",
    "machine-learning": "Version data and models, prevent leakage, measure drift, bound inference, and retain reproducible evaluation.",
    "devops": "Make desired state, credentials, rollout, health, rollback, blast radius, and environment differences explicit.",
    "interoperability": "Specify wire format, ABI, encoding, ownership, version negotiation, and conformance fixtures.",
}

_CONCERNS = {
    "architecture": "Map responsibilities and dependency direction before adding abstractions; reject cycles and duplicated ownership.",
    "correctness": "State invariants, counterexamples, boundary conditions, and direct evidence for every completion claim.",
    "debugging": "Reproduce first, reduce the failing path, collect discriminating evidence, and fix the confirmed root cause.",
    "error-handling": "Classify expected, retryable, user, dependency, and programmer failures without erasing causal context.",
    "typing": "Represent valid states, validate untrusted inputs, avoid unchecked casts, and test runtime/static boundaries.",
    "state-management": "Define the source of truth, transitions, persistence, invalidation, concurrency, and recovery semantics.",
    "resource-lifetime": "Assign ownership and deterministic cleanup for files, sockets, processes, locks, tasks, and memory.",
    "async-control-flow": "Propagate cancellation and deadlines, bound concurrency, avoid orphan tasks, and preserve ordering contracts.",
    "serialization": "Version formats, specify encoding and numeric limits, reject hostile shapes, and round-trip representative fixtures.",
    "validation": "Validate at trust boundaries, return actionable errors, and keep normalization separate from authorization.",
    "authentication": "Protect credentials and sessions, verify issuer/audience/lifetime, rotate safely, and fail closed.",
    "authorization": "Check the requested action on the target resource server-side; test confused-deputy and tenancy boundaries.",
    "caching": "Define keys, freshness, invalidation, stampede control, negative entries, and behavior under partial outage.",
    "idempotency": "Choose stable operation identity, persist outcomes atomically, and define replay plus expiration behavior.",
    "migrations": "Use expand-contract sequencing, compatibility windows, resumability, observability, and rehearsed rollback.",
    "compatibility": "Enumerate supported versions and platforms, preserve contracts, and test upgrade/downgrade edges.",
    "profiling": "Use representative workloads and the correct profiler; separate CPU, allocation, I/O, lock, and latency cost.",
    "testing-strategy": "Map risks to the cheapest decisive tests and retain integration evidence for real boundaries.",
    "deployment": "Use staged rollout, health gates, immutable artifacts, configuration validation, and automatic rollback signals.",
    "maintenance": "Optimize for readable ownership, bounded dependencies, discoverable operations, and safe future modification.",
}

_WORKFLOWS = {
    "implement-feature": "Translate the request into observable acceptance criteria, inspect existing contracts, compare designs, then implement in dependency order.",
    "debug-failure": "Reproduce the failure, trace the real path, test competing root-cause hypotheses, then verify the fix against the reproduction.",
    "refactor-design": "Characterize behavior first, identify the pressure requiring change, preserve contracts, and migrate in reviewable steps.",
    "optimize-performance": "Capture a representative baseline, profile, change the dominant cost, and prove both speed and semantic equivalence.",
    "review-code": "Read callers and tests, challenge assumptions, prioritize concrete defects by impact, and cite direct evidence.",
    "write-tests": "Select boundary, property, integration, concurrency, and failure cases from risk rather than implementation shape.",
    "secure-boundary": "Model assets and attacker control, inspect every trust transition, test denials, and avoid logging secrets.",
    "design-api": "Start from consumer outcomes, define schemas and errors, handle evolution and idempotency, then write conformance tests.",
    "migrate-system": "Inventory compatibility constraints, use staged reversible steps, observe progress, and rehearse rollback before cutover.",
    "automate-build": "Model inputs and artifacts, pin dependencies, make caching sound, and verify clean reproducible execution on supported platforms.",
}

_LEVELS = {
    "foundation": "Focus on the smallest correct implementation and decisive local verification.",
    "production": "Include failure handling, operational visibility, compatibility, and realistic integration checks.",
    "advanced": "Compare architectural alternatives, concurrency and performance effects, and long-term evolution cost.",
    "audit": "Assume claims are wrong until supported; inspect adversarial cases and require evidence for every acceptance criterion.",
}

ARTICLE_COUNT = len(_LANGUAGES) * len(_DOMAINS) * len(_CONCERNS)
VIRTUAL_SKILL_COUNT = len(_LANGUAGES) * len(_WORKFLOWS) * len(_LEVELS)
_TOKEN_RE = re.compile(r"[\w+#.-]+", re.UNICODE)
_ARTICLE_ID_PARTS = 4
_QUERY_ALIASES = {
    "архитект": {"architecture", "design"},
    "бэкенд": {"web", "backend"},
    "валидац": {"validation"},
    "веб": {"web"},
    "деплой": {"deployment", "devops"},
    "интеграц": {"interoperability", "integration"},
    "кэш": {"caching"},
    "миграц": {"migration", "migrate"},
    "наблюдаем": {"observability"},
    "обработк": {"handling"},
    "оптимиз": {"optimize", "performance"},
    "отлад": {"debug", "debugging", "failure"},
    "памят": {"memory", "resource", "lifetime"},
    "производ": {"performance", "profiling", "optimize"},
    "продакшен": {"production"},
    "развертыв": {"deployment"},
    "распредел": {"distributed", "systems"},
    "сериализац": {"serialization"},
    "согласован": {"correctness", "state"},
    "тест": {"test", "testing", "tests"},
    "типиз": {"typing", "types"},
    "фронтенд": {"web", "frontend"},
    "безопас": {"security", "secure"},
}


@dataclass(frozen=True, slots=True)
class CodingArticle:
    id: str
    title: str
    language: str
    domain: str
    concern: str
    content: str


@dataclass(frozen=True, slots=True)
class CodingSkill:
    name: str
    title: str
    language: str
    workflow: str
    level: str
    description: str
    prompt: str


@dataclass(frozen=True, slots=True)
class KnowledgeMatch:
    id: str
    title: str
    score: int
    language: str
    domain: str | None = None
    concern: str | None = None
    workflow: str | None = None
    level: str | None = None


def _tokens(value: str) -> set[str]:
    normalized = value.casefold().replace("_", " ").replace("-", " ")
    tokens = set(_TOKEN_RE.findall(normalized))
    for token in tuple(tokens):
        for prefix, replacements in _QUERY_ALIASES.items():
            if token.startswith(prefix):
                tokens.update(replacements)
    return tokens


def _score(query: set[str], fields: tuple[str, ...]) -> int:
    field_tokens = [_tokens(field) for field in fields]
    return sum(
        (len(query & tokens) * weight)
        for tokens, weight in zip(field_tokens, (8, 5, 3, 2), strict=False)
    )


class CodingKnowledgeCatalog:
    @property
    def article_count(self) -> int:
        return ARTICLE_COUNT

    @property
    def skill_count(self) -> int:
        return VIRTUAL_SKILL_COUNT

    def iter_article_ids(self) -> Iterator[str]:
        for language, domain, concern in product(_LANGUAGES, _DOMAINS, _CONCERNS):
            yield f"dw:{language}:{domain}:{concern}"

    def iter_skill_names(self) -> Iterator[str]:
        for language, workflow, level in product(_LANGUAGES, _WORKFLOWS, _LEVELS):
            yield f"coding-{language}-{workflow}-{level}"

    def get_article(self, article_id: str) -> CodingArticle | None:
        parts = article_id.split(":")
        if len(parts) != _ARTICLE_ID_PARTS or parts[0] != "dw":
            return None
        _, language, domain, concern = parts
        if (
            language not in _LANGUAGES
            or domain not in _DOMAINS
            or concern not in _CONCERNS
        ):
            return None
        title = f"{language}: {concern} for {domain}"
        content = "\n\n".join([
            f"# {title}",
            f"DeepWiki article `{article_id}`.",
            "## Language lens\n" + _LANGUAGES[language],
            "## Domain constraints\n" + _DOMAINS[domain],
            "## Engineering decision\n" + _CONCERNS[concern],
            (
                "## Working method\n"
                f"For a {language} {domain} change centered on {concern}, first "
                "inspect repository instructions and the real execution path. "
                "Write observable acceptance criteria, compare a minimal route with "
                "a structurally different route, and implement only after the "
                "decisive unknowns are tested. Keep assumptions separate from facts."
            ),
            (
                "## Verification gate\n"
                f"Exercise the {concern} boundary directly in the {domain} runtime; "
                f"run the {language} formatter, static checks, focused tests, and the "
                "smallest realistic integration scenario. Re-open changed artifacts "
                "and map each completion claim to observed output. If the boundary "
                "cannot be exercised, report that limitation instead of claiming it."
            ),
            (
                "## Failure and pivot signals\n"
                "Pivot when the reproduction contradicts the assumed root cause, "
                "when the proposed contract breaks a supported consumer, or when "
                "measurement shows the changed path is not the dominant risk."
            ),
        ])
        return CodingArticle(
            id=article_id,
            title=title,
            language=language,
            domain=domain,
            concern=concern,
            content=content,
        )

    def get_skill(self, name: str) -> CodingSkill | None:
        for language, workflow, level in product(_LANGUAGES, _WORKFLOWS, _LEVELS):
            expected = f"coding-{language}-{workflow}-{level}"
            if name != expected:
                continue
            title = f"{language} {workflow} ({level})"
            description = (
                f"Use for {level} {workflow.replace('-', ' ')} work in {language}. "
                "Loads language-specific engineering constraints, evidence-first "
                "execution, and targeted DeepWiki retrieval."
            )
            prompt = "\n\n".join([
                f"# Coding skill: {title}",
                _LANGUAGES[language],
                _WORKFLOWS[workflow],
                _LEVELS[level],
                (
                    "Before editing, inspect repository instructions and retrieve "
                    "one to three directly relevant DeepWiki articles with the "
                    "`deep_wiki` tool. Do not load broad background material. Build "
                    "a short dependency-ordered plan, keep logs bounded, and require "
                    "observable verification before claiming completion."
                ),
            ])
            return CodingSkill(
                name=name,
                title=title,
                language=language,
                workflow=workflow,
                level=level,
                description=description,
                prompt=prompt,
            )
        return None

    def search_articles(
        self, query: str, *, language: str | None = None, limit: int = 5
    ) -> list[KnowledgeMatch]:
        query_tokens = _tokens(query)
        matches: list[KnowledgeMatch] = []
        for candidate_language, domain, concern in product(
            _LANGUAGES, _DOMAINS, _CONCERNS
        ):
            if language is not None and candidate_language != language:
                continue
            score = _score(
                query_tokens,
                (
                    candidate_language,
                    domain,
                    concern,
                    _LANGUAGES[candidate_language]
                    + " "
                    + _DOMAINS[domain]
                    + " "
                    + _CONCERNS[concern],
                ),
            )
            if score == 0:
                continue
            article_id = f"dw:{candidate_language}:{domain}:{concern}"
            matches.append(
                KnowledgeMatch(
                    id=article_id,
                    title=f"{candidate_language}: {concern} for {domain}",
                    score=score,
                    language=candidate_language,
                    domain=domain,
                    concern=concern,
                )
            )
        matches.sort(key=lambda item: (-item.score, item.id))
        return matches[:limit]

    def search_skills(
        self, query: str, *, language: str | None = None, limit: int = 5
    ) -> list[KnowledgeMatch]:
        query_tokens = _tokens(query)
        matches: list[KnowledgeMatch] = []
        for candidate_language, workflow, level in product(
            _LANGUAGES, _WORKFLOWS, _LEVELS
        ):
            if language is not None and candidate_language != language:
                continue
            score = _score(
                query_tokens,
                (
                    candidate_language,
                    workflow,
                    level,
                    _WORKFLOWS[workflow] + " " + _LEVELS[level],
                ),
            )
            if score == 0:
                continue
            name = f"coding-{candidate_language}-{workflow}-{level}"
            matches.append(
                KnowledgeMatch(
                    id=name,
                    title=f"{candidate_language} {workflow} ({level})",
                    score=score,
                    language=candidate_language,
                    workflow=workflow,
                    level=level,
                )
            )
        matches.sort(key=lambda item: (-item.score, item.id))
        return matches[:limit]


@cache
def get_coding_knowledge_catalog() -> CodingKnowledgeCatalog:
    return CodingKnowledgeCatalog()


__all__ = [
    "ARTICLE_COUNT",
    "VIRTUAL_SKILL_COUNT",
    "CodingArticle",
    "CodingKnowledgeCatalog",
    "CodingSkill",
    "KnowledgeMatch",
    "get_coding_knowledge_catalog",
]
