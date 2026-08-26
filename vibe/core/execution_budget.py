from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, auto
import re
import time


class TaskScale(StrEnum):
    DIRECT = auto()
    MINI = auto()
    MEDIUM = auto()
    LARGE = auto()


class BudgetSignal(StrEnum):
    CONTINUE = auto()
    VERIFY = auto()
    EXHAUSTED = auto()


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    scale: TaskScale
    max_parallel_subagents: int
    max_review_retries: int
    max_plan_tasks: int
    advisor_turns: int
    worker_turns: int
    reviewer_turns: int
    advisor_timeout_seconds: int
    worker_timeout_seconds: int
    reviewer_timeout_seconds: int


_PROFILES = {
    TaskScale.DIRECT: ExecutionProfile(
        scale=TaskScale.DIRECT,
        max_parallel_subagents=0,
        max_review_retries=0,
        max_plan_tasks=0,
        advisor_turns=0,
        worker_turns=0,
        reviewer_turns=0,
        advisor_timeout_seconds=0,
        worker_timeout_seconds=0,
        reviewer_timeout_seconds=0,
    ),
    TaskScale.MINI: ExecutionProfile(
        scale=TaskScale.MINI,
        max_parallel_subagents=1,
        max_review_retries=0,
        max_plan_tasks=3,
        advisor_turns=3,
        worker_turns=6,
        reviewer_turns=3,
        advisor_timeout_seconds=2 * 60,
        worker_timeout_seconds=8 * 60,
        reviewer_timeout_seconds=3 * 60,
    ),
    TaskScale.MEDIUM: ExecutionProfile(
        scale=TaskScale.MEDIUM,
        max_parallel_subagents=3,
        max_review_retries=1,
        max_plan_tasks=6,
        advisor_turns=4,
        worker_turns=10,
        reviewer_turns=5,
        advisor_timeout_seconds=3 * 60,
        worker_timeout_seconds=12 * 60,
        reviewer_timeout_seconds=5 * 60,
    ),
    TaskScale.LARGE: ExecutionProfile(
        scale=TaskScale.LARGE,
        max_parallel_subagents=4,
        max_review_retries=2,
        max_plan_tasks=12,
        advisor_turns=6,
        worker_turns=18,
        reviewer_turns=8,
        advisor_timeout_seconds=5 * 60,
        worker_timeout_seconds=25 * 60,
        reviewer_timeout_seconds=10 * 60,
    ),
}

_ACTION_MARKERS = (
    "add",
    "build",
    "change",
    "debug",
    "deploy",
    "design",
    "fix",
    "implement",
    "install",
    "migrate",
    "optimize",
    "refactor",
    "remove",
    "test",
    "update",
    "write",
    "добав",
    "внедр",
    "выполн",
    "задепло",
    "измен",
    "исправ",
    "мигр",
    "напис",
    "обнов",
    "оптимиз",
    "передел",
    "постав",
    "реализ",
    "рефактор",
    "созда",
    "тест",
    "удал",
)
_MEDIUM_MARKERS = (
    "api",
    "backend",
    "database",
    "frontend",
    "integration",
    "multiple files",
    "security",
    "ui test",
    "баз дан",
    "интеграц",
    "нескольк",
    "безопас",
    "фронтенд",
    "бэкенд",
)
_LARGE_MARKERS = (
    "all tasks",
    "architecture",
    "complete redesign",
    "entire project",
    "full audit",
    "full redesign",
    "production",
    "two repositories",
    "все задач",
    "весь проект",
    "полный аудит",
    "полный редизайн",
    "полностью передел",
    "продакшен",
    "архитектур",
)
_DIRECT_MARKERS = (
    "explain",
    "what is",
    "why is",
    "расскажи",
    "объясни",
    "почему",
    "что такое",
)
_TRIVIAL = frozenset({
    "hello",
    "hey",
    "hi",
    "thanks",
    "здравствуй",
    "привет",
    "спасибо",
})
_PATH_PATTERN = re.compile(r"(?:[a-zA-Z]:[\\/]|(?:^|\s)[./][\w.-]+[\\/])")
_DIRECT_QUESTION_MAX_CHARS = 120
_MEDIUM_OBJECTIVE_CHARS = 240
_LARGE_OBJECTIVE_CHARS = 600
_MEDIUM_SCORE = 3
_LARGE_SCORE = 7
_MAX_IDENTICAL_READS = 2


def _is_direct_task(objective: str, normalized: str, action_count: int) -> bool:
    if normalized.strip(" .!?\t\r\n") in _TRIVIAL:
        return True
    if action_count != 0:
        return False
    return any(marker in normalized for marker in _DIRECT_MARKERS) or (
        len(normalized) < _DIRECT_QUESTION_MAX_CHARS and "?" in objective
    )


def classify_task(objective: str, *, force_large: bool = False) -> ExecutionProfile:
    normalized = " ".join(objective.casefold().split())
    action_count = sum(marker in normalized for marker in _ACTION_MARKERS)
    if _is_direct_task(objective, normalized, action_count):
        return _PROFILES[TaskScale.DIRECT]
    if force_large and action_count:
        return _PROFILES[TaskScale.LARGE]

    score = min(action_count, 3)
    score += sum(marker in normalized for marker in _MEDIUM_MARKERS)
    score += 3 * sum(marker in normalized for marker in _LARGE_MARKERS)
    score += min(len(_PATH_PATTERN.findall(objective)), 2)
    score += min(normalized.count(" and ") + normalized.count(" и "), 2)
    if len(normalized) >= _LARGE_OBJECTIVE_CHARS:
        score += 2
    elif len(normalized) >= _MEDIUM_OBJECTIVE_CHARS:
        score += 1

    scale = TaskScale.MINI
    if score >= _LARGE_SCORE:
        scale = TaskScale.LARGE
    elif score >= _MEDIUM_SCORE:
        scale = TaskScale.MEDIUM
    return _PROFILES[scale]


def requires_autonomous_pipeline(objective: str) -> bool:
    """Return whether local evidence proves this is substantive executable work."""
    normalized = " ".join(objective.casefold().split())
    action_count = sum(marker in normalized for marker in _ACTION_MARKERS)
    if _is_direct_task(objective, normalized, action_count):
        return False
    return bool(
        action_count
        or any(marker in normalized for marker in _MEDIUM_MARKERS)
        or _PATH_PATTERN.search(objective)
    )


def execution_profile(scale: TaskScale) -> ExecutionProfile:
    return _PROFILES[scale]


@dataclass(slots=True)
class ExecutionBudgetTracker:
    profile: ExecutionProfile
    initial_tokens: int
    clock: Callable[[], float] = time.monotonic
    started_at: float = 0.0
    main_turns: int = 0
    verification_announced: bool = False
    read_fingerprints: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.started_at == 0.0:
            self.started_at = self.clock()
        if self.read_fingerprints is None:
            self.read_fingerprints = {}

    def record_main_turn(self) -> None:
        self.main_turns += 1

    def record_mutation(self) -> None:
        if self.read_fingerprints is not None:
            self.read_fingerprints.clear()

    def repeated_read(self, tool_name: str, arguments: str) -> bool:
        if tool_name.endswith("_output") or tool_name in {"skill", "task", "todo"}:
            return False
        fingerprints = self.read_fingerprints
        if fingerprints is None:
            return False
        fingerprint = f"{tool_name}:{arguments}"
        count = fingerprints.get(fingerprint, 0) + 1
        fingerprints[fingerprint] = count
        return count > _MAX_IDENTICAL_READS

    def remaining_seconds(self) -> None:
        return None

    def check(self, total_tokens: int) -> tuple[BudgetSignal, str | None]:
        del total_tokens
        return BudgetSignal.CONTINUE, None


__all__ = [
    "BudgetSignal",
    "ExecutionBudgetTracker",
    "ExecutionProfile",
    "TaskScale",
    "classify_task",
    "execution_profile",
    "requires_autonomous_pipeline",
]
