from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator, Sequence
import contextlib
import copy
from dataclasses import dataclass, replace
from enum import StrEnum, auto
from functools import wraps
from http import HTTPStatus
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import threading
from threading import Thread
import time
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, JsonValue, ValidationError

from vibe.config_values import WebSearchActivity
from vibe.core.agent_loop._request_broker import InteractionRequestBroker
from vibe.core.agent_loop_hooks import AgentLoopHooksMixin, PostToolFinalization
from vibe.core.agents.manager import AgentManager
from vibe.core.agents.models import AgentProfile, BuiltinAgentName
from vibe.core.autocompletion.path_prompt import build_path_prompt_payload
from vibe.core.autonomy import (
    AUTONOMY_PLAN_END,
    AUTONOMY_PLAN_START,
    AutonomyCoordinator,
    AutonomyDecisionKind,
    AutonomyIntent,
    AutonomyPlan,
    AutonomyPlanTask,
    AutonomyPolicy,
    parse_advisor_plan,
    parse_autonomy_intent,
)
from vibe.core.checkpoints import Checkpointer, CheckpointRecorder, FileStore
from vibe.core.compaction import (
    CompactionFailedError as CompactionFailedError,
    CompactionManager,
)
from vibe.core.compaction.context import (
    extract_summary,
    render_teleport_summary_request,
    select_model_context,
)
from vibe.core.config import (
    ModelConfig,
    ProviderConfig,
    ThinkingLevel,
    VibeConfigSchema,
)
from vibe.core.config.harness_files import (
    HarnessFilesManager,
    get_harness_files_manager,
)
from vibe.core.config.layers.growthbook import GrowthbookLayer
from vibe.core.config.orchestrator import ConfigOrchestrator
from vibe.core.execution_budget import (
    ExecutionBudgetTracker,
    ExecutionProfile,
    TaskScale,
    classify_task,
    execution_profile,
    requires_autonomous_pipeline,
)
from vibe.core.experience import (
    ExperienceStore,
    ExperienceStoreError,
    project_experience_key,
    render_experience_context,
    sanitize_experience_text,
)
from vibe.core.experiments import ExperimentManager
from vibe.core.experiments.client import RemoteEvalClient
from vibe.core.experiments.models import EvalResponse
from vibe.core.experiments.session import (
    hydrate_experiments_from_session as session_hydrate_experiments_from_session,
    initialize_experiments as session_initialize_experiments,
)
from vibe.core.harness import (
    HarnessDecision,
    HarnessPhase,
    HarnessPlugin,
    HarnessRuntime,
    create_default_harness,
)
from vibe.core.hooks.config import load_hooks_from_fs
from vibe.core.hooks.manager import HooksManager
from vibe.core.hooks.models import HookConfigResult, HookEvent
from vibe.core.identity_cache import IdentityCache
from vibe.core.llm.backend.factory import create_backend
from vibe.core.llm.exceptions import BackendError, IncompleteStreamError
from vibe.core.llm.failover import ModelFailoverState
from vibe.core.llm.format import (
    APIToolFormatHandler,
    FailedToolCall,
    ResolvedMessage,
    ResolvedToolCall,
)
from vibe.core.llm.types import BackendLike
from vibe.core.memory import (
    GlobalMemoryStore,
    MemoryStoreError,
    build_working_memory_message,
    is_working_memory_message,
    load_working_memory,
)
from vibe.core.middleware import (
    PLAN_AGENT_EXIT,
    AutoCompactMiddleware,
    AutonomyRefreshMiddleware,
    ContextWarningMiddleware,
    ConversationContext,
    ExecutionBudgetMiddleware,
    MiddlewareAction,
    MiddlewarePipeline,
    MiddlewareResult,
    PriceLimitMiddleware,
    ReadOnlyAgentMiddleware,
    ResetReason,
    TokenLimitMiddleware,
    TurnLimitMiddleware,
    make_plan_agent_reminder,
)
from vibe.core.plan_session import PlanSession
from vibe.core.prompts import UtilityPrompt
from vibe.core.review import ReviewManager
from vibe.core.rewind import RewindManager
from vibe.core.scratchpad import cleanup_scratchpad, init_scratchpad
from vibe.core.session.session_id import extract_suffix, generate_session_id
from vibe.core.session.session_lease import SessionLease
from vibe.core.session.session_logger import SessionLogger
from vibe.core.session.session_migration import migrate_sessions_entrypoint
from vibe.core.skills.manager import SkillManager
from vibe.core.subagents import SubagentRunnerPort, TaskArgs, TaskResult
from vibe.core.system_prompt import get_universal_system_prompt
from vibe.core.telemetry.build_metadata import (
    build_attachment_counts,
    build_request_metadata,
)
from vibe.core.telemetry.send import TelemetryClient
from vibe.core.telemetry.types import (
    LaunchContext,
    ProjectPickerTelemetryPayload,
    TelemetryCallType,
    TelemetryRequestMetadata,
)
from vibe.core.teleport.errors import ServiceTeleportError
from vibe.core.teleport.telemetry import TeleportTelemetryTracker
from vibe.core.teleport.types import (
    TELEPORT_MESSAGE_CONTEXT_MAX_LENGTH,
    TeleportCompleteEvent,
    TeleportMessageContext,
    TeleportMessageContextSource,
    TeleportSummarizingContextEvent,
)
from vibe.core.tools.base import (
    BaseTool,
    CancellableToolResult,
    InvokeContext,
    ToolError,
    ToolPermission,
    ToolPermissionError,
)
from vibe.core.tools.builtins.read_file import ReadFileArgs
from vibe.core.tools.builtins.skill import (
    Skill as SkillTool,
    SkillArgs,
    select_skill_result,
    skill_content_marker,
)
from vibe.core.tools.builtins.todo import TodoArgs, TodoItem, TodoResult, TodoStatus
from vibe.core.tools.builtins.web_search import WebSearchArgs
from vibe.core.tools.io_port import ToolIOPort
from vibe.core.tools.manager import NoSuchToolError, ToolManager
from vibe.core.tools.permissions import (
    ApprovedRule,
    PermissionContext,
    PermissionStore,
    RequiredPermission,
)
from vibe.core.tools.ui import ToolUIDataAdapter
from vibe.core.tracing import (
    agent_span,
    build_otel_span_exporter_config,
    set_tool_result,
    tool_span,
)
from vibe.core.trusted_folders import has_agents_md_file
from vibe.core.types import (
    AgentProfileChangedEvent,
    AgentStats,
    ApprovalResponse,
    AssistantEvent,
    AvailableTool,
    Backend,
    BaseEvent,
    ChildSessionLink,
    CompactEndEvent,
    CompactStartEvent,
    ContextClearedEvent,
    ContextTooLongError,
    FunctionCall,
    ImageAttachment,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    ManualShellContext,
    MemoryStatusEvent,
    MessageList,
    PersistedToolResult,
    PlanReviewEndedEvent,
    PlanReviewRequestedEvent,
    RateLimitError,
    ReasoningEvent,
    RefusalError,
    ResponseTooLongError,
    Role,
    SessionMetadata,
    SessionTitleUpdatedEvent,
    StrToolChoice,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
    UserMessageEvent,
)
from vibe.core.utils import (
    TOOL_ERROR_TAG,
    VIBE_STOP_EVENT_TAG,
    CancellationReason,
    RetryCategory,
    RetryObserver,
    RetryReason,
    get_user_cancellation_message,
    is_user_cancellation_event,
)
from vibe.observability.logging import log_model_call_success, logger
from vibe.setup.auth.whoami import WhoAmICache
from vibe.user_content import UserDisplayContent, UserResource

_EXPERIENCE_QUERY_MESSAGE_LIMIT = 4
from vibe.utils import VIBE_WARNING_TAG
from vibe.utils.api_keys import resolve_api_key
from vibe.utils.cache_store import CacheStore, InMemoryCacheStore
from vibe.utils.http import get_server_url_from_api_base, get_user_agent
from vibe.utils.tool_presentation import ToolEffectKind

_AUTONOMY_OBJECTIVE_MAX_CHARS = 6_000
_AUTONOMY_REVIEW_EVIDENCE_MAX_CHARS = 8_000
_AUTONOMY_DEPENDENCY_RESULT_MAX_CHARS = 2_000
_AUTONOMY_DEPENDENCY_CONTEXT_MAX_CHARS = 6_000
_AUTONOMY_STORED_RESULT_MAX_CHARS = 4_000
_AUTONOMY_TASK_PASS_MARKER = "TASK_RESULT: PASS"
_ADVISOR_FALLBACK_MAX_TOKENS = 4_096
_LOCAL_OR_SENSITIVE_SEARCH_RE = re.compile(
    r"(?:\b(?:codex|file|vscode)://|"
    r"(?:^|[\s\"'`(])(?:[a-z]:[\\/]|\\\\)|"
    r"\b(?:localhost|127\.0\.0\.1)(?::\d{2,5})?\b|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[ _-]?key|password|passwd|secret|access[ _-]?token|"
    r"refresh[ _-]?token|authorization)\b[\"']?\s*[:=]\s*[\"']?\S+|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"\b\d{8,12}:AA[A-Za-z0-9_-]{20,})",
    re.IGNORECASE,
)
_PERFORMANCE_MUTATION_EFFECTS = frozenset({
    ToolEffectKind.FILE_EDIT,
    ToolEffectKind.FILE_WRITE,
    ToolEffectKind.SHELL,
    ToolEffectKind.WORKTREE,
})
_VIBE_DELIBERATION_MAX_TOKENS: dict[ThinkingLevel, int] = {
    "off": 0,
    "low": 512,
    "medium": 640,
    "high": 768,
    "max": 896,
}
_VIBE_DELIBERATION_SYNTHESIS_MAX_TOKENS: dict[ThinkingLevel, int] = {
    "off": 0,
    "low": 512,
    "medium": 640,
    "high": 768,
    "max": 896,
}
_VIBE_DELIBERATION_CONTEXT_MAX_CHARS: dict[ThinkingLevel, int] = {
    "off": 0,
    "low": 4_000,
    "medium": 6_000,
    "high": 9_000,
    "max": 12_000,
}
_VIBE_DELIBERATION_MESSAGE_MAX_CHARS = 2_400
_VIBE_DELIBERATION_MEMORY_SHARE = 4
_VIBE_FULL_THINKING_INTERVAL = 10
_VIBE_FAST_THINKING_MAX_TOKENS = 192
_VIBE_DELIBERATION_LENSES = {
    1: ("strategy challenge",),
    2: ("direction audit", "alternative search"),
    3: ("direction audit", "alternative search", "adversarial decision"),
    4: (
        "direction audit",
        "alternative search",
        "failure rehearsal",
        "constraint-first design",
    ),
}
_VIBE_DELIBERATION_TASKS = {
    "strategy challenge": (
        "Restate the real outcome, expose the weakest assumption, generate two "
        "materially different routes, and choose continue, pivot, or clarify."
    ),
    "direction audit": (
        "Judge whether the current path serves the user's real outcome. Identify "
        "fixation, unsupported assumptions, and evidence that would falsify it."
    ),
    "alternative search": (
        "Construct at least two genuinely different approaches, including a simpler "
        "or safer one, and compare their costs, risks, and information value."
    ),
    "failure rehearsal": (
        "Assume the current plan failed. Work backward to the likely cause, detect "
        "the earliest warning sign, and define a reversible escape route."
    ),
    "adversarial decision": (
        "Attack the leading approach using the strongest alternative, then choose "
        "continue, pivot, or clarify and name the decisive evidence."
    ),
    "constraint-first design": (
        "Ignore the leading route and derive a solution from the user's constraints, "
        "available evidence, reversibility, and the cheapest decisive verification."
    ),
}
_VISIBLE_THINKING_STATUS_CHARS = 120


def _operational_brief_fields(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip().lstrip("-*# ").strip("` ")
        key, separator, value = line.partition(":")
        normalized_key = key.strip("` ").upper()
        if not separator or not normalized_key or not value.strip():
            continue
        if not normalized_key.replace(" ", "").isalpha():
            continue
        fields.setdefault(
            normalized_key, sanitize_experience_text(value.strip("` "), limit=320)
        )
    return fields


def _visible_thinking_summary(
    title: str, brief: str, field_labels: tuple[tuple[str, str], ...]
) -> tuple[str, str]:
    fields = _operational_brief_fields(brief)
    selected = [
        (label, fields[field]) for field, label in field_labels if field in fields
    ]
    if not selected:
        return "", f"{title} · result ready"

    content = "\n".join([
        f"\n**{title}**",
        *(f"- **{label}:** {value}" for label, value in selected),
        "",
    ])
    direction = fields.get("DIRECTION")
    focus = next(
        (
            fields[field]
            for field in ("NEXT ACTION", "NEXT", "GAP", "PLAN", "DECISION")
            if field in fields
        ),
        selected[0][1],
    )
    status_parts = [title]
    if direction:
        status_parts.append(direction)
    status_parts.append(focus)
    status = " · ".join(status_parts)
    return content, sanitize_experience_text(
        status, limit=_VISIBLE_THINKING_STATUS_CHARS
    )


def _compact_autonomy_text(content: str, max_chars: int, label: str) -> str:
    if len(content) <= max_chars:
        return content
    marker = f"\n\n[... {label} omitted to save context ...]\n\n"
    if max_chars <= len(marker):
        return content[:max_chars]
    available = max_chars - len(marker)
    head_chars = available * 2 // 3
    tail_chars = available - head_chars
    return content[:head_chars] + marker + content[-tail_chars:]


def _extract_tagged_text(content: str, tag: str) -> str:
    opening = f"<{tag}>"
    closing = f"</{tag}>"
    start = content.find(opening)
    if start < 0:
        return ""
    end = content.find(closing, start + len(opening))
    if end < 0:
        return ""
    return content[start : end + len(closing)]


def _is_git_executable_available() -> bool:
    executable = os.environ.get("GIT_PYTHON_GIT_EXECUTABLE")
    if not executable:
        return shutil.which("git") is not None

    path = Path(executable).expanduser()
    if path.is_absolute() or os.sep in executable:
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


_TELEPORT_AVAILABLE = _is_git_executable_available()


def _load_teleport_service() -> type[TeleportService]:
    try:
        from vibe.core.teleport.teleport import TeleportService
    except ImportError as e:
        raise TeleportError(
            "Teleport requires git to be installed. Please install git and try again."
        ) from e
    return TeleportService


if TYPE_CHECKING:
    from opentelemetry import trace

    from vibe.core.teleport.teleport import TeleportService
    from vibe.core.teleport.types import TeleportPushResponseEvent, TeleportYieldEvent
    from vibe.core.tools.connectors.connector_registry import ConnectorRegistry
    from vibe.core.tools.mcp.pool import MCPConnectionPool
    from vibe.core.tools.mcp.registry import MCPRegistry
    from vibe.core.tools.mcp_sampling import MCPSamplingHandler


class ToolExecutionResponse(StrEnum):
    SKIP = auto()
    EXECUTE = auto()


class ToolDecision(BaseModel):
    verdict: ToolExecutionResponse
    approval_type: ToolPermission
    feedback: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRuntimePolicy:
    max_turns: int | None
    max_price: float | None
    max_tokens: int | None
    max_session_tokens: int | None
    enable_streaming: bool
    launch_context: LaunchContext | None
    headless: bool
    hook_config_result: HookConfigResult | None
    permission_store: PermissionStore
    cache_store: CacheStore
    force_bypass_tool_permissions: bool
    local_managed_shell_runtime_enabled: bool
    model_failover_state: ModelFailoverState


class _SwappableConfigSource:
    """Config getter for reload-prepared managers.

    Points at the target agent's config while preparation runs off-loop, then is
    repointed to the live config inside the synchronous commit. The prepared
    managers are not shared with the running turn until commit, so the running
    turn only ever observes the live getter.
    """

    def __init__(self, getter: Callable[[], VibeConfigSchema]) -> None:
        self._getter = getter

    def get(self) -> VibeConfigSchema:
        return self._getter()

    def point_to(self, getter: Callable[[], VibeConfigSchema]) -> None:
        self._getter = getter


@dataclass(frozen=True, slots=True)
class _PreparedReload:
    backend: BackendLike
    tool_manager: ToolManager
    skill_manager: SkillManager
    system_prompt: str
    config_source: _SwappableConfigSource
    hook_config_result: HookConfigResult | None


@dataclass(frozen=True, slots=True)
class AgentTurnOptions:
    retry_sink: RetryObserver | None = None
    injected: bool = False


@dataclass(frozen=True, slots=True)
class _ActiveTurn:
    """Collaborators lent to the loop for one turn; its presence means one is running."""

    subagent_runner: SubagentRunnerPort | None = None
    tool_io: ToolIOPort | None = None
    retry_sink: RetryObserver | None = None


_NO_TURN = _ActiveTurn()


class AgentLoopError(Exception):
    """Base exception for AgentLoop errors."""


class AgentLoopStateError(AgentLoopError):
    """Raised when agent loop is in an invalid state."""


class AgentLoopLLMResponseError(AgentLoopError):
    """Raised when LLM response is malformed or missing expected data."""


class ImagesNotSupportedError(AgentLoopError):
    """Raised when the active model does not support image attachments."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(model)


class TeleportError(AgentLoopError):
    """Raised when teleport to Vibe Code fails."""


def _refusal_error(provider: str, model: str, chunk: LLMChunk) -> RefusalError:
    stop = chunk.stop
    return RefusalError(
        provider,
        model,
        category=stop.category if stop else None,
        explanation=stop.explanation if stop else None,
    )


def _log_model_call_failure(
    alias: str, provider: str, error: Exception, duration_ms: int
) -> None:
    backend_error = _extract_backend_error(error)
    if backend_error is not None:
        logger.warning(
            "Model call failed model=%s duration_ms=%d\n%s",
            alias,
            duration_ms,
            str(backend_error),
        )
    else:
        logger.warning(
            "Model call failed model=%s provider=%s error=%s duration_ms=%d",
            alias,
            provider,
            type(error).__name__,
            duration_ms,
        )


def _extract_backend_error(error: BaseException) -> BackendError | None:
    if isinstance(error, BackendError):
        return error
    if isinstance(error, RuntimeError) and isinstance(error.__cause__, BackendError):
        return error.__cause__
    return None


def _should_raise_rate_limit_error(e: Exception) -> bool:
    return isinstance(e, BackendError) and e.status == HTTPStatus.TOO_MANY_REQUESTS


def _is_context_too_long_error(e: Exception) -> bool:
    if isinstance(e, BackendError):
        return e.is_context_too_long or e.is_large_payload_transport_error
    if isinstance(e, RuntimeError) and isinstance(e.__cause__, BackendError):
        return (
            e.__cause__.is_context_too_long
            or e.__cause__.is_large_payload_transport_error
        )
    return False


def _is_response_too_long_error(e: Exception) -> bool:
    if isinstance(e, BackendError):
        return e.is_response_too_long
    if isinstance(e, RuntimeError) and isinstance(e.__cause__, BackendError):
        return e.__cause__.is_response_too_long
    return False


def _is_non_retryable_error(e: BaseException) -> bool:
    # Detect Temporal-style ``non_retryable`` flag without importing temporalio.
    # Walks ``__cause__`` so an ``ActivityError`` whose cause is a non-retryable
    # ``ApplicationError`` is detected too — that's what callers driving the
    # agent loop from a Temporal activity will see when a sub-activity has
    # already failed terminally.
    seen: set[int] = set()
    current: BaseException | None = e
    while current is not None and id(current) not in seen:
        if getattr(current, "non_retryable", False):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def requires_init(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that awaits deferred initialization before executing the method."""
    if inspect.isasyncgenfunction(fn):

        @wraps(fn)
        async def gen_wrapper(self: AgentLoop, *args: Any, **kwargs: Any) -> Any:
            await self.wait_until_ready()
            agen = fn(self, *args, **kwargs)
            sent: Any = None
            try:
                while True:
                    sent = yield await agen.asend(sent)
            except StopAsyncIteration:
                return
            finally:
                await agen.aclose()

        return gen_wrapper

    @wraps(fn)
    async def wrapper(self: AgentLoop, *args: Any, **kwargs: Any) -> Any:
        await self.wait_until_ready()
        return await fn(self, *args, **kwargs)

    return wrapper


class AgentLoop(AgentLoopHooksMixin):  # noqa: PLR0904
    def __init__(  # noqa: PLR0913, PLR0915
        self,
        config_orchestrator: ConfigOrchestrator[VibeConfigSchema],
        *,
        agent_name: str = BuiltinAgentName.ACCEPT_EDITS,
        max_turns: int | None = None,
        max_price: float | None = None,
        max_tokens: int | None = None,
        max_session_tokens: int | None = None,
        backend: BackendLike | None = None,
        model_failover_state: ModelFailoverState | None = None,
        enable_streaming: bool = False,
        launch_context: LaunchContext | None = None,
        is_subagent: bool = False,
        defer_heavy_init: bool = False,
        headless: bool = False,
        hook_config_result: HookConfigResult | None = None,
        permission_store: PermissionStore | None = None,
        mcp_registry: MCPRegistry | None = None,
        cache_store: CacheStore | None = None,
        force_bypass_tool_permissions: bool = False,
        local_managed_shell_runtime_enabled: bool = True,
        experiment_state: EvalResponse | None = None,
        await_experiment_model: bool = False,
        parent_session_id: str | None = None,
        cwd: Path | None = None,
        harness_files: HarnessFilesManager | None = None,
        session_id: str | None = None,
        session_dir: Path | None = None,
        session_lease: SessionLease | None = None,
        harness_plugins: Sequence[HarnessPlugin] = (),
    ) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.harness_files = replace(
            harness_files or get_harness_files_manager(), cwd=self.cwd
        )
        self._config_orchestrator = config_orchestrator
        self._force_bypass_tool_permissions = force_bypass_tool_permissions
        self._local_managed_shell_runtime_enabled = local_managed_shell_runtime_enabled
        self._headless = headless
        self._is_subagent = is_subagent
        self.cache_store = cache_store or InMemoryCacheStore()
        self.harness: HarnessRuntime = create_default_harness(harness_plugins)

        self._defer_heavy_init = defer_heavy_init
        self._deferred_init_thread: threading.Thread | None = None
        self._deferred_init_lock = threading.Lock()
        self._init_error: Exception | None = None
        self._init_start_time = time.monotonic()
        self._experiments_task: asyncio.Task[None] | None = None
        self._reload_generation: int = 0
        self._pending_new_session_telemetry: bool = False
        self._deferred_new_session_telemetry: bool = False
        self._ready_telemetry_pending: bool = defer_heavy_init
        self._last_init_duration_ms: int | None = None

        self._permission_store = permission_store or PermissionStore()
        self.session_id = session_id or generate_session_id()
        self._session_lease = session_lease
        self.parent_session_id = parent_session_id
        self.scratchpad_dir = (
            init_scratchpad(self.session_id) if not is_subagent else None
        )

        self.mcp_registry: MCPRegistry | None = (
            mcp_registry
            if defer_heavy_init
            else mcp_registry or self._create_mcp_registry()
        )
        self._mcp_pool: MCPConnectionPool | None = (
            None if defer_heavy_init else self._create_mcp_pool()
        )
        self.connector_registry: ConnectorRegistry | None = (
            None if defer_heavy_init else self._create_connector_registry()
        )
        self.agent_manager = AgentManager(
            self._config_orchestrator,
            initial_agent=agent_name,
            allow_subagent=is_subagent,
            harness_files=self.harness_files,
        )
        config = self.config
        self.experiment_manager = ExperimentManager(
            client=RemoteEvalClient.from_settings(
                api_host=config.experiments.api_host,
                client_key=config.experiments.client_key,
            )
        )
        if experiment_state is not None:
            self.experiment_manager.hydrate(experiment_state)

        self._await_experiment_model = await_experiment_model
        self.identity_cache = IdentityCache()
        self.whoami_cache = WhoAmICache()
        self.tool_manager = ToolManager(
            lambda: self.config,
            mcp_registry=self.mcp_registry,
            connector_registry=self.connector_registry,
            defer_mcp=True,
            permission_getter=self._permission_store.get_tool_permission,
            local_managed_shell_runtime_enabled=self._local_managed_shell_runtime_enabled,
            cwd=self.cwd,
            harness_files=self.harness_files,
            scratchpad_dir=self.scratchpad_dir,
        )
        self.skill_manager = SkillManager(
            lambda: self.config, harness_files=self.harness_files
        )
        self._max_turns = max_turns
        self._max_price = max_price
        self._max_tokens = max_tokens
        self._max_session_tokens = max_session_tokens
        self._plan_session = PlanSession()
        self._user_plan: str | None = None

        self.format_handler = APIToolFormatHandler()

        self._injected_backend = backend
        self._model_failover_state = model_failover_state or ModelFailoverState()
        self.backend = self.backend_factory()
        self._sampling_handler = self._create_sampling_handler(
            backend_getter=lambda: self.backend,
            config_getter=lambda: self.config,
            metadata_getter=lambda: self._build_backend_metadata(
                call_type="secondary_call"
            ).model_dump(exclude_none=True),
            extra_headers_getter=self._get_extra_headers,
        )

        self.enable_streaming = enable_streaming
        self.middleware_pipeline = MiddlewarePipeline()
        self._setup_middleware()

        self.messages = MessageList()

        self.stats = AgentStats()
        self._tool_event_queue: asyncio.Queue[BaseEvent | None] | None = None
        self._pending_working_memory_records: list[
            tuple[str, str, Literal["success", "failure", "skipped"], str]
        ] = []
        self._request_broker = InteractionRequestBroker()
        self._active_turn: _ActiveTurn | None = None
        self.launch_context = launch_context
        config = self.config
        try:
            active_model = config.get_active_model()
            self.stats.input_price_per_million = active_model.input_price
            self.stats.output_price_per_million = active_model.output_price
            self.stats.cached_input_price_per_million = active_model.cached_input_price
        except ValueError:
            pass

        self._current_user_message_id: str | None = None
        self._is_user_prompt_call: bool = False
        self._reactive_recovery_used: bool = False
        self._autonomy_advisor_completed: bool = False
        self._autonomy_bootstrap_blocked: bool = False
        self._autonomy_coordinator: AutonomyCoordinator | None = None
        self._autonomy_evidence: list[str] = []
        self._autonomy_objective: str = ""
        self._autonomy_plan: AutonomyPlan | None = None
        self._autonomy_todos: list[TodoItem] = []
        self._autonomy_completed_tasks: set[str] = set()
        self._autonomy_task_results: dict[str, str] = {}
        self._autonomy_scheduler_needs_resume: bool = False
        self._automatic_todo_write_succeeded: bool = False
        self._automatic_wave_progressed: bool = False
        self._pending_injected_messages: list[LLMMessage] = []
        self._pending_clear_context: bool = False
        self._active_vibe_deliberation_message: LLMMessage | None = None
        self._active_experience_message: LLMMessage | None = None
        self._experience_context_fresh: bool = False
        self._reported_experience_fingerprints: frozenset[str] | None = None
        self._experience_error_reported: bool = False
        self._experience_store = ExperienceStore()
        self._automatic_web_queries: set[str] = set()
        self._vibe_turns_since_full: int = _VIBE_FULL_THINKING_INTERVAL
        self._global_memory_status_reported: bool = False
        self._execution_profile: ExecutionProfile | None = None
        self._execution_budget: ExecutionBudgetTracker | None = None

        self.telemetry_client = TelemetryClient(
            config_getter=lambda: self.config,
            session_id_getter=lambda: self.session_id,
            parent_session_id_getter=lambda: self.parent_session_id,
            launch_context=self.launch_context,
            experiments_getter=lambda: self.experiment_manager.assignments(),
            user_plan_getter=lambda: self.user_plan,
        )
        self.session_logger = SessionLogger(
            config.session_logging,
            self.session_id,
            cwd=self.cwd,
            session_dir=session_dir,
        )
        if self.session_logger.session_metadata is not None:
            self.session_logger.session_metadata.parent_session_id = parent_session_id
        self._hook_config_result = hook_config_result
        self._hooks_manager = (
            HooksManager(hook_config_result.hooks, cwd=self.cwd)
            if hook_config_result
            else None
        )
        self.hook_config_issues = (
            hook_config_result.issues if hook_config_result else []
        )
        self.hooks_count = len(hook_config_result.hooks) if hook_config_result else 0
        checkpointer = Checkpointer()
        file_store = FileStore()
        self.checkpoint_recorder = CheckpointRecorder(
            checkpointer, self.messages, file_store
        )
        self.review_manager = ReviewManager(checkpointer, file_store)
        self.rewind_manager = RewindManager(
            checkpointer,
            messages=self.messages,
            save_messages=self._save_messages,
            reset_session=self._reset_session,
            files=file_store,
        )
        self.compaction_manager = CompactionManager(
            messages=self.messages,
            stats_getter=lambda: self.stats,
            config_getter=lambda: self.config,
            complete=self._complete,
            available_tools=lambda: self.format_handler.get_available_tools(
                self.tool_manager
            ),
            tool_choice=self.format_handler.get_tool_choice,
            save=self._save_messages,
            telemetry_client=self.telemetry_client,
            session_ids=lambda: (self.session_id, self.parent_session_id),
        )
        self._teleport_service: TeleportService | None = None

        Thread(
            target=migrate_sessions_entrypoint,
            args=(config.session_logging,),
            daemon=True,
            name="migrate_sessions",
        ).start()

        if defer_heavy_init:
            self._start_deferred_init()
        else:
            self._complete_init()
            if err := self._init_error:
                raise err

    def _start_deferred_init(self) -> threading.Thread:
        """Spawn a daemon thread that finishes deferred heavy I/O once."""
        with self._deferred_init_lock:
            if self._deferred_init_thread is not None:
                return self._deferred_init_thread

            thread = threading.Thread(
                target=self._complete_init, daemon=True, name="agent_loop_init"
            )
            self._deferred_init_thread = thread
            thread.start()
            return thread

    @property
    def is_initialized(self) -> bool:
        """Whether deferred initialization has completed (successfully or not)."""
        if not self._defer_heavy_init:
            return True
        thread = self._deferred_init_thread
        return thread is not None and not thread.is_alive()

    @property
    def awaiting_experiment_model(self) -> bool:
        if not self._await_experiment_model:
            return False
        task = self._experiments_task
        return task is None or not task.done()

    def _complete_init(self) -> None:
        """Run deferred heavy I/O: MCP and connector discovery.

        Intended to be called from a background thread when
        ``defer_heavy_init=True`` was passed to ``__init__``.
        """
        try:
            self._ensure_remote_registries()
            self.tool_manager.integrate_all(raise_on_mcp_failure=True)
            self.messages.update_system_prompt(self._build_system_prompt())
        except Exception as exc:
            self._init_error = exc

    async def wait_until_ready(self) -> None:
        """Await deferred initialization (MCP + experiments) from an async context."""
        await self._await_deferred_init()
        self._ensure_init_duration_recorded()
        if self._pending_new_session_telemetry:
            self._pending_new_session_telemetry = False
            self.emit_new_session_telemetry()

    async def _await_deferred_init(self) -> None:
        """Await only the deferred init thread + experiments task."""
        if self._defer_heavy_init:
            thread = self._start_deferred_init()
            await asyncio.to_thread(thread.join)
            if err := self._init_error:
                raise copy.copy(err).with_traceback(err.__traceback__)
        if (task := self._experiments_task) is not None:
            if task is asyncio.current_task():
                return
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _ensure_init_duration_recorded(self) -> None:
        """Record init duration exactly once; emit ready telemetry on fresh start.

        Idempotent. Emits the ``ready`` event only on the fresh-start path
        (``_ready_telemetry_pending``); resume clears that flag, so the event
        stays suppressed while the duration is still recorded.
        """
        if self._last_init_duration_ms is not None:
            return
        if not self._ready_telemetry_pending and not self._defer_heavy_init:
            return
        duration = int((time.monotonic() - self._init_start_time) * 1000)
        self._last_init_duration_ms = duration
        if self._ready_telemetry_pending:
            self._ready_telemetry_pending = False
            self.emit_ready_telemetry(duration)

    @property
    def agent_profile(self) -> AgentProfile:
        return self.agent_manager.active_profile

    @property
    def config_orchestrator(self) -> ConfigOrchestrator[VibeConfigSchema]:
        return self._config_orchestrator

    def _sync_growthbook_layer_variants(self) -> None:
        with contextlib.suppress(AttributeError, KeyError):
            layer = self.config_orchestrator.get_layer(GrowthbookLayer.NAME)
            if isinstance(layer, GrowthbookLayer):
                layer.set_variants(self.experiment_manager.config_variants())

    @property
    def config(self) -> VibeConfigSchema:
        return self.agent_manager.config

    @property
    def bypass_tool_permissions(self) -> bool:
        return (
            self._force_bypass_tool_permissions or self.config.bypass_tool_permissions
        )

    @property
    def runtime_policy(self) -> AgentRuntimePolicy:
        return AgentRuntimePolicy(
            max_turns=self._max_turns,
            max_price=self._max_price,
            max_tokens=self._max_tokens,
            max_session_tokens=self._max_session_tokens,
            enable_streaming=self.enable_streaming,
            launch_context=self.launch_context,
            headless=self._headless,
            hook_config_result=self._hook_config_result,
            permission_store=self._permission_store,
            cache_store=self.cache_store,
            force_bypass_tool_permissions=self.bypass_tool_permissions,
            local_managed_shell_runtime_enabled=self._local_managed_shell_runtime_enabled,
            model_failover_state=self._model_failover_state,
        )

    async def record_child_session(
        self, child: AgentLoop, tool_call_id: str
    ) -> ChildSessionLink:
        parent_dir = self.session_logger.session_dir
        child_dir = child.session_logger.session_dir
        relative_path = (
            str(child_dir.relative_to(parent_dir))
            if parent_dir is not None and child_dir is not None
            else None
        )
        link = ChildSessionLink(
            session_id=child.session_id,
            tool_call_id=tool_call_id,
            agent=child.agent_profile.name,
            relative_path=relative_path,
        )
        metadata = self.session_logger.session_metadata
        if metadata is None:
            return link
        existing = next(
            (
                item
                for item in metadata.child_sessions
                if item.tool_call_id == tool_call_id
            ),
            None,
        )
        if existing is not None:
            if existing != link:
                raise RuntimeError(
                    f"Tool call {tool_call_id} is already linked to a child session"
                )
            return existing
        metadata.child_sessions.append(link)
        try:
            await self._save_messages()
            await self.session_logger.persist_child_sessions()
        except BaseException:
            metadata.child_sessions.remove(link)
            raise
        return link

    async def replace_child_session(
        self, old_session_id: str, child: AgentLoop, tool_call_id: str
    ) -> ChildSessionLink:
        parent_dir = self.session_logger.session_dir
        child_dir = child.session_logger.session_dir
        replacement = ChildSessionLink(
            session_id=child.session_id,
            tool_call_id=tool_call_id,
            agent=child.agent_profile.name,
            relative_path=(
                str(child_dir.relative_to(parent_dir))
                if parent_dir is not None and child_dir is not None
                else None
            ),
        )
        metadata = self.session_logger.session_metadata
        if metadata is None:
            return replacement
        index = next(
            (
                index
                for index, link in enumerate(metadata.child_sessions)
                if link.session_id == old_session_id
                and link.tool_call_id == tool_call_id
            ),
            None,
        )
        if index is None:
            raise RuntimeError(f"Child session link not found: {old_session_id}")
        previous = metadata.child_sessions[index]
        metadata.child_sessions[index] = replacement
        try:
            await self.session_logger.persist_child_sessions()
        except BaseException:
            metadata.child_sessions[index] = previous
            raise
        return replacement

    async def forget_child_session(
        self, child_session_id: str, tool_call_id: str
    ) -> None:
        metadata = self.session_logger.session_metadata
        if metadata is None:
            return
        index = next(
            (
                index
                for index, link in enumerate(metadata.child_sessions)
                if link.session_id == child_session_id
                and link.tool_call_id == tool_call_id
            ),
            None,
        )
        if index is None:
            return
        link = metadata.child_sessions.pop(index)
        try:
            await self.session_logger.persist_child_sessions()
        except BaseException:
            metadata.child_sessions.insert(index, link)
            raise

    async def persist_empty_session(self) -> None:
        await self._save_messages(allow_empty=True)

    async def refresh_config(self) -> None:
        await self._config_orchestrator.reload()
        self._ensure_remote_registries()
        if self.mcp_registry is not None:
            self.mcp_registry.sync_active_servers(self.config.mcp_servers)

    def _drain_pending_injections(self) -> bool:
        if not self._pending_injected_messages:
            return False
        for injected in self._pending_injected_messages:
            self.messages.append(injected)
        self._pending_injected_messages.clear()
        return True

    def resolve_approval_request(
        self, request_id: str, response: ApprovalResponse, feedback: str | None = None
    ) -> None:
        self._request_broker.resolve_approval(request_id, response, feedback)

    def resolve_user_input_request(self, request_id: str, result: BaseModel) -> None:
        self._request_broker.resolve_user_input(request_id, result)

    def reject_request(self, request_id: str, error: BaseException) -> None:
        self._request_broker.reject(request_id, error)

    async def set_tool_permission(
        self, tool_name: str, permission: ToolPermission, save_permanently: bool = False
    ) -> None:
        if save_permanently:
            await self.config_orchestrator.set_field(
                f"/tools/{tool_name}/permission", permission.value
            )

        self._permission_store.set_tool_permission(tool_name, permission)

    async def approve_always(
        self,
        tool_name: str,
        required_permissions: list[RequiredPermission] | None,
        save_permanently: bool = False,
    ) -> None:
        """Handle 'Allow Always' approval: add session rules or set tool-level permission."""
        if required_permissions:
            for rp in required_permissions:
                self._permission_store.add_rule(
                    ApprovedRule(
                        tool_name=tool_name,
                        scope=rp.scope,
                        session_pattern=rp.session_pattern,
                    )
                )
            if save_permanently and (
                update := self.config.build_tool_allowlist_update(
                    tool_name,
                    [rp.session_pattern for rp in required_permissions],
                    current_allowlist=self.tool_manager.get_tool_config(
                        tool_name
                    ).allowlist,
                )
            ):
                await self.config_orchestrator.set_field(
                    f"/tools/{tool_name}/allowlist",
                    update["tools"][tool_name]["allowlist"],
                )
        else:
            await self.set_tool_permission(
                tool_name, ToolPermission.ALWAYS, save_permanently=save_permanently
            )

    def start_initialize_experiments(
        self, *, defer_new_session_telemetry: bool = False
    ) -> None:
        if self._experiments_task is not None:
            return
        # When deferred (the --resume picker's throwaway session), hold the
        # new-session event: it is dropped on resume (see _reset_session_scoped_state)
        # and emitted only if the session is actually used (see act).
        self._pending_new_session_telemetry = not defer_new_session_telemetry
        self._deferred_new_session_telemetry = defer_new_session_telemetry
        self._ready_telemetry_pending = True
        self._experiments_task = asyncio.create_task(self.initialize_experiments())

    async def initialize_experiments(self) -> None:
        updated = await session_initialize_experiments(
            config=self.config,
            manager=self.experiment_manager,
            session_logger=self.session_logger,
            launch_context=self.launch_context,
            resolve_identity=self.identity_cache.resolve,
            resolve_whoami=self.whoami_cache.resolve,
        )
        if updated and self._await_experiment_model:
            with contextlib.suppress(Exception):
                self._sync_growthbook_layer_variants()
                await self.refresh_config()
                await self.refresh_system_prompt()

    async def hydrate_experiments_from_session(
        self, *, refresh_prompt: bool = True
    ) -> None:
        hydrated = await session_hydrate_experiments_from_session(
            config=self.config,
            manager=self.experiment_manager,
            session_logger=self.session_logger,
        )
        if hydrated:
            with contextlib.suppress(Exception):
                self._sync_growthbook_layer_variants()
                await self.refresh_config()
                if refresh_prompt:
                    await self.refresh_system_prompt()

    def emit_new_session_telemetry(self) -> None:
        # Any direct emit (e.g. /new, /clear via _reset_session) consumes a pending
        # deferred event so act() cannot re-emit it.
        self._deferred_new_session_telemetry = False
        has_agents_md = has_agents_md_file(self.cwd)
        nb_skills = len(self.skill_manager.available_skills)
        nb_mcp_servers = len(self.config.mcp_servers)
        nb_models = len(self.config.models)

        self.telemetry_client.send_new_session(
            has_agents_md=has_agents_md,
            nb_skills=nb_skills,
            nb_mcp_servers=nb_mcp_servers,
            nb_models=nb_models,
        )

    def emit_ready_telemetry(self, init_duration_ms: int) -> None:
        self.telemetry_client.send_ready(init_duration_ms=init_duration_ms)

    @property
    def init_duration_ms(self) -> int | None:
        return self._last_init_duration_ms

    def emit_session_closed_telemetry(self) -> None:
        self.telemetry_client.send_session_closed()

    async def aclose(self) -> None:
        if (task := self._experiments_task) is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        if self._mcp_pool is not None:
            with contextlib.suppress(Exception):
                await self._mcp_pool.aclose()
        with contextlib.suppress(Exception):
            await self.backend.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            await self.experiment_manager.aclose()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.tool_manager.terminal_runtime.close)
        cleanup_scratchpad(self.scratchpad_dir)
        lease = self._session_lease
        self._session_lease = None
        if lease is not None:
            await asyncio.to_thread(lease.release)
        self.harness.close()

    def _create_connector_registry(self) -> ConnectorRegistry | None:
        # Runs during __init__ before agent_manager exists, so read the
        # orchestrator config directly. Connector fields are profile-independent.
        config = self._config_orchestrator.config
        if not config.enable_connectors:
            return None

        provider = config.get_mistral_provider()
        if provider is None:
            return None

        api_key_env = provider.api_key_env_var or "MISTRAL_API_KEY"
        api_key = resolve_api_key(api_key_env) or ""
        if not api_key:
            return None

        server_url = get_server_url_from_api_base(provider.api_base)
        from vibe.core.tools.connectors.connector_registry import ConnectorRegistry

        return ConnectorRegistry(api_key=api_key, server_url=server_url)

    @staticmethod
    def _create_mcp_registry() -> MCPRegistry:
        from vibe.core.tools.mcp.registry import MCPRegistry

        return MCPRegistry()

    @staticmethod
    def _create_mcp_pool() -> MCPConnectionPool:
        from vibe.core.tools.mcp.pool import MCPConnectionPool

        return MCPConnectionPool()

    def _ensure_remote_registries(self) -> None:
        if self.mcp_registry is None and self.config.mcp_servers:
            self.mcp_registry = self._create_mcp_registry()
            self.tool_manager.set_mcp_registry(self.mcp_registry)

        if self._mcp_pool is None and self.config.mcp_servers:
            self._mcp_pool = self._create_mcp_pool()

        if self.connector_registry is None:
            self.connector_registry = self._create_connector_registry()
            self.tool_manager.set_connector_registry(self.connector_registry)

    @staticmethod
    def _create_sampling_handler(
        *,
        backend_getter: Callable[[], BackendLike],
        config_getter: Callable[[], VibeConfigSchema],
        metadata_getter: Callable[[], dict[str, Any]],
        extra_headers_getter: Callable[[], dict[str, str]],
    ) -> MCPSamplingHandler:
        handler: MCPSamplingHandler | None = None

        async def lazy_handler(context: Any, params: Any) -> Any:
            nonlocal handler
            if handler is None:
                from vibe.core.tools.mcp_sampling import MCPSamplingHandler

                handler = MCPSamplingHandler(
                    backend_getter=backend_getter,
                    config_getter=config_getter,
                    metadata_getter=metadata_getter,
                    extra_headers_getter=extra_headers_getter,
                )
            return await handler(context, params)

        # Only ever invoked as a callable, never attribute-accessed.
        return cast("MCPSamplingHandler", lazy_handler)

    def _render_system_prompt(
        self,
        skill_manager: SkillManager,
        config: VibeConfigSchema | None = None,
        tool_manager: ToolManager | None = None,
    ) -> str:
        return get_universal_system_prompt(
            config or self.config,
            skill_manager,
            self.agent_manager,
            scratchpad_dir=self.scratchpad_dir,
            headless=self._headless,
            cwd=self.cwd,
            harness_files=self.harness_files,
            tool_manager=tool_manager or self.tool_manager,
        )

    def _build_system_prompt(self) -> str:
        return self._render_system_prompt(self.skill_manager)

    @requires_init
    async def refresh_system_prompt(self) -> None:
        """Rebuild and replace the system prompt with current tool/skill state."""
        self.messages.update_system_prompt(self._build_system_prompt())

    @property
    def _turn(self) -> _ActiveTurn:
        return self._active_turn or _NO_TURN

    async def notice_retry(self, reason: RetryReason) -> None:
        if (sink := self._turn.retry_sink) is not None:
            await sink(reason)

    def backend_factory(self, config: VibeConfigSchema | None = None) -> BackendLike:
        return self._injected_backend or self._select_backend(config)

    def _select_backend(self, config: VibeConfigSchema | None = None) -> BackendLike:
        config = config or self.config
        provider = config.get_active_provider()
        return self._create_backend(provider, config)

    def _create_backend(
        self, provider: ProviderConfig, config: VibeConfigSchema | None = None
    ) -> BackendLike:
        config = config or self.config
        fallback = config.get_fallback_model()
        return create_backend(
            provider=provider,
            on_retry=self.notice_retry,
            timeout=config.api_timeout,
            retry_max_elapsed_time=config.api_retry_max_elapsed_time,
            enable_otel=(
                config.enable_telemetry
                and config.enable_otel
                and build_otel_span_exporter_config(
                    config.otel_endpoint, config.get_mistral_provider()
                )
                is not None
            ),
            fail_fast_on_quota_exhaustion=(
                fallback is not None and provider.name != fallback.provider
            ),
        )

    def _backend_for_model(self, model: ModelConfig) -> BackendLike:
        if self._injected_backend is not None:
            return self._injected_backend
        provider = self.config.get_provider_for_model(model)
        return self._create_backend(provider)

    @contextlib.asynccontextmanager
    async def _backend_for_call(self, model: ModelConfig) -> AsyncIterator[BackendLike]:
        active_model = self.config.get_active_model()
        if self._injected_backend is not None or model.alias == active_model.alias:
            yield self.backend
            return

        backend = self._backend_for_model(model)
        try:
            yield backend
        finally:
            with contextlib.suppress(Exception):
                await backend.__aexit__(None, None, None)

    def _available_fallback_for(self, preferred: ModelConfig) -> ModelConfig | None:
        preferred_provider = self.config.get_provider_for_model(preferred)
        if preferred_provider.backend is not Backend.MISTRAL:
            return None
        fallback = self.config.get_fallback_model()
        if fallback is None or fallback.alias == preferred.alias:
            return None
        fallback_provider = self.config.get_provider_for_model(fallback)
        if fallback_provider.api_key_env_var and not resolve_api_key(
            fallback_provider.api_key_env_var
        ):
            return None
        return fallback

    def _failover_key(self, model: ModelConfig) -> str:
        provider = self.config.get_provider_for_model(model)
        return f"{provider.name}:{provider.api_key_env_var}"

    async def _activate_model_fallback(
        self, preferred: ModelConfig, *, reason: str
    ) -> ModelConfig | None:
        fallback = self._available_fallback_for(preferred)
        if fallback is None:
            return None
        first_activation = await self._model_failover_state.activate(
            self._failover_key(preferred)
        )
        self.stats.update_pricing(
            fallback.input_price, fallback.output_price, fallback.cached_input_price
        )
        if first_activation:
            logger.warning(
                "Model fallback activated primary=%s fallback=%s reason=%s",
                preferred.alias,
                fallback.alias,
                reason,
            )
            await self.notice_retry(
                RetryReason(
                    RetryCategory.MODEL_FALLBACK,
                    f"model fallback {preferred.alias} -> {fallback.alias}: {reason}",
                )
            )
        return fallback

    async def _preflight_model(self, preferred: ModelConfig) -> ModelConfig:
        fallback = self._available_fallback_for(preferred)
        if fallback is None:
            return preferred
        provider = self.config.get_provider_for_model(preferred)
        missing_key = bool(
            provider.api_key_env_var and not resolve_api_key(provider.api_key_env_var)
        )
        if (
            not self._model_failover_state.active(self._failover_key(preferred))
            and not missing_key
        ):
            return preferred
        return (
            await self._activate_model_fallback(
                preferred, reason="primary credentials unavailable"
            )
            or preferred
        )

    async def _fallback_after_error(
        self, preferred: ModelConfig, error: Exception
    ) -> ModelConfig | None:
        backend_error = _extract_backend_error(error)
        if backend_error is None or not backend_error.is_failover_eligible:
            return None
        return await self._activate_model_fallback(
            preferred, reason=f"HTTP {backend_error.status or 'N/A'}"
        )

    async def _save_messages(self, *, allow_empty: bool = False) -> None:
        await self.session_logger.save_interaction(
            self.messages,
            self.stats,
            self.config,
            self.tool_manager,
            self.agent_profile,
            allow_empty=allow_empty,
        )

    @requires_init
    async def inject_user_context(
        self,
        content: str,
        *,
        as_message: bool = False,
        inject_implicit: bool = False,
        images: list[ImageAttachment] | None = None,
        input_text: str | None = None,
        resources: list[UserResource] | None = None,
        client_message_id: str | None = None,
        manual_shell: ManualShellContext | None = None,
    ) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        if as_message:
            message = LLMMessage(
                role=Role.user,
                content=content,
                message_id=client_message_id or str(uuid4()),
                images=images or None,
                input_text=input_text,
                resources=resources or None,
                manual_shell=manual_shell,
            )
            self.messages.append(message)
            if message.message_id is None:
                raise AgentLoopError("User message must have a message_id")
            events.append(
                UserMessageEvent(
                    content=input_text if input_text is not None else content,
                    message_id=message.message_id,
                    images=list(message.images or []),
                    resources=list(message.resources or []),
                )
            )
            if inject_implicit:
                async for event in self._inject_invoked_skill(content):
                    events.append(event)
                async for event in self._inject_mentioned_files(content):
                    events.append(event)
        else:
            self.messages.append(
                LLMMessage(
                    role=Role.user,
                    content=content,
                    injected=True,
                    images=images or None,
                    input_text=input_text,
                    resources=resources or None,
                    manual_shell=manual_shell,
                )
            )
        await self._save_messages()
        return events

    @requires_init
    async def act(
        self,
        msg: str,
        client_message_id: str | None = None,
        *,
        auto_title: str | None = None,
        images: list[ImageAttachment] | None = None,
        user_display_content: UserDisplayContent | None = None,
        input_text: str | None = None,
        resources: list[UserResource] | None = None,
        subagent_runner: SubagentRunnerPort | None = None,
        tool_io: ToolIOPort | None = None,
        turn_options: AgentTurnOptions | None = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        self._emit_deferred_new_session_telemetry()
        try:
            active_model = self.config.get_active_model()
            model_name = active_model.name
        except ValueError:
            active_model = None
            model_name = None
        if images and active_model is not None and not active_model.supports_images:
            raise ImagesNotSupportedError(
                active_model.display_name or active_model.alias
            )
        if self._active_turn is not None:
            raise AgentLoopStateError("A turn is already active")
        options = turn_options or AgentTurnOptions()
        self._active_turn = _ActiveTurn(
            subagent_runner=subagent_runner,
            tool_io=tool_io,
            retry_sink=options.retry_sink,
        )
        try:
            self._clean_message_history()
            self.checkpoint_recorder.create_checkpoint()
            try:
                async with agent_span(model=model_name, session_id=self.session_id):
                    async for event in self._conversation_loop(
                        msg,
                        client_message_id=client_message_id,
                        auto_title=auto_title,
                        images=images,
                        user_display_content=user_display_content,
                        input_text=input_text,
                        resources=resources,
                        injected=options.injected,
                    ):
                        yield event
            finally:
                self.checkpoint_recorder.seal_turn()
        finally:
            self._active_turn = None

    @property
    def teleport_service(self) -> TeleportService:
        if not _TELEPORT_AVAILABLE:
            raise TeleportError(
                "Teleport requires git to be installed. "
                "Please install git and try again."
            )

        if self._teleport_service is None:
            self._teleport_service = _load_teleport_service()(
                session_logger=self.session_logger,
                vibe_code_sessions_base_url=self.config.vibe_code_sessions_base_url,
                vibe_code_api_key=self.config.vibe_code_api_key,
                vibe_config=self.config,
                workdir=self.cwd,
            )
        return self._teleport_service

    @requires_init
    async def teleport_to_vibe_code(
        self,
        prompt: str | None,
        *,
        project_id: str | None = None,
        project_picker: ProjectPickerTelemetryPayload | None = None,
    ) -> AsyncGenerator[TeleportYieldEvent, TeleportPushResponseEvent | None]:
        nb_session_messages = max(len(self.messages) - 1, 0)
        resolved_prompt = self._resolve_teleport_prompt(prompt)
        telemetry_tracker = TeleportTelemetryTracker(
            telemetry_client=self.telemetry_client,
            nb_session_messages=nb_session_messages,
            stage="no_history" if not resolved_prompt else "git_check",
            project_picker=project_picker,
        )
        try:
            teleport_message_context: TeleportMessageContext | None = None
            if resolved_prompt and self._should_summarize_teleport_context(prompt):
                summary_event = TeleportSummarizingContextEvent()
                telemetry_tracker.record_event(summary_event)
                yield summary_event
                try:
                    message_context = await self._summarize_teleport_context(
                        prompt=prompt, resolved_prompt=resolved_prompt
                    )
                except ServiceTeleportError:
                    telemetry_tracker.record_context_summary_failed()
                    raise
                except Exception as e:
                    telemetry_tracker.record_context_summary_failed()
                    raise ServiceTeleportError(
                        "Failed to summarize context for teleport.",
                        telemetry_details={"failure_kind": "context_summary_failed"},
                    ) from e

                teleport_message_context = self._build_teleport_message_context(
                    message_context, telemetry_tracker
                )
            async with self.teleport_service:
                gen = self.teleport_service.execute(
                    prompt=resolved_prompt,
                    project_id=project_id,
                    message_context=teleport_message_context,
                    conversation_id=self.session_id,
                )
                response: TeleportPushResponseEvent | None = None
                while True:
                    try:
                        event = await gen.asend(response)
                        telemetry_tracker.record_event(event)
                        if isinstance(event, TeleportCompleteEvent):
                            telemetry_tracker.send_success()
                        response = yield event
                    except StopAsyncIteration:
                        break
        except ServiceTeleportError as e:
            telemetry_tracker.record_service_error(e)
            raise TeleportError(str(e)) from e
        except (asyncio.CancelledError, GeneratorExit):
            telemetry_tracker.record_cancelled()
            raise
        except Exception as e:
            telemetry_tracker.record_unexpected_error(e)
            raise
        finally:
            telemetry_tracker.send_failure_if_needed()
            self._teleport_service = None

    def _resolve_teleport_prompt(self, prompt: str | None) -> str:
        if prompt:
            return prompt

        last = self._last_user_message()
        content = last.content if last else None
        return content if isinstance(content, str) and content else ""

    def _build_teleport_message_context(
        self, summary: str, telemetry_tracker: TeleportTelemetryTracker
    ) -> TeleportMessageContext | None:
        try:
            message_context = TeleportMessageContext(
                summary=summary, source=self._teleport_message_context_source()
            )
        except ValidationError:
            telemetry_tracker.record_context_summary_failed()
            return None

        telemetry_tracker.record_context_summary_generated(summary)
        return message_context

    def _should_summarize_teleport_context(self, prompt: str | None) -> bool:
        return any(
            self._is_teleport_context_message(message)
            for message in self._teleport_context_messages(prompt)
        )

    @staticmethod
    def _is_teleport_context_message(message: LLMMessage) -> bool:
        if message.role == Role.system:
            return False
        return bool(
            message.content
            or message.reasoning_content
            or message.tool_calls
            or message.tool_call_id
            or message.images
        )

    def _teleport_context_messages(self, prompt: str | None) -> list[LLMMessage]:
        messages = self._current_model_context()
        excluded = None if prompt else self._last_user_message_from(messages)
        return [message for message in messages if message is not excluded]

    async def _summarize_teleport_context(
        self, *, prompt: str | None, resolved_prompt: str
    ) -> str:
        source_messages = [
            message.model_copy(deep=True)
            for message in self._teleport_context_messages(prompt)
        ]
        summary_request = render_teleport_summary_request(
            self.config.compaction_prompt,
            resolved_prompt,
            max_summary_chars=TELEPORT_MESSAGE_CONTEXT_MAX_LENGTH,
        )
        summary_messages = [
            *source_messages,
            LLMMessage(role=Role.user, content=summary_request),
        ]
        self.stats.steps += 1
        compaction_model = self.config.get_compaction_model()
        start_time = time.perf_counter()
        summary_result = await self._complete(
            model=compaction_model,
            messages=summary_messages,
            tools=[],
            tool_choice=None,
            call_type="secondary_call",
        )
        _usage = summary_result.usage
        log_model_call_success(
            compaction_model.alias,
            int((time.perf_counter() - start_time) * 1000),
            prompt_tokens=_usage.prompt_tokens if _usage else 0,
            completion_tokens=_usage.completion_tokens if _usage else 0,
            cached_tokens=_usage.cached_tokens if _usage else 0,
        )
        raw_content = (summary_result.message.content or "").strip()
        if summary_result.message.tool_calls or not raw_content:
            raise ServiceTeleportError(
                "Failed to summarize context for teleport.",
                telemetry_details={"failure_kind": "context_summary_failed"},
            )
        return extract_summary(raw_content) or raw_content

    def _teleport_message_context_source(self) -> TeleportMessageContextSource:
        if self.launch_context is None:
            return TeleportMessageContextSource()
        return TeleportMessageContextSource(
            entrypoint=self.launch_context.agent_entrypoint,
            client_name=self.launch_context.client_name,
        )

    def _last_user_message(self) -> LLMMessage | None:
        return AgentLoop._last_user_message_from(select_model_context(self.messages))

    def _current_model_context(self) -> list[LLMMessage]:
        return select_model_context(self.messages)

    @staticmethod
    def _last_user_message_from(messages: Sequence[LLMMessage]) -> LLMMessage | None:
        return next(
            (m for m in reversed(messages) if m.role == Role.user and not m.injected),
            None,
        )

    def set_max_turns(self, max_turns: int) -> None:
        self._max_turns = max_turns
        self._setup_middleware()

    def set_max_tokens(self, max_tokens: int) -> None:
        self._max_tokens = max_tokens

    def _setup_middleware(self) -> None:
        """Configure middleware pipeline for this conversation."""
        self.middleware_pipeline.clear()

        if self._max_turns is not None:
            self.middleware_pipeline.add(TurnLimitMiddleware(self._max_turns))

        if self._max_price is not None:
            self.middleware_pipeline.add(PriceLimitMiddleware(self._max_price))

        if self._max_session_tokens is not None:
            self.middleware_pipeline.add(TokenLimitMiddleware(self._max_session_tokens))

        self.middleware_pipeline.add(
            ExecutionBudgetMiddleware(lambda: self._execution_budget)
        )
        self.middleware_pipeline.add(AutoCompactMiddleware())
        if self.config.autonomy.enabled:
            self.middleware_pipeline.add(
                AutonomyRefreshMiddleware(self.config.autonomy.context_refresh_turns)
            )
        if self.config.context_warnings:
            self.middleware_pipeline.add(ContextWarningMiddleware(0.5))

        self.middleware_pipeline.add(
            ReadOnlyAgentMiddleware(
                lambda: self.agent_profile,
                BuiltinAgentName.PLAN,
                lambda: make_plan_agent_reminder(
                    self._plan_session.plan_file_path_str,
                    has_ask_user_question="ask_user_question"
                    in self.tool_manager.available_tools,
                    has_exit_plan_mode="exit_plan_mode"
                    in self.tool_manager.available_tools,
                ),
                PLAN_AGENT_EXIT,
            )
        )

    async def _handle_middleware_result(
        self, result: MiddlewareResult
    ) -> AsyncGenerator[BaseEvent]:
        match result.action:
            case MiddlewareAction.STOP:
                yield AssistantEvent(
                    content=f"<{VIBE_STOP_EVENT_TAG}>{result.reason}</{VIBE_STOP_EVENT_TAG}>",
                    stopped_by_middleware=True,
                )

            case MiddlewareAction.INJECT_MESSAGE:
                if result.message:
                    injected_message = LLMMessage(
                        role=Role.user, content=result.message, injected=True
                    )
                    self.messages.append(injected_message)

            case MiddlewareAction.COMPACT:
                async for event in self._run_compaction():
                    yield event

            case MiddlewareAction.CONTINUE:
                pass

    async def _run_compaction(self) -> AsyncGenerator[BaseEvent]:
        # Auto/reactive compaction: emit boundary events, compact, report status.
        old_tokens = self.stats.context_tokens
        threshold = self.config.get_active_model().auto_compact_threshold
        old_session_id = self.session_id
        old_parent_session_id = self.parent_session_id
        tool_call_id = str(uuid4())

        compaction_start = await self._run_harness_phase(
            HarnessPhase.COMPACTION,
            self._autonomy_objective or "compact session context",
            metadata={"state": "start", "context_tokens": old_tokens},
        )
        if compaction_start.stop_reason is not None:
            raise RuntimeError(compaction_start.stop_reason)

        yield CompactStartEvent(
            tool_call_id=tool_call_id,
            current_context_tokens=old_tokens,
            threshold=threshold,
        )

        compact_status: Literal["success", "failure", "cancelled"] = "success"
        try:
            summary = await self.compact()
        except asyncio.CancelledError:
            compact_status = "cancelled"
            raise
        except Exception:
            compact_status = "failure"
            raise
        finally:
            self.telemetry_client.send_auto_compact_triggered(
                nb_context_tokens_before=old_tokens,
                auto_compact_threshold=threshold,
                status=compact_status,
                session_id=old_session_id,
                parent_session_id=old_parent_session_id,
            )

        yield CompactEndEvent(
            tool_call_id=tool_call_id,
            summary_length=len(summary),
            old_session_id=old_session_id,
            new_session_id=self.session_id,
        )
        compaction_end = await self._run_harness_phase(
            HarnessPhase.COMPACTION,
            self._autonomy_objective or "compact session context",
            outcome="success",
            metadata={"state": "end", "summary_length": len(summary)},
        )
        if compaction_end.stop_reason is not None:
            raise RuntimeError(compaction_end.stop_reason)

    @property
    def user_plan(self) -> str | None:
        return self._user_plan

    def set_user_plan(self, user_plan: str | None) -> None:
        self._user_plan = user_plan

    def _should_self_heal(self) -> bool:
        # Recover from an overflow at most once per turn; strict mode surfaces it.
        return (
            not self._reactive_recovery_used
            and not self.config.raise_on_compaction_failure
        )

    def _get_context(self) -> ConversationContext:
        return ConversationContext(
            messages=self.messages, stats=self.stats, config=self.config
        )

    def _new_autonomy_coordinator(self) -> AutonomyCoordinator | None:
        if self._is_subagent or not self.config.autonomy.enabled:
            return None
        autonomy = self.config.autonomy
        review_retries = autonomy.effective_review_retries
        if self._execution_profile is not None:
            review_retries = min(
                review_retries, self._execution_profile.max_review_retries
            )
        coordinator = AutonomyCoordinator(
            AutonomyPolicy(
                max_retries=review_retries,
                require_worker=autonomy.require_worker or autonomy.boost_mode,
                require_review=autonomy.require_review or autonomy.boost_mode,
            )
        )
        coordinator.start_turn()
        return coordinator

    async def _prepare_autonomy_turn(
        self, *, injected: bool, objective: str
    ) -> AutonomyCoordinator | None:
        self._reactive_recovery_used = False
        self._autonomy_bootstrap_blocked = False
        if injected and self._autonomy_coordinator is not None:
            return self._autonomy_coordinator
        self._autonomy_evidence = []
        if self._is_subagent or not self.config.autonomy.enabled:
            self._autonomy_coordinator = None
            return None
        if self.config.autonomy.gauntlet_loop and not self.config.autonomy.boost_mode:
            self._autonomy_coordinator = self._new_autonomy_coordinator()
            return self._autonomy_coordinator
        intent = await self._analyze_autonomy_intent(objective)
        locally_substantive = requires_autonomous_pipeline(objective)
        if intent is AutonomyIntent.DIRECT and not locally_substantive:
            self._autonomy_coordinator = None
            return None
        if (
            self._execution_profile is not None
            and self._execution_profile.max_parallel_subagents == 0
        ):
            self._execution_profile = execution_profile(TaskScale.MEDIUM)
            self._execution_budget = ExecutionBudgetTracker(
                self._execution_profile,
                initial_tokens=self.stats.session_total_llm_tokens,
            )
        self._autonomy_coordinator = self._new_autonomy_coordinator()
        return self._autonomy_coordinator

    async def _analyze_autonomy_intent(self, objective: str) -> AutonomyIntent:
        """Let the active model decide whether advisor orchestration is useful."""
        intent_prompt = UtilityPrompt.AUTONOMY_INTENT.read()
        if self.config.autonomy.boost_mode:
            intent_prompt += (
                "\nBOOST is enabled. Classify every implementation, investigation, "
                "research, browser/computer action, or multi-step request as "
                "AUTONOMOUS. Reserve DIRECT only for genuinely trivial interactions."
            )
        messages = [
            LLMMessage(role=Role.system, content=intent_prompt),
            LLMMessage(role=Role.user, content=objective),
        ]
        try:
            result = await self._complete(
                model=self.config.get_active_model(),
                messages=messages,
                tools=[],
                tool_choice=None,
                call_type="secondary_call",
                max_tokens=16,
            )
        except Exception:
            logger.warning(
                "Autonomy intent analysis failed; continuing without advisor",
                exc_info=True,
            )
            return AutonomyIntent.DIRECT
        intent = parse_autonomy_intent(result.message.content or "")
        if intent is None:
            logger.warning(
                "Invalid autonomy intent response %r; continuing without advisor",
                result.message.content,
            )
            return AutonomyIntent.DIRECT
        return intent

    def _vibe_deliberation_context(
        self, objective: str, *, thinking: ThinkingLevel
    ) -> str:
        max_chars = _VIBE_DELIBERATION_CONTEXT_MAX_CHARS[thinking]
        memory_max_chars = max_chars // _VIBE_DELIBERATION_MEMORY_SHARE
        working_memory = next(
            (
                _compact_autonomy_text(
                    message.content or "", memory_max_chars, "older fast-memory entries"
                )
                for message in reversed(self.messages)
                if is_working_memory_message(message)
            ),
            "",
        )
        global_memory = next(
            (
                _compact_autonomy_text(
                    tagged, memory_max_chars, "older global-memory entries"
                )
                for message in reversed(self.messages)
                if message.role is Role.system
                and (
                    tagged := _extract_tagged_text(
                        message.content or "", "global_memory"
                    )
                )
            ),
            "",
        )
        recent_messages: list[str] = []
        used = len(working_memory) + len(global_memory)
        for message in reversed(self.messages):
            content = message.content or ""
            if (
                message.role is Role.system
                or not content
                or "<vibe_deliberation>" in content
            ):
                continue
            bounded = _compact_autonomy_text(
                content,
                min(_VIBE_DELIBERATION_MESSAGE_MAX_CHARS, max_chars),
                f"older {message.role.value} content",
            )
            rendered = f"{message.role.value}: {bounded}"
            if recent_messages and used + len(rendered) > max_chars:
                break
            recent_messages.append(rendered)
            used += len(rendered)
        recent_messages.reverse()
        sections = []
        if working_memory:
            sections.append(
                "FAST WORKING MEMORY (status/digests only; use it to avoid repeated "
                f"actions):\n{working_memory}"
            )
        if global_memory:
            sections.append(
                "SLOW GLOBAL MEMORY (potentially stale; verify before use):\n"
                f"{global_memory}"
            )
        if recent_messages:
            sections.append("RECENT EVIDENCE:\n" + "\n\n".join(recent_messages))
        context = "\n\n".join(sections)
        if not context:
            context = f"user: {objective}"
        return _compact_autonomy_text(context, max_chars, "deliberation evidence")

    def _clear_vibe_deliberation(self) -> None:
        if self._active_vibe_deliberation_message is None:
            return
        self.messages.discard(self._active_vibe_deliberation_message)
        self._active_vibe_deliberation_message = None

    async def _synthesize_vibe_candidates(
        self,
        objective: str,
        bounded_context: str,
        candidates: list[tuple[str, str]],
        thinking: ThinkingLevel,
    ) -> str:
        candidate_text = "\n\n".join(
            f'<candidate lens="{lens}">\n{candidate}\n</candidate>'
            for lens, candidate in candidates
        )
        synthesis_prompt = (
            "Original user objective:\n"
            f"{objective}\n\n"
            "Bounded evidence and fast-memory ledger:\n"
            f"{bounded_context}\n\n"
            "Independent candidate briefs:\n"
            f"{candidate_text}"
        )
        synthesis_model = self.config.get_compaction_model().model_copy(
            update={"thinking": "off"}
        )
        try:
            synthesis = await self._complete(
                model=synthesis_model,
                messages=[
                    LLMMessage(
                        role=Role.system,
                        content=UtilityPrompt.VIBE_DELIBERATION_SYNTHESIS.read(),
                    ),
                    LLMMessage(role=Role.user, content=synthesis_prompt),
                ],
                tools=[],
                tool_choice=None,
                call_type="secondary_call",
                max_tokens=_VIBE_DELIBERATION_SYNTHESIS_MAX_TOKENS[thinking],
            )
        except Exception:
            logger.warning("Vibe deliberation synthesis failed", exc_info=True)
            return candidates[-1][1]
        return (synthesis.message.content or "").strip() or candidates[-1][1]

    async def _run_full_vibe_deliberation(
        self, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        passes = self.config.autonomy.vibe_thinking_passes
        if (
            self._is_subagent
            or passes == 0
            or (
                self.config.autonomy.boost_mode
                and self._is_trivial_direct_request(objective)
            )
        ):
            return

        self._vibe_turns_since_full = 0
        self._clear_vibe_deliberation()
        thinking = self.config.autonomy.vibe_thinking
        bounded_context = self._vibe_deliberation_context(objective, thinking=thinking)
        deliberation_model = self.config.get_active_model().model_copy(
            update={"thinking": thinking}
        )
        reasoning_message_id = f"vibe-thinking-{uuid4()}"
        lenses = _VIBE_DELIBERATION_LENSES[passes]
        candidates: list[tuple[str, str]] = []
        for pass_index, lens in enumerate(lenses, start=1):
            yield ReasoningEvent(
                content=f"Vibe thinking: {lens} {pass_index}/{passes}\n",
                message_id=reasoning_message_id,
                status_text=f"Vibe thinking {pass_index}/{passes} · {lens}",
            )
            prompt = (
                f"Independent reflection candidate {pass_index}/{passes}: {lens}.\n"
                f"Task for this stage: {_VIBE_DELIBERATION_TASKS[lens]}\n"
                "Do not assume another candidate's approach. Derive this candidate "
                "independently from the objective and supplied evidence.\n"
                "Original user objective (never replace it with the current plan):\n"
                f"{objective}\n\n"
                f"Current conversation and evidence:\n{bounded_context}"
            )
            try:
                result = await self._complete(
                    model=deliberation_model,
                    messages=[
                        LLMMessage(
                            role=Role.system,
                            content=UtilityPrompt.VIBE_DELIBERATION.read(),
                        ),
                        LLMMessage(role=Role.user, content=prompt),
                    ],
                    tools=[],
                    tool_choice=None,
                    call_type="secondary_call",
                    max_tokens=_VIBE_DELIBERATION_MAX_TOKENS[thinking],
                )
            except Exception:
                logger.warning("Vibe deliberation failed", exc_info=True)
                yield ReasoningEvent(
                    content=(
                        f"Vibe thinking candidate {pass_index} unavailable; "
                        "continuing with the remaining candidates."
                    ),
                    message_id=reasoning_message_id,
                )
                continue
            candidate = (result.message.content or "").strip()
            if candidate:
                candidates.append((lens, candidate))
                summary, status = _visible_thinking_summary(
                    f"Vibe candidate {pass_index}/{passes}",
                    candidate,
                    (
                        ("REAL OUTCOME", "Goal"),
                        ("NEXT ACTION", "Candidate action"),
                        ("VERIFICATION", "Proof"),
                        ("PIVOT TRIGGER", "Pivot if"),
                    ),
                )
                yield ReasoningEvent(
                    content=summary, message_id=reasoning_message_id, status_text=status
                )

        if not candidates:
            return

        yield ReasoningEvent(
            content=(
                f"Vibe thinking: compacting {len(candidates)} independent "
                "candidate(s) into the execution brief\n"
            ),
            message_id=reasoning_message_id,
            status_text=f"Vibe thinking · synthesizing {len(candidates)} options",
        )
        brief = await self._synthesize_vibe_candidates(
            objective, bounded_context, candidates, thinking
        )
        summary, status = _visible_thinking_summary(
            "Vibe decision",
            brief,
            (
                ("DIRECTION", "Direction"),
                ("REAL OUTCOME", "Goal"),
                ("DECISION", "Chosen route"),
                ("PLAN", "Plan"),
                ("NEXT ACTION", "Next action"),
                ("COMPLETION GATE", "Completion proof"),
                ("PIVOT TRIGGER", "Pivot if"),
            ),
        )
        yield ReasoningEvent(
            content=summary, message_id=reasoning_message_id, status_text=status
        )

        deliberation_message = LLMMessage(
            role=Role.user,
            content=(
                "<vibe_deliberation>\n"
                "Use this private preflight brief for the next decision only. Verify "
                "its claims rather than treating them as evidence. Use web_search "
                "before asserting current, external, or uncertain facts when web "
                "search is enabled. If a material ambiguity cannot be resolved from "
                "available context, ask one focused clarifying question instead of "
                "guessing. Do not keep the current plan because of sunk cost: when "
                "the brief says PIVOT and available evidence supports it, change the "
                "plan before taking the next action. CONTINUE still requires the "
                "stated proof. For a substantive task, materialize or state the brief's "
                "plan before the first mutating tool call, execute it in dependency "
                "order, and do not claim completion until every completion gate has "
                f"observable evidence.\n\n{brief}\n"
                "</vibe_deliberation>"
            ),
            injected=True,
        )
        self.messages.append(deliberation_message)
        self._active_vibe_deliberation_message = deliberation_message

    def _latest_tool_failed(self) -> bool:
        latest = next(
            (
                message
                for message in reversed(self.messages)
                if not is_working_memory_message(message)
            ),
            None,
        )
        if latest is None or latest.role is not Role.tool:
            return False
        content = latest.content or ""
        return f"<{TOOL_ERROR_TAG}>" in content or "TASK_RESULT: FAIL" in content

    async def _run_fast_thinking(
        self, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        thinking = self.config.autonomy.vibe_thinking
        bounded_context = self._vibe_deliberation_context(objective, thinking=thinking)
        reasoning_message_id = f"fast-thinking-{uuid4()}"
        yield ReasoningEvent(
            content="Fast thinking: checking direction and missing proof\n",
            message_id=reasoning_message_id,
            status_text=(
                "Fast thinking · self-check "
                f"{self._vibe_turns_since_full}/{_VIBE_FULL_THINKING_INTERVAL}"
            ),
        )
        fast_model = self.config.get_active_model().model_copy(
            update={"thinking": "low"}
        )
        try:
            result = await self._complete(
                model=fast_model,
                messages=[
                    LLMMessage(
                        role=Role.system,
                        content=UtilityPrompt.VIBE_FAST_THINKING.read(),
                    ),
                    LLMMessage(
                        role=Role.user,
                        content=(
                            "Original objective:\n"
                            f"{objective}\n\nCurrent bounded evidence:\n{bounded_context}"
                        ),
                    ),
                ],
                tools=[],
                tool_choice=None,
                call_type="secondary_call",
                max_tokens=_VIBE_FAST_THINKING_MAX_TOKENS,
            )
        except Exception:
            logger.warning("Fast thinking failed", exc_info=True)
            return
        brief = (result.message.content or "").strip()
        if not brief:
            return
        summary, status = _visible_thinking_summary(
            "Fast check",
            brief,
            (
                ("DIRECTION", "Direction"),
                ("EVIDENCE", "Evidence"),
                ("GAP", "Missing proof"),
                ("NEXT", "Next action"),
            ),
        )
        yield ReasoningEvent(
            content=summary, message_id=reasoning_message_id, status_text=status
        )
        deliberation_message = LLMMessage(
            role=Role.user,
            content=(
                "<fast_thinking>\n"
                "Use this bounded self-check for the next action only. Verify its "
                "claims and pivot when its evidence supports PIVOT. Do not report "
                f"completion without observable proof.\n\n{brief}\n"
                "</fast_thinking>"
            ),
            injected=True,
        )
        self.messages.append(deliberation_message)
        self._active_vibe_deliberation_message = deliberation_message

    async def _run_vibe_deliberation(
        self, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        passes = self.config.autonomy.vibe_thinking_passes
        if self._is_subagent or passes == 0:
            return
        full_due = (
            self._vibe_turns_since_full >= _VIBE_FULL_THINKING_INTERVAL
            or self._latest_tool_failed()
        )
        runner = (
            self._run_full_vibe_deliberation if full_due else self._run_fast_thinking
        )
        async for event in runner(objective):
            yield event

    async def _prepare_turn_orchestration(
        self, objective: str, *, injected: bool
    ) -> AsyncGenerator[BaseEvent, None]:
        if not injected:
            self._vibe_turns_since_full = _VIBE_FULL_THINKING_INTERVAL
        async for event in self._refresh_personal_experience(objective):
            yield event
        async for event in self._run_vibe_deliberation(objective):
            yield event
        autonomy = await self._prepare_autonomy_turn(
            injected=injected, objective=objective
        )
        async for event in self._bootstrap_autonomy(
            autonomy, objective, injected=injected
        ):
            yield event

    async def _prepare_bounded_turn_orchestration(
        self, objective: str, *, injected: bool
    ) -> AsyncGenerator[BaseEvent, None]:
        async for event in self._prepare_turn_orchestration(
            objective, injected=injected
        ):
            yield event

    async def _perform_deliberated_llm_turn(
        self, objective: str, autonomy: AutonomyCoordinator | None
    ) -> AsyncGenerator[BaseEvent, None]:
        async for event in self._run_recovery_web_search(objective):
            yield event
        async for event in self._refresh_personal_experience(objective):
            yield event
        if self._active_vibe_deliberation_message is None:
            async for event in self._run_vibe_deliberation(objective):
                yield event
        try:
            async for event in self._perform_llm_turn():
                if autonomy is not None:
                    autonomy.observe(event)
                yield event
        finally:
            self._clear_vibe_deliberation()
            self._clear_personal_experience_context()

    def _build_backend_metadata(
        self, call_type: TelemetryCallType | None = None
    ) -> TelemetryRequestMetadata:
        return build_request_metadata(
            launch_context=self.launch_context,
            session_id=self.session_id,
            parent_session_id=self.parent_session_id,
            call_type=(
                call_type
                if call_type is not None
                else ("main_call" if self._is_user_prompt_call else "secondary_call")
            ),
            message_id=self._current_user_message_id,
            user_plan=self.user_plan,
        )

    def _get_extra_headers(
        self, provider: ProviderConfig | None = None
    ) -> dict[str, str]:
        provider = self.config.get_active_provider() if provider is None else provider
        headers: dict[str, str] = {**provider.extra_headers}
        headers["user-agent"] = get_user_agent(provider.backend)
        headers["x-affinity"] = self.session_id
        return headers

    async def _open_user_turn(
        self,
        user_msg: str,
        *,
        client_message_id: str | None = None,
        auto_title: str | None = None,
        images: list[ImageAttachment] | None = None,
        user_display_content: UserDisplayContent | None = None,
        input_text: str | None = None,
        resources: list[UserResource] | None = None,
        injected: bool = False,
    ) -> AsyncGenerator[BaseEvent]:
        if injected:
            self.messages.append(
                LLMMessage(
                    role=Role.user,
                    content=user_msg,
                    injected=True,
                    images=images or None,
                    user_display_content=user_display_content,
                    input_text=input_text,
                    resources=resources or None,
                )
            )
            last_user = self._last_user_message()
            self._current_user_message_id = (
                last_user.message_id if last_user is not None else None
            )
            return

        user_message = LLMMessage(
            role=Role.user,
            content=user_msg,
            message_id=client_message_id,
            images=images or None,
            user_display_content=user_display_content,
            input_text=input_text,
            resources=resources or None,
        )
        self.messages.append(user_message)
        self._automatic_web_queries.clear()
        self._experience_context_fresh = False
        self.stats.steps += 1
        self._current_user_message_id = user_message.message_id

        if user_message.message_id is None:
            raise AgentLoopError("User message must have a message_id")

        yield UserMessageEvent(
            content=input_text if input_text is not None else user_msg,
            message_id=user_message.message_id,
            images=list(user_message.images or []),
            user_display_content=user_message.user_display_content,
            resources=list(user_message.resources or []),
        )

        async for event in self._inject_invoked_skill(user_msg):
            yield event

        async for event in self._inject_mentioned_files(user_msg):
            yield event

        if auto_title is not None and self.session_logger.set_initial_auto_title(
            auto_title
        ):
            yield SessionTitleUpdatedEvent(title=auto_title)

        if self._hooks_manager:
            self._hooks_manager.reset_retry_count()

    def _clear_personal_experience_context(self) -> None:
        if self._active_experience_message is None:
            return
        self.messages.discard(self._active_experience_message)
        self._active_experience_message = None

    def _personal_experience_query(self, objective: str) -> str:
        recent: list[str] = []
        for message in reversed(self.messages):
            content = message.content or ""
            if (
                not content
                or message.role is not Role.user
                or message.injected
                or message is self._active_experience_message
                or "<personal_experience>" in content
            ):
                continue
            recent.append(content[:1_000])
            if len(recent) == _EXPERIENCE_QUERY_MESSAGE_LIMIT:
                break
        return "\n".join([objective, *reversed(recent)])

    async def _refresh_personal_experience(
        self, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        if self._is_subagent or self._experience_context_fresh:
            return
        self._clear_personal_experience_context()
        self._experience_context_fresh = True
        if not self.config.autonomy.personal_experience:
            return
        project_key = project_experience_key(self.cwd)
        try:
            entries = await asyncio.to_thread(
                self._experience_store.search,
                self._personal_experience_query(objective),
                project_key=project_key,
            )
        except ExperienceStoreError as exc:
            if not self._experience_error_reported:
                self._experience_error_reported = True
                yield MemoryStatusEvent(
                    status="error",
                    message=f"Personal experience could not be searched: {exc}",
                )
            return
        rendered = render_experience_context(entries)
        if rendered:
            message = LLMMessage(role=Role.system, content=rendered, injected=True)
            self.messages.append(message)
            self._active_experience_message = message
        fingerprints = frozenset(entry.fingerprint for entry in entries)
        selection_changed = fingerprints != self._reported_experience_fingerprints
        self._reported_experience_fingerprints = fingerprints
        if entries and selection_changed:
            successes = sum(entry.status == "success" for entry in entries)
            failures = sum(entry.status == "failure" for entry in entries)
            tools = ", ".join(dict.fromkeys(entry.tool for entry in entries))
            yield MemoryStatusEvent(
                status="experience_loaded",
                message=(
                    f"Experience applied: {len(entries)} relevant observations "
                    f"({successes} successful, {failures} failed) · {tools}."
                ),
                persistent_entries=len(entries),
            )

    async def _persist_personal_experience(
        self,
        records: Sequence[
            tuple[str, str, Literal["success", "failure", "skipped"], str]
        ],
    ) -> AsyncGenerator[BaseEvent, None]:
        self._experience_context_fresh = False
        if (
            self._is_subagent
            or not self.config.autonomy.personal_experience
            or not records
        ):
            return
        try:
            result = await asyncio.to_thread(
                self._experience_store.record_many,
                records,
                project_key=project_experience_key(self.cwd),
            )
        except ExperienceStoreError as exc:
            yield MemoryStatusEvent(
                status="error",
                message=f"Personal experience could not be updated: {exc}",
            )
            return
        if result.learned == 0:
            return
        changes = []
        if result.inserted:
            changes.append(f"{result.inserted} new")
        if result.changed:
            changes.append(f"{result.changed} changed")
        highlight = f" · {'; '.join(result.highlights)}" if result.highlights else ""
        yield MemoryStatusEvent(
            status="experience_saved",
            message=(
                f"Experience learned: {', '.join(changes)} verified "
                f"observation{'s' if result.learned != 1 else ''}{highlight}."
            ),
            persistent_entries=result.learned,
        )

    async def _report_global_memory_status(
        self, *, injected: bool
    ) -> AsyncGenerator[BaseEvent, None]:
        if injected or self._is_subagent or self._global_memory_status_reported:
            return
        self._global_memory_status_reported = True
        working_memory = load_working_memory(self.messages)
        fast_entries = len(working_memory.entries)
        if "user" not in self.harness_files.sources:
            if fast_entries:
                yield MemoryStatusEvent(
                    status="restored",
                    message=(
                        f"Fast working memory restored: {fast_entries} recent tool "
                        "results; global memory is disabled."
                    ),
                    fast_entries=fast_entries,
                )
            else:
                yield MemoryStatusEvent(
                    status="skipped",
                    message="Global memory skipped: user-level context is disabled.",
                )
            return
        try:
            document = await asyncio.to_thread(GlobalMemoryStore().load)
        except MemoryStoreError as exc:
            yield MemoryStatusEvent(
                status="error",
                message=f"Global memory could not be loaded: {exc}",
                fast_entries=fast_entries,
            )
            return

        if not document.entries:
            if fast_entries:
                yield MemoryStatusEvent(
                    status="restored",
                    message=(
                        f"Fast working memory restored: {fast_entries} recent tool "
                        "results will be checked before new actions."
                    ),
                    fast_entries=fast_entries,
                )
            return

        memory_was_injected = any(
            message.role is Role.system and "<global_memory>" in (message.content or "")
            for message in self.messages
        )
        if not memory_was_injected:
            yield MemoryStatusEvent(
                status="stale",
                message=(
                    "Global memory exists but was not injected into this session; "
                    "restart Vibe to reload it."
                ),
                fast_entries=fast_entries,
                persistent_entries=len(document.entries),
            )
            return
        yield MemoryStatusEvent(
            status="loaded",
            message=(
                f"Memory loaded: {fast_entries} recent tool results and "
                f"{len(document.entries)} persistent entries are available."
            ),
            fast_entries=fast_entries,
            persistent_entries=len(document.entries),
        )

    async def _run_automatic_web_search(
        self, objective: str, *, injected: bool
    ) -> AsyncGenerator[BaseEvent, None]:
        activity = self.config.autonomy.web_search_activity
        if (
            injected
            or self._is_subagent
            or not self._should_presearch(objective, activity=activity)
        ):
            return
        query = " ".join(objective.split())[:500].strip()
        normalized_query = query.casefold()
        if not query or normalized_query in self._automatic_web_queries:
            return
        self._automatic_web_queries.add(normalized_query)
        try:
            tool_instance = self.tool_manager.get("web_search")
        except NoSuchToolError:
            yield AssistantEvent(
                content=(
                    f"◇ {activity.title()} web search could not start: the web_search tool is "
                    "unavailable in the active runtime."
                )
            )
            return
        args = WebSearchArgs(query=query)
        call_id = str(uuid4())
        self.messages.append(
            LLMMessage(
                role=Role.assistant,
                content="",
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        index=0,
                        function=FunctionCall(
                            name="web_search", arguments=args.model_dump_json()
                        ),
                    )
                ],
            )
        )
        resolved = ResolvedMessage(
            tool_calls=[
                ResolvedToolCall(
                    tool_name="web_search",
                    tool_class=type(tool_instance),
                    validated_args=args,
                    call_id=call_id,
                )
            ]
        )
        async for event in self._handle_tool_calls(resolved):
            yield event

    def _latest_tool_failure_summary(self) -> tuple[str, str] | None:
        latest = next(
            (
                message
                for message in reversed(self.messages)
                if not is_working_memory_message(message)
            ),
            None,
        )
        if (
            latest is None
            or latest.role is not Role.tool
            or latest.name == "web_search"
        ):
            return None
        content = latest.content or ""
        if f"<{TOOL_ERROR_TAG}>" not in content and "TASK_RESULT: FAIL" not in content:
            return None
        return latest.name or "tool", sanitize_experience_text(content, limit=280)

    async def _run_recovery_web_search(
        self, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        if self.config.autonomy.web_search_activity not in {"high", "max"}:
            return
        failure = self._latest_tool_failure_summary()
        if failure is None:
            return
        tool_name, error = failure
        query = (
            "Find current primary documentation or authoritative fixes for this "
            f"failed {tool_name} step. Goal: {objective}. Observed failure: {error}"
        )
        async for event in self._run_automatic_web_search(query, injected=False):
            yield event

    @staticmethod
    def _is_trivial_direct_request(objective: str) -> bool:
        normalized = objective.casefold().strip(" .!?\t\r\n")
        return normalized in {
            "hi",
            "hello",
            "hey",
            "привет",
            "здравствуй",
            "спасибо",
            "thanks",
        }

    def _start_execution_budget(
        self, objective: str, *, injected: bool
    ) -> ReasoningEvent | None:
        if injected and self._execution_budget is not None:
            return None
        if self._is_subagent or not self.config.autonomy.enabled:
            self._execution_profile = None
            self._execution_budget = None
            return None
        profile = classify_task(
            objective,
            force_large=(
                (self.config.autonomy.boost_mode or self.config.autonomy.gauntlet_loop)
                and not self._is_trivial_direct_request(objective)
            ),
        )
        self._execution_profile = profile
        self._execution_budget = ExecutionBudgetTracker(
            profile, initial_tokens=self.stats.session_total_llm_tokens
        )
        if profile.scale is not TaskScale.DIRECT:
            self.messages.insert(
                -1,
                LLMMessage(
                    role=Role.user,
                    injected=True,
                    content=(
                        "<execution_budget>\n"
                        f"Locally classified task size: {profile.scale.value}. "
                        "This profile has no total wall-clock, token, or main-turn "
                        "limit. Classification controls orchestration depth only. "
                        "Prefer the shortest proven path, "
                        "reuse existing evidence, avoid repeating unchanged tool calls. "
                        "Issue known independent reads, searches, and checks together "
                        "as 2-4 tool calls in the same response so the runtime can run "
                        "them concurrently. Combine dependent cheap shell diagnostics "
                        "into one bounded multiline script instead of paying a model "
                        "round trip per command. Preserve real dependencies and never "
                        "parallelize overlapping mutations. Run tests and observable "
                        "verification before completion. Stop exploring once the acceptance criteria are "
                        "proven.\n</execution_budget>"
                    ),
                ),
            )
        return ReasoningEvent(
            content=(f"Performance profile: {profile.scale.value} · no total limits\n"),
            status_text=f"Performance · {profile.scale.value} · unlimited",
        )

    def _bounded_subagent_timeout(self, configured_seconds: int) -> float | None:
        if configured_seconds <= 0:
            return None
        if self._execution_budget is None:
            return float(configured_seconds)
        return float(configured_seconds)

    def _should_presearch(self, objective: str, *, activity: WebSearchActivity) -> bool:
        if self._is_trivial_direct_request(objective):
            return False
        if _LOCAL_OR_SENSITIVE_SEARCH_RE.search(objective):
            return False
        normalized = objective.casefold()
        temporal_markers = (
            "latest",
            "current",
            "today",
            "now",
            "version",
            "price",
            "documentation",
            "последн",
            "текущ",
            "сегодня",
            "сейчас",
            "актуал",
            "верси",
            "цен",
            "документац",
        )
        explicit_search_markers = (
            "search",
            "web search",
            "google",
            "internet",
            "поиск",
            "поищи",
            "интернет",
        )
        research_markers = (
            "compare",
            "research",
            "verify",
            "docs",
            "api",
            "сравни",
            "исслед",
            "проверь",
            "документац",
        )
        match activity:
            case "off":
                return False
            case "low":
                return any(marker in normalized for marker in explicit_search_markers)
            case "medium":
                return any(
                    marker in normalized
                    for marker in (
                        *temporal_markers,
                        *explicit_search_markers,
                        *research_markers,
                    )
                )
            case "high" | "max":
                return True

    async def _prepare_visible_turn_context(
        self, objective: str, *, injected: bool
    ) -> AsyncGenerator[BaseEvent, None]:
        if budget_event := self._start_execution_budget(objective, injected=injected):
            yield budget_event
        async for event in self._report_global_memory_status(injected=injected):
            yield event
        async for event in self._run_automatic_web_search(objective, injected=injected):
            yield event

    async def _run_harness_phase(
        self,
        phase: HarnessPhase,
        objective: str,
        *,
        tool_name: str | None = None,
        outcome: str | None = None,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> HarnessDecision:
        decision = await self.harness.run(
            phase,
            session_id=self.session_id,
            step=self.stats.steps,
            objective=objective,
            tool_name=tool_name,
            outcome=outcome,
            metadata=metadata,
        )
        context_phases = {
            HarnessPhase.TURN_START,
            HarnessPhase.PRE_STEP,
            HarnessPhase.MODEL_REQUEST,
        }
        if decision.event.context and phase not in context_phases:
            raise RuntimeError(
                f"Harness plugins cannot inject model context during {phase.value}"
            )
        for context_item in decision.event.context:
            self.messages.append(
                LLMMessage(role=Role.user, content=context_item, injected=True)
            )
        return decision

    @staticmethod
    def _should_break_after_turn(last_message: LLMMessage, *, drained: bool) -> bool:
        injected_visual_result = (
            last_message.role is Role.user
            and last_message.injected
            and bool(last_message.images)
        )
        return (
            last_message.role is not Role.tool
            and not injected_visual_result
            and not drained
        )

    def _record_completed_main_turn(self) -> None:
        self.stats.steps += 1
        self._vibe_turns_since_full += 1
        if self._execution_budget is not None:
            self._execution_budget.record_main_turn()

    async def _conversation_loop(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        user_msg: str,
        client_message_id: str | None = None,
        *,
        auto_title: str | None = None,
        images: list[ImageAttachment] | None = None,
        user_display_content: UserDisplayContent | None = None,
        input_text: str | None = None,
        resources: list[UserResource] | None = None,
        injected: bool = False,
    ) -> AsyncGenerator[BaseEvent]:
        turn_start = await self._run_harness_phase(HarnessPhase.TURN_START, user_msg)
        if turn_start.stop_reason is not None:
            yield AssistantEvent(
                content=turn_start.stop_reason, stopped_by_middleware=True
            )
            return
        async for event in self._open_user_turn(
            user_msg,
            client_message_id=client_message_id,
            auto_title=auto_title,
            images=images,
            user_display_content=user_display_content,
            input_text=input_text,
            resources=resources,
            injected=injected,
        ):
            yield event

        async for event in self._prepare_visible_turn_context(
            user_msg, injected=injected
        ):
            yield event

        try:
            async for event in self._prepare_bounded_turn_orchestration(
                user_msg, injected=injected
            ):
                yield event
            autonomy = self._autonomy_coordinator
            if self._autonomy_bootstrap_blocked:
                return
            should_break_loop = False
            first_llm_turn = True
            while not should_break_loop:
                self._is_user_prompt_call = False
                pre_step = await self._run_harness_phase(
                    HarnessPhase.PRE_STEP, user_msg
                )
                if pre_step.stop_reason is not None:
                    yield AssistantEvent(
                        content=pre_step.stop_reason, stopped_by_middleware=True
                    )
                    return
                result = await self.middleware_pipeline.run_before_turn(
                    self._get_context()
                )
                async for event in self._handle_middleware_result(result):
                    yield event

                if result.action == MiddlewareAction.STOP:
                    return

                user_cancelled = False
                self._is_user_prompt_call = first_llm_turn
                try:
                    model_request = await self._run_harness_phase(
                        HarnessPhase.MODEL_REQUEST, user_msg
                    )
                    if model_request.stop_reason is not None:
                        yield AssistantEvent(
                            content=model_request.stop_reason,
                            stopped_by_middleware=True,
                        )
                        return
                    async for event in self._perform_deliberated_llm_turn(
                        user_msg, autonomy
                    ):
                        if is_user_cancellation_event(event):
                            user_cancelled = True
                        yield event
                except ContextTooLongError:
                    if not self._should_self_heal():
                        raise
                    self._reactive_recovery_used = True
                    async for event in self._run_compaction():
                        yield event
                    continue  # retry the turn — still the user's first response
                # A turn ran to completion: count it against the turn budget (so
                # an overflow-and-retry never does) and mark later turns as
                # follow-ups.
                self._record_completed_main_turn()
                first_llm_turn = False
                # Per-turn save so the on-disk log stays fresh; after the
                # inner loop so pre_tool rewrites land in the snapshot.
                await self._save_messages()
                self._is_user_prompt_call = False

                if self._pending_clear_context:
                    async for event in self._clear_context_after_plan_accept():
                        yield event
                    should_break_loop = False
                    continue

                last_message = next(
                    message
                    for message in reversed(self.messages)
                    if not is_working_memory_message(message)
                )
                drained = self._drain_pending_injections()
                should_break_loop = self._should_break_after_turn(
                    last_message, drained=drained
                )

                if user_cancelled:
                    return

                if should_break_loop:
                    stopping = await self._run_harness_phase(
                        HarnessPhase.TURN_STOPPING, user_msg
                    )
                    if stopping.stop_reason is not None:
                        yield AssistantEvent(
                            content=stopping.stop_reason, stopped_by_middleware=True
                        )
                        return
                    if stopping.continue_prompt:
                        self.messages.append(
                            LLMMessage(
                                role=Role.user,
                                content=stopping.continue_prompt,
                                injected=True,
                            )
                        )
                        should_break_loop = False
                        continue
                    retry_msg, hook_events = await self._dispatch_post_turn_hooks()
                    for hook_event in hook_events:
                        yield hook_event
                    should_break_loop = self._queue_post_turn_retry(retry_msg)
                    if should_break_loop and autonomy is not None:
                        async for event in self._maybe_run_autonomy_reviewer(
                            autonomy, self._autonomy_objective or user_msg
                        ):
                            yield event
                        should_break_loop, blocked_event = (
                            self._apply_autonomy_completion(autonomy)
                        )
                        if blocked_event is not None:
                            yield blocked_event
                            return

        finally:
            with contextlib.suppress(Exception):
                await self._run_harness_phase(HarnessPhase.TURN_END, user_msg)
            self.harness.finish_turn()
            self._clear_vibe_deliberation()
            self._clear_personal_experience_context()
            await self._save_messages()

    async def _bootstrap_autonomy(
        self, coordinator: AutonomyCoordinator | None, objective: str, *, injected: bool
    ) -> AsyncGenerator[BaseEvent, None]:
        if coordinator is None:
            return
        if injected and self._autonomy_advisor_completed:
            if (
                self._autonomy_scheduler_needs_resume
                and self._autonomy_plan is not None
            ):
                async for event in self._run_automatic_autonomy_plan(
                    self._autonomy_plan,
                    self._autonomy_objective or objective,
                    resume=True,
                ):
                    coordinator.observe(event)
                    yield event
            return
        if not injected:
            self._autonomy_advisor_completed = False
            self._autonomy_objective = objective
        async for event in self._run_autonomy_advisor(objective):
            coordinator.observe(event)
            yield event
        if not self._autonomy_advisor_completed:
            self._autonomy_bootstrap_blocked = True

    async def _run_autonomy_advisor(
        self, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        active_turn = self._active_turn
        if active_turn is None or active_turn.subagent_runner is None:
            yield AssistantEvent(
                content=(
                    "Autonomous planning stopped: the subagent runtime is unavailable, "
                    "so Vibe cannot run the required goal advisor."
                ),
                stopped_by_middleware=True,
            )
            return
        try:
            tool_instance = self.tool_manager.get("task")
        except NoSuchToolError:
            yield AssistantEvent(
                content=(
                    "Autonomous planning stopped: the task tool is unavailable, so "
                    "Vibe cannot launch the required goal advisor or workers."
                ),
                stopped_by_middleware=True,
            )
            return

        bounded_objective = _compact_autonomy_text(
            objective, _AUTONOMY_OBJECTIVE_MAX_CHARS, "objective"
        )
        gauntlet_instruction = ""
        if self.config.autonomy.gauntlet_loop:
            gauntlet_instruction = (
                "Gauntlet Loop is enabled. Establish a named, fetchable, directly "
                "comparable quality bar from real evidence. Include acquiring that "
                "reference in the plan, split implementation into independently "
                "judgeable builder tasks, and preserve the bar and comparison method "
                "in task text so a separate critic can judge the actual outputs. "
            )
        boost_instruction = ""
        if self.config.autonomy.boost_mode:
            boost_instruction = (
                "BOOST is enabled. Build acceptance criteria from the exact request; "
                "separate known facts from assumptions; add repository or web research "
                "where evidence is missing; assign independent implementation and "
                "verification work; and include concrete commands or observable state "
                "that can prove every completion claim. Keep the graph compact. "
            )
        preflight_instruction = ""
        if self._active_vibe_deliberation_message is not None:
            preflight_instruction = (
                "The root Vibe Thinking preflight ran before delegation. Use this "
                "bounded decision brief as planning input, but verify every claim "
                "against real evidence and keep the original objective authoritative:\n"
                + _compact_autonomy_text(
                    self._active_vibe_deliberation_message.content or "",
                    _AUTONOMY_DEPENDENCY_CONTEXT_MAX_CHARS,
                    "preflight brief",
                )
                + "\n\n"
            )
        args = TaskArgs(
            agent="goal-advisor",
            max_turns=(
                self._execution_profile.advisor_turns
                if self._execution_profile is not None
                and self._execution_profile.advisor_turns > 0
                else None
            ),
            timeout_seconds=(
                self._bounded_subagent_timeout(
                    self._execution_profile.advisor_timeout_seconds
                )
                if self._execution_profile is not None
                and self._execution_profile.advisor_timeout_seconds > 0
                else None
            ),
            task=(
                preflight_instruction
                + boost_instruction
                + gauntlet_instruction
                + "Decompose this objective into acceptance criteria and a compact "
                "dependency graph of executable tasks. Identify which tasks are "
                "independent and can be delegated immediately. For compound work, "
                "create separate non-overlapping explore/worker tasks and mark real "
                "dependencies; do not collapse independent work into one generic "
                "worker. Parallel workers must own disjoint files or components. "
                "Do not implement. "
                + (
                    f"Use at most {self._execution_profile.max_plan_tasks} tasks. "
                    if self._execution_profile is not None
                    else ""
                )
                + "Do not create separate tasks for work that can be verified in "
                "the same focused execution.\n\n"
                f"Objective:\n{bounded_objective}"
            ),
        )
        call_id = str(uuid4())
        self.messages.append(
            LLMMessage(
                role=Role.assistant,
                content="",
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        index=0,
                        function=FunctionCall(
                            name="task", arguments=args.model_dump_json()
                        ),
                    )
                ],
            )
        )
        resolved = ResolvedMessage(
            tool_calls=[
                ResolvedToolCall(
                    tool_name="task",
                    tool_class=type(tool_instance),
                    validated_args=args,
                    call_id=call_id,
                )
            ]
        )
        advisor_result: TaskResult | None = None
        async for event in self._handle_tool_calls(resolved):
            if isinstance(event, ToolResultEvent) and self._advisor_succeeded(
                event, call_id
            ):
                if isinstance(event.result, TaskResult):
                    advisor_result = event.result
            yield event

        if advisor_result is None:
            yield AssistantEvent(
                content=(
                    "Goal advisor unavailable; using the active model fallback and "
                    "continuing autonomous planning."
                )
            )
        plan = await self._resolve_advisor_plan(advisor_result, args.task, objective)
        if (
            self._execution_profile is not None
            and self._execution_profile.max_plan_tasks > 0
            and len(plan.tasks) > self._execution_profile.max_plan_tasks
        ):
            logger.warning(
                "Advisor plan exceeded performance cap tasks=%s cap=%s; "
                "using one focused worker task",
                len(plan.tasks),
                self._execution_profile.max_plan_tasks,
            )
            plan = parse_advisor_plan("", objective)
        has_worker = any(task.agent == "worker" for task in plan.tasks)
        has_root = any(task.agent == "root" for task in plan.tasks)
        if self.config.autonomy.require_worker and not has_worker and not has_root:
            plan = parse_advisor_plan("", objective)
        elif has_root and self._autonomy_coordinator is not None:
            self._autonomy_coordinator.allow_root_owned_plan()
        async for event in self._run_automatic_autonomy_plan(plan, objective):
            yield event
        if self._autonomy_bootstrap_blocked:
            return
        self.messages.append(
            LLMMessage(
                role=Role.user,
                injected=True,
                content=(
                    "The goal advisor plan was materialized. Ready explore/worker "
                    "tasks were delegated; root tasks remain for you to execute "
                    "directly with root-only tools such as computer_use. Resolve every "
                    "remaining todo, inspect observable state after mutations, and "
                    "finish only after the automatic reviewer passes."
                ),
            )
        )

    async def _resolve_advisor_plan(
        self, advisor_result: TaskResult | None, task: str, objective: str
    ) -> AutonomyPlan:
        if advisor_result is not None:
            response = advisor_result.response
            evidence_label = "goal-advisor: "
        else:
            self.messages.append(
                LLMMessage(
                    role=Role.user,
                    injected=True,
                    content=(
                        "The dedicated goal advisor failed. The active main model is "
                        "now producing a read-only fallback plan; continue instead "
                        "of stopping the user's task."
                    ),
                )
            )
            response = await self._run_active_model_advisor_fallback(task)
            evidence_label = "advisor-fallback: "
            if self._autonomy_coordinator is not None:
                self._autonomy_coordinator.seed_advisor_completed()

        self._autonomy_advisor_completed = True
        self._autonomy_evidence.append(
            evidence_label
            + _compact_autonomy_text(
                response or "active model fallback was unavailable",
                _AUTONOMY_STORED_RESULT_MAX_CHARS,
                "advisor evidence",
            )
        )
        if response is not None:
            return parse_advisor_plan(response, objective)
        worker_plan = parse_advisor_plan("", objective)
        return AutonomyPlan(
            tasks=[worker_plan.tasks[0].model_copy(update={"agent": "root"})]
        )

    async def _run_active_model_advisor_fallback(self, task: str) -> str | None:
        messages = [
            LLMMessage(
                role=Role.system,
                content=(
                    "You are the read-only fallback goal advisor. Return only a "
                    f"{AUTONOMY_PLAN_START} JSON object {AUTONOMY_PLAN_END}. The "
                    "object must contain a tasks array; every task has id, content, "
                    "agent (explore, worker, or root), and depends_on. Keep the graph "
                    "compact, dependency ordered, and faithful to the objective. Do "
                    "not execute tools or claim work is complete."
                ),
            ),
            LLMMessage(role=Role.user, content=task),
        ]
        try:
            result = await self._complete(
                model=self.config.get_active_model(),
                messages=messages,
                tools=[],
                tool_choice=None,
                call_type="secondary_call",
                max_tokens=_ADVISOR_FALLBACK_MAX_TOKENS,
            )
        except Exception:
            logger.warning("Active-model advisor fallback failed", exc_info=True)
            return None
        response = result.message.content or ""
        if AUTONOMY_PLAN_START not in response or AUTONOMY_PLAN_END not in response:
            logger.warning("Active-model advisor fallback returned no valid plan")
            return None
        return response

    async def _run_automatic_autonomy_plan(
        self, plan: AutonomyPlan, objective: str, *, resume: bool = False
    ) -> AsyncGenerator[BaseEvent, None]:
        if not self._autonomy_tools_available(("todo", "task")):
            self._autonomy_bootstrap_blocked = True
            self._autonomy_advisor_completed = False
            self.messages.append(
                LLMMessage(
                    role=Role.user,
                    injected=True,
                    content=(
                        "Automatic planning could not start because the todo or task "
                        "tool is unavailable. Restore both tools before completion."
                    ),
                )
            )
            yield AssistantEvent(
                content=(
                    "Autonomous planning stopped: the todo or task tool is unavailable, "
                    "so Vibe cannot publish the plan and delegate its work."
                ),
                stopped_by_middleware=True,
            )
            return

        if not resume:
            self._autonomy_plan = plan
            self._autonomy_todos = plan.to_todos()
            self._autonomy_completed_tasks = set()
            self._autonomy_task_results = {}
        todos = self._autonomy_todos
        async for event in self._write_automatic_todos(todos):
            yield event
        if not self._automatic_todo_write_succeeded:
            self._autonomy_bootstrap_blocked = True
            self._autonomy_advisor_completed = False
            yield self._autonomy_plan_write_failure("initial")
            return

        completed = self._autonomy_completed_tasks
        while len(completed) < len(plan.tasks):
            selected = self._select_automatic_plan_tasks(plan, todos, completed)
            if not selected:
                break
            async for event in self._run_automatic_autonomy_wave(selected, objective):
                yield event
            if self._autonomy_bootstrap_blocked:
                return
            if not self._automatic_wave_progressed:
                break
            todos = self._autonomy_todos
        self._autonomy_scheduler_needs_resume = len(completed) < len(plan.tasks)

    async def _run_automatic_autonomy_wave(
        self, selected: list[AutonomyPlanTask], objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        self._automatic_wave_progressed = False
        selected_ids = {task.id for task in selected}
        todos = self._todos_with_status(selected_ids, TodoStatus.IN_PROGRESS)
        self._autonomy_todos = todos
        async for event in self._write_automatic_todos(todos):
            yield event
        if not self._automatic_todo_write_succeeded:
            self._autonomy_bootstrap_blocked = True
            self._autonomy_advisor_completed = False
            yield self._autonomy_plan_write_failure("in-progress")
            return

        args = [
            TaskArgs(
                agent=task.agent,
                max_turns=(
                    self._execution_profile.worker_turns
                    if self._execution_profile is not None
                    and self._execution_profile.worker_turns > 0
                    else None
                ),
                timeout_seconds=(
                    self._bounded_subagent_timeout(
                        self._execution_profile.worker_timeout_seconds
                    )
                    if self._execution_profile is not None
                    and self._execution_profile.worker_timeout_seconds > 0
                    else None
                ),
                task=self._automatic_task_prompt(
                    task, objective, self._autonomy_task_results
                ),
            )
            for task in selected
        ]
        batch = self._build_automatic_tool_batch("task", args)
        if batch is None:
            return
        resolved, call_ids = batch
        task_by_call = dict(zip(call_ids, selected, strict=True))
        succeeded: set[str] = set()
        try:
            async for event in self._run_automatic_task_batch(
                resolved, task_by_call, succeeded, self._autonomy_task_results
            ):
                yield event
        except asyncio.CancelledError:
            self._autonomy_todos = self._todos_with_status(
                selected_ids, TodoStatus.PENDING
            )
            self._autonomy_scheduler_needs_resume = True
            async for event in self._write_automatic_todos(self._autonomy_todos):
                yield event
            raise
        except GeneratorExit:
            self._autonomy_todos = self._todos_with_status(
                selected_ids, TodoStatus.PENDING
            )
            self._autonomy_scheduler_needs_resume = True
            raise

        self._autonomy_todos = self._todos_with_outcomes(selected_ids, succeeded)
        self._autonomy_completed_tasks.update(succeeded)
        async for event in self._write_automatic_todos(self._autonomy_todos):
            yield event
        if not self._automatic_todo_write_succeeded:
            self._autonomy_bootstrap_blocked = True
            self._autonomy_scheduler_needs_resume = True
            yield self._autonomy_plan_write_failure("terminal")
            return
        self._automatic_wave_progressed = succeeded == selected_ids

    def _todos_with_status(
        self, task_ids: set[str], status: TodoStatus
    ) -> list[TodoItem]:
        return [
            todo.model_copy(update={"status": status}) if todo.id in task_ids else todo
            for todo in self._autonomy_todos
        ]

    def _todos_with_outcomes(
        self, selected_ids: set[str], succeeded: set[str]
    ) -> list[TodoItem]:
        return [
            todo.model_copy(
                update={
                    "status": (
                        TodoStatus.COMPLETED
                        if todo.id in succeeded
                        else TodoStatus.PENDING
                    )
                }
            )
            if todo.id in selected_ids
            else todo
            for todo in self._autonomy_todos
        ]

    async def _run_automatic_task_batch(
        self,
        resolved: ResolvedMessage,
        task_by_call: dict[str, AutonomyPlanTask],
        succeeded: set[str],
        task_results: dict[str, str],
    ) -> AsyncGenerator[BaseEvent, None]:
        async for event in self._handle_tool_calls(resolved):
            if isinstance(event, ToolResultEvent):
                task = task_by_call.get(event.tool_call_id)
                result = event.result
                if (
                    task is not None
                    and isinstance(result, TaskResult)
                    and self._automatic_task_succeeded(event, result)
                ):
                    succeeded.add(task.id)
                    compact_result = _compact_autonomy_text(
                        result.response,
                        _AUTONOMY_STORED_RESULT_MAX_CHARS,
                        "subagent result",
                    )
                    task_results[task.id] = compact_result
                    self._autonomy_evidence.append(
                        f"{task.id} ({task.agent}): {compact_result}"
                    )
            yield event

    async def _run_autonomy_reviewer(
        self, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        bounded_objective = _compact_autonomy_text(
            objective, _AUTONOMY_OBJECTIVE_MAX_CHARS, "objective"
        )
        gauntlet_instruction = ""
        if self.config.autonomy.gauntlet_loop:
            gauntlet_instruction = (
                "Act as the independent harsh Gauntlet critic. Obtain the real quality "
                "bar named in the execution evidence, compare it directly and blindly "
                "against the actual output, make a binary choice, and identify the "
                "largest remaining gap. A pass is forbidden unless our output wins. "
            )
        boost_instruction = ""
        if self.config.autonomy.boost_mode:
            boost_instruction = (
                "BOOST review is adversarial and evidence-first. Re-open the changed "
                "artifacts, run the most relevant checks, challenge the chosen approach "
                "against at least one plausible alternative, and reject unsupported "
                "claims, stale web facts, skipped acceptance criteria, or simulated "
                "verification. "
            )
        args = TaskArgs(
            agent="reviewer",
            max_turns=(
                self._execution_profile.reviewer_turns
                if self._execution_profile is not None
                and self._execution_profile.reviewer_turns > 0
                else None
            ),
            timeout_seconds=(
                self._bounded_subagent_timeout(
                    self._execution_profile.reviewer_timeout_seconds
                )
                if self._execution_profile is not None
                and self._execution_profile.reviewer_timeout_seconds > 0
                else None
            ),
            task=(
                boost_instruction
                + gauntlet_instruction
                + "Review the current workspace and fresh evidence for this autonomous "
                "goal. Independently check every acceptance criterion and todo result. "
                "Do not trust completion claims without matching tool, test, file, or "
                "observable-state evidence. Before a passing verdict, emit at least one "
                "`EVIDENCE_CHECKED: <claim> => <specific evidence>` line. End with "
                "VERDICT: PASS only if every material claim is proven; otherwise end "
                "with VERDICT: FAIL and list precise remaining work.\n\n"
                f"Goal:\n{bounded_objective}\n\nBounded execution evidence:\n"
                f"{self._bounded_autonomy_evidence()}"
            ),
        )
        batch = self._build_automatic_tool_batch("task", [args])
        if batch is None:
            yield AssistantEvent(
                content=(
                    "Autonomous review could not start because the task tool is "
                    "unavailable. Completion remains blocked until review can run."
                ),
                stopped_by_middleware=True,
            )
            return
        resolved, _ = batch
        async for event in self._handle_tool_calls(resolved):
            yield event

    async def _maybe_run_autonomy_reviewer(
        self, coordinator: AutonomyCoordinator | None, objective: str
    ) -> AsyncGenerator[BaseEvent, None]:
        if coordinator is None or not coordinator.should_run_reviewer():
            return
        async for event in self._run_autonomy_reviewer(objective):
            coordinator.observe(event)
            yield event

    def _select_automatic_plan_tasks(
        self, plan: AutonomyPlan, todos: list[TodoItem], completed: set[str]
    ) -> list[AutonomyPlanTask]:
        ready = [
            task
            for task in plan.tasks
            if task.id not in completed
            and all(dependency in completed for dependency in task.depends_on)
            and self._todo_status(todos, task.id) is TodoStatus.PENDING
        ]
        if not ready:
            return []
        limit = self.config.autonomy.effective_parallel_subagents
        if self._execution_profile is not None:
            limit = min(limit, self._execution_profile.max_parallel_subagents)
        if limit == 0:
            return []
        return [task for task in ready if task.agent in {"explore", "worker"}][:limit]

    @staticmethod
    def _automatic_task_prompt(
        task: AutonomyPlanTask, objective: str, task_results: dict[str, str]
    ) -> str:
        bounded_objective = _compact_autonomy_text(
            objective, _AUTONOMY_OBJECTIVE_MAX_CHARS, "objective"
        )
        dependency_evidence = "\n\n".join(
            f"Dependency {dependency}:\n"
            f"{task_results.get(dependency, '[no result]')[-_AUTONOMY_DEPENDENCY_RESULT_MAX_CHARS:]}"
            for dependency in task.depends_on
        )
        dependency_evidence = dependency_evidence[
            -_AUTONOMY_DEPENDENCY_CONTEXT_MAX_CHARS:
        ]
        if dependency_evidence:
            dependency_evidence = f"\n\nDependency results:\n{dependency_evidence}"
        return (
            "Complete this assigned part of the autonomous goal. Work only within "
            "this scope, respect repository instructions, and report concrete changes "
            "and verification. If the task is actually complete, make the final "
            f"non-empty line exactly `{_AUTONOMY_TASK_PASS_MARKER}`. Otherwise end "
            "with `TASK_RESULT: FAIL` and explain the blocker.\n\n"
            f"Goal:\n{bounded_objective}\n\nAssigned task ({task.id}):\n{task.content}"
            f"{dependency_evidence}"
        )

    def _bounded_autonomy_evidence(self) -> str:
        evidence: list[str] = []
        seen: set[str] = set()
        used_chars = 0

        def append_unique(item: str) -> bool:
            nonlocal used_chars
            normalized = item.strip()
            if not normalized or normalized in seen:
                return True
            separator_chars = 2 if evidence else 0
            remaining = (
                _AUTONOMY_REVIEW_EVIDENCE_MAX_CHARS - used_chars - separator_chars
            )
            if remaining <= 0:
                return False
            compact = _compact_autonomy_text(normalized, remaining, "evidence")
            evidence.append(compact)
            seen.add(normalized)
            used_chars += separator_chars + len(compact)
            return used_chars < _AUTONOMY_REVIEW_EVIDENCE_MAX_CHARS

        for item in reversed(self._autonomy_evidence):
            if not append_unique(item):
                break
        for message in reversed(self.messages):
            if message.role is not Role.tool or not message.content:
                continue
            if message.name == "task":
                continue
            if not append_unique(f"{message.name or 'tool'}: {message.content}"):
                break
        joined = "\n\n".join(evidence)
        return joined or "No execution evidence was recorded."

    @staticmethod
    def _automatic_task_succeeded(event: ToolResultEvent, result: TaskResult) -> bool:
        lines = [line.strip() for line in result.response.splitlines() if line.strip()]
        return (
            result.completed
            and not (event.error or event.cancelled or event.skipped)
            and bool(lines)
            and lines[-1] == _AUTONOMY_TASK_PASS_MARKER
        )

    @staticmethod
    def _autonomy_plan_write_failure(stage: str) -> AssistantEvent:
        return AssistantEvent(
            content=(
                f"Autonomous planning stopped: the {stage} todo update failed. "
                "No further worker tasks will be launched with an untracked plan."
            ),
            stopped_by_middleware=True,
        )

    def _autonomy_tools_available(self, names: Sequence[str]) -> bool:
        try:
            for name in names:
                self.tool_manager.get(name)
        except NoSuchToolError:
            return False
        return True

    def _build_automatic_tool_batch(
        self, tool_name: str, args: Sequence[BaseModel]
    ) -> tuple[ResolvedMessage, list[str]] | None:
        try:
            tool_instance = self.tool_manager.get(tool_name)
        except NoSuchToolError:
            return None
        call_ids = [str(uuid4()) for _ in args]
        self.messages.append(
            LLMMessage(
                role=Role.assistant,
                content="",
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        index=index,
                        function=FunctionCall(
                            name=tool_name, arguments=tool_args.model_dump_json()
                        ),
                    )
                    for index, (call_id, tool_args) in enumerate(
                        zip(call_ids, args, strict=True)
                    )
                ],
            )
        )
        return (
            ResolvedMessage(
                tool_calls=[
                    ResolvedToolCall(
                        tool_name=tool_name,
                        tool_class=type(tool_instance),
                        validated_args=tool_args,
                        call_id=call_id,
                    )
                    for call_id, tool_args in zip(call_ids, args, strict=True)
                ]
            ),
            call_ids,
        )

    async def _write_automatic_todos(
        self, todos: list[TodoItem]
    ) -> AsyncGenerator[BaseEvent, None]:
        self._automatic_todo_write_succeeded = False
        self._automatic_wave_progressed = False
        batch = self._build_automatic_tool_batch(
            "todo", [TodoArgs(action="write", todos=todos)]
        )
        if batch is None:
            return
        resolved, _ = batch
        async for event in self._handle_tool_calls(resolved):
            if (
                isinstance(event, ToolResultEvent)
                and isinstance(event.result, TodoResult)
                and not (event.error or event.cancelled or event.skipped)
            ):
                self._automatic_todo_write_succeeded = True
            yield event

    @staticmethod
    def _todo_status(todos: list[TodoItem], task_id: str) -> TodoStatus:
        return next(todo.status for todo in todos if todo.id == task_id)

    @staticmethod
    def _advisor_succeeded(event: BaseEvent, call_id: str) -> bool:
        if not isinstance(event, ToolResultEvent) or event.tool_call_id != call_id:
            return False
        result = event.result
        failed = event.error or event.cancelled or event.skipped
        return isinstance(result, TaskResult) and result.completed and not failed

    def _queue_post_turn_retry(self, retry_msg: LLMMessage | None) -> bool:
        # Returns whether the loop should still break (no retry queued).
        if retry_msg is None:
            return True
        self.messages.append(retry_msg)
        return False

    def _apply_autonomy_completion(
        self, coordinator: AutonomyCoordinator
    ) -> tuple[bool, AssistantEvent | None]:
        decision = coordinator.evaluate_completion()
        match decision.kind:
            case AutonomyDecisionKind.RETRY:
                should_break = self._queue_post_turn_retry(
                    LLMMessage(
                        role=Role.user,
                        content=decision.instruction or "Continue working.",
                        injected=True,
                    )
                )
                return should_break, None
            case AutonomyDecisionKind.BLOCK:
                return True, AssistantEvent(
                    content=(
                        f"<{VIBE_STOP_EVENT_TAG}>{decision.instruction}"
                        f"</{VIBE_STOP_EVENT_TAG}>"
                    ),
                    stopped_by_middleware=True,
                )
            case AutonomyDecisionKind.PASS:
                return True, None

    def _skill_already_loaded(self, name: str) -> bool:
        marker = skill_content_marker(name)
        return any(
            m.role == Role.tool and m.name == "skill" and marker in (m.content or "")
            for m in self._current_model_context()
        )

    async def _inject_invoked_skill(
        self, user_msg: str
    ) -> AsyncGenerator[BaseEvent, None]:
        parsed = self.skill_manager.parse_skill_command(user_msg)
        if parsed is None:
            return
        skill_info = self.skill_manager.get_skill(parsed.name)
        if skill_info is None:
            return

        result = select_skill_result(
            skill_info, already_loaded=self._skill_already_loaded(parsed.name)
        )
        call_id = str(uuid4())
        tool_class = self.tool_manager.available_tools.get("skill", SkillTool)
        call_event = ToolCallEvent(
            tool_call_id=call_id,
            tool_call_index=0,
            tool_name="skill",
            tool_class=tool_class,
            args=SkillArgs(name=parsed.name),
        )
        call_event = call_event.model_copy(
            update={
                "presentation": ToolUIDataAdapter(
                    tool_class, harness_files=self.harness_files
                ).get_call_presentation(call_event)
            }
        )
        result_event = ToolResultEvent(
            tool_name="skill",
            tool_class=tool_class,
            result=result,
            tool_call_id=call_id,
        )
        result_event = result_event.model_copy(
            update={
                "presentation": ToolUIDataAdapter(
                    tool_class, harness_files=self.harness_files
                ).get_result_presentation(result_event)
            }
        )

        self.messages.append(
            LLMMessage(
                role=Role.assistant,
                content="",
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        index=0,
                        function=FunctionCall(
                            name="skill", arguments=json.dumps({"name": parsed.name})
                        ),
                        presentation=call_event.presentation,
                    )
                ],
            )
        )
        result_text = "\n".join(f"{k}: {v}" for k, v in result.model_dump().items())
        self.messages.append(
            LLMMessage(
                role=Role.tool,
                tool_call_id=call_id,
                name="skill",
                content=result_text,
                tool_result=PersistedToolResult(
                    output=cast(dict[str, JsonValue], result.model_dump(mode="json")),
                    presentation=result_event.presentation,
                ),
            )
        )

        yield call_event
        yield result_event

    async def _inject_mentioned_files(
        self, user_msg: str
    ) -> AsyncGenerator[BaseEvent, None]:
        payload = build_path_prompt_payload(user_msg)
        file_resources = [r for r in payload.resources if r.kind == "file"]
        if not file_resources:
            return
        try:
            tool_instance = self.tool_manager.get("read_file")
        except NoSuchToolError:
            return
        tool_class = type(tool_instance)

        for resource in file_resources:
            file_path = str(resource.path)
            call_id = str(uuid4())
            self.messages.append(
                LLMMessage(
                    role=Role.assistant,
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=call_id,
                            index=0,
                            function=FunctionCall(
                                name="read_file",
                                arguments=json.dumps({"file_path": file_path}),
                            ),
                        )
                    ],
                )
            )
            tool_call = ResolvedToolCall(
                tool_name="read_file",
                tool_class=tool_class,
                validated_args=ReadFileArgs(file_path=file_path),
                call_id=call_id,
            )
            call_event = ToolCallEvent(
                tool_call_id=call_id,
                tool_call_index=0,
                tool_name="read_file",
                tool_class=tool_class,
                args=ReadFileArgs(file_path=file_path),
            )
            call_event = call_event.model_copy(
                update={
                    "presentation": ToolUIDataAdapter(
                        tool_class, harness_files=self.harness_files
                    ).get_call_presentation(call_event)
                }
            )
            self._record_tool_call_presentation(call_event)
            yield call_event
            async for event in self._process_one_tool_call(tool_call):
                yield event

    def _handle_plan_review_ended(self) -> None:
        if not self._plan_session.has_content_changed():
            return None

        content = self._plan_session.read()
        if content is None:
            return None

        msg = LLMMessage(
            role=Role.user,
            content=(
                f"<{VIBE_WARNING_TAG}>The user has manually updated the plan file. "
                f"Here is the updated version -- use this as the source of truth "
                f"for implementation:\n\n{content}</{VIBE_WARNING_TAG}>"
            ),
            injected=True,
        )
        self._pending_injected_messages.append(msg)

    def _handle_session_plan_events(self, event: BaseEvent) -> BaseEvent | None:
        if isinstance(event, ToolCallEvent) and event.tool_name == "exit_plan_mode":
            self._plan_session.snapshot_content_hash()
            return PlanReviewRequestedEvent(file_path=self._plan_session.plan_file_path)

        if isinstance(event, ToolResultEvent) and event.tool_name == "exit_plan_mode":
            self._handle_plan_review_ended()
            return PlanReviewEndedEvent()

        return None

    async def _perform_llm_turn(self) -> AsyncGenerator[BaseEvent, None]:
        if self.enable_streaming:
            async for event in self._stream_assistant_events():
                yield event
        else:
            assistant_event = await self._get_assistant_event()
            if assistant_event.content:
                yield assistant_event

        last_message = self.messages[-1]

        parsed = self.format_handler.parse_message(last_message)
        resolved = self.format_handler.resolve_tool_calls(parsed, self.tool_manager)

        if not resolved.tool_calls and not resolved.failed_calls:
            return

        profile_before = self.agent_profile.name
        async for event in self._handle_tool_calls(resolved):
            yield event

            if session_plan_event := self._handle_session_plan_events(event):
                yield session_plan_event

        if self.agent_profile.name != profile_before:
            yield AgentProfileChangedEvent(agent_name=self.agent_profile.name)

    def _build_tool_call_events(
        self, tool_calls: list[ToolCall] | None, emitted_ids: set[str]
    ) -> Generator[ToolCallEvent, None, None]:
        for tc in tool_calls or []:
            if tc.id is None or not tc.function.name:
                continue
            if tc.id in emitted_ids:
                continue

            tool_class = self.tool_manager.available_tools.get(tc.function.name)
            if tool_class is None:
                continue

            event = ToolCallEvent(
                tool_call_id=tc.id,
                tool_call_index=tc.index,
                tool_name=tc.function.name,
                tool_class=tool_class,
            )
            yield event.model_copy(
                update={
                    "presentation": ToolUIDataAdapter(
                        tool_class, harness_files=self.harness_files
                    ).get_call_presentation(event)
                }
            )

    async def _stream_assistant_events(
        self,
    ) -> AsyncGenerator[AssistantEvent | ReasoningEvent | ToolCallEvent]:
        message_id: str | None = None
        reasoning_message_id: str | None = None
        emitted_tool_call_ids = set[str]()

        async for chunk in self._chat_streaming():
            if message_id is None:
                message_id = chunk.message.message_id
            if reasoning_message_id is None:
                reasoning_message_id = chunk.message.reasoning_message_id

            if chunk.message.reasoning_content:
                yield ReasoningEvent(
                    content=chunk.message.reasoning_content,
                    message_id=reasoning_message_id,
                )

            if chunk.message.content:
                yield AssistantEvent(
                    content=chunk.message.content, message_id=message_id
                )

            for event in self._build_tool_call_events(
                chunk.message.tool_calls, emitted_tool_call_ids
            ):
                emitted_tool_call_ids.add(event.tool_call_id)
                yield event

    async def _get_assistant_event(self) -> AssistantEvent:
        llm_result = await self._chat()
        return AssistantEvent(
            content=llm_result.message.content or "",
            message_id=llm_result.message.message_id,
        )

    async def _handle_tool_calls(
        self, resolved: ResolvedMessage
    ) -> AsyncGenerator[BaseEvent]:
        self._pending_working_memory_records.clear()
        self._experience_context_fresh = False
        async for event in self._emit_failed_tool_events(resolved.failed_calls):
            yield event
        if not resolved.tool_calls:
            experience_records = list(self._pending_working_memory_records)
            self._flush_working_memory()
            async for event in self._persist_personal_experience(experience_records):
                yield event
            return

        objective = self._autonomy_objective or "execute requested tool batch"
        dispatch_decision = await self._run_harness_phase(
            HarnessPhase.TOOL_DISPATCH,
            objective,
            metadata={"tool_count": len(resolved.tool_calls)},
        )
        if dispatch_decision.stop_reason is not None:
            async for event in self._emit_failed_tool_events([
                FailedToolCall(
                    tool_name=tool_call.tool_name,
                    call_id=tool_call.call_id,
                    error=dispatch_decision.stop_reason,
                )
                for tool_call in resolved.tool_calls
            ]):
                yield event
            return

        for tool_call in resolved.tool_calls:
            event = ToolCallEvent(
                tool_name=tool_call.tool_name,
                tool_class=tool_call.tool_class,
                args=tool_call.validated_args,
                tool_call_id=tool_call.call_id,
            )
            event = event.model_copy(
                update={
                    "presentation": ToolUIDataAdapter(
                        tool_call.tool_class, harness_files=self.harness_files
                    ).get_call_presentation(event)
                }
            )
            self._record_tool_call_presentation(event)
            yield event

        result_images: list[ImageAttachment] = []
        async for event in self._run_tools_concurrently(resolved.tool_calls):
            if (
                isinstance(event, ToolResultEvent)
                and event.result is not None
                and not (event.error or event.cancelled or event.skipped)
            ):
                with contextlib.suppress(NoSuchToolError):
                    tool = self.tool_manager.get(event.tool_name)
                    result_images.extend(tool.get_result_images(event.result))
            yield event
        result_decision = await self._run_harness_phase(
            HarnessPhase.TOOL_RESULT,
            objective,
            outcome="completed",
            metadata={"tool_count": len(resolved.tool_calls)},
        )
        if result_decision.stop_reason is not None:
            raise RuntimeError(result_decision.stop_reason)
        experience_records = list(self._pending_working_memory_records)
        self._flush_working_memory()
        async for event in self._persist_personal_experience(experience_records):
            yield event
        if result_images:
            self.messages.append(
                LLMMessage(
                    role=Role.user,
                    injected=True,
                    content=(
                        "Visual output captured by the completed tool batch is "
                        "attached. Inspect it before deciding the next action and "
                        "verify the observable result instead of assuming success."
                    ),
                    images=result_images,
                )
            )

    def _record_tool_call_presentation(self, event: ToolCallEvent) -> None:
        if event.presentation is None:
            return
        for message in reversed(self.messages):
            for tool_call in message.tool_calls or []:
                if tool_call.id == event.tool_call_id:
                    tool_call.presentation = event.presentation
                    return

    async def _emit_failed_tool_events(
        self, failed_calls: list[FailedToolCall]
    ) -> AsyncGenerator[ToolResultEvent]:
        for failed in failed_calls:
            error_msg = f"<{TOOL_ERROR_TAG}>{failed.tool_name}: {failed.error}</{TOOL_ERROR_TAG}>"
            yield ToolResultEvent(
                tool_name=failed.tool_name,
                tool_class=None,
                error=error_msg,
                tool_call_id=failed.call_id,
            )
            self.stats.tool_calls_failed += 1
            self.messages.append(
                self.format_handler.create_failed_tool_response_message(
                    failed, error_msg
                )
            )
            if not self._is_subagent:
                self._pending_working_memory_records.append((
                    failed.tool_name,
                    failed.error,
                    "failure",
                    error_msg,
                ))

    async def _run_tools_concurrently(
        self, tool_calls: list[ResolvedToolCall]
    ) -> AsyncGenerator[BaseEvent]:
        """Execute multiple tool calls concurrently, yielding events as they arrive."""
        queue: asyncio.Queue[BaseEvent | None] = asyncio.Queue()
        if self._tool_event_queue is not None:
            raise AgentLoopStateError("A tool batch is already active")
        self._tool_event_queue = queue
        self._request_broker.bind(queue)

        tasks = [
            asyncio.create_task(self._execute_tool_to_queue(tc, queue))
            for tc in tool_calls
        ]

        async def _signal_when_all_done() -> None:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await queue.put(None)

        monitor = asyncio.create_task(_signal_when_all_done())

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        except GeneratorExit:
            for t in tasks:
                if not t.done():
                    t.cancel()
            raise
        except asyncio.CancelledError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._request_broker.unbind(queue)
            self._tool_event_queue = None
            if not monitor.done():
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor

    async def _execute_tool_to_queue(
        self, tc: ResolvedToolCall, queue: asyncio.Queue[BaseEvent | None]
    ) -> None:
        """Run a single tool call, sending events to the queue."""
        async for event in self._process_one_tool_call(tc):
            await queue.put(event)

    async def _process_one_tool_call(
        self, tool_call: ResolvedToolCall
    ) -> AsyncGenerator[ToolResultEvent | ToolStreamEvent | HookEvent]:
        if self._execution_budget is not None:
            effect = ToolUIDataAdapter(
                tool_call.tool_class, harness_files=self.harness_files
            ).effect_kind
            if effect in _PERFORMANCE_MUTATION_EFFECTS:
                self._execution_budget.record_mutation()
            elif self._execution_budget.repeated_read(
                tool_call.tool_name, tool_call.validated_args.model_dump_json()
            ):
                async with tool_span(
                    tool_name=tool_call.tool_name,
                    call_id=tool_call.call_id,
                    arguments=tool_call.validated_args.model_dump_json(),
                ) as span:
                    yield self._tool_failure_event(
                        tool_call,
                        (
                            "Repeated unchanged read blocked by the adaptive "
                            "performance guard. Reuse the two existing observations "
                            "from context, change the query, or make progress before "
                            "reading the same target again."
                        ),
                        span=span,
                    )
                return
        async with tool_span(
            tool_name=tool_call.tool_name,
            call_id=tool_call.call_id,
            arguments=tool_call.validated_args.model_dump_json(),
        ) as span:
            async for event in self._execute_tool_call(span, tool_call):
                yield event

    async def _execute_tool_call(
        self, span: trace.Span, tool_call: ResolvedToolCall
    ) -> AsyncGenerator[ToolResultEvent | ToolStreamEvent | HookEvent]:
        try:
            tool_instance = self.tool_manager.get(tool_call.tool_name)
        except Exception as exc:
            error_msg = f"Error getting tool '{tool_call.tool_name}': {exc}"
            yield self._tool_failure_event(tool_call, error_msg, span=span)
            return

        try:
            tool_input = self._serialize_tool_input(tool_call)
        except Exception as exc:
            error_msg = (
                f"<{TOOL_ERROR_TAG}>Failed to serialize tool input for "
                f"'{tool_call.tool_name}': {exc}</{TOOL_ERROR_TAG}>"
            )
            self.stats.tool_calls_failed += 1
            yield ToolResultEvent(
                tool_name=tool_call.tool_name,
                tool_class=tool_call.tool_class,
                error=error_msg,
                tool_call_id=tool_call.call_id,
            )
            self._handle_tool_response(tool_call, error_msg, "failure", span=span)
            return

        events, resolution = await self._run_pre_tool_pipeline(
            tool_call, tool_input, span=span
        )
        for ev in events:
            yield ev
        if resolution.denial_event is not None:
            yield resolution.denial_event
            return
        tool_call = resolution.tool_call
        tool_input = resolution.tool_input

        decision: ToolDecision | None = None
        tool_started = False
        try:
            decision = await self._should_execute_tool(
                tool_instance, tool_call.validated_args, tool_call.call_id
            )

            if decision.verdict == ToolExecutionResponse.SKIP:
                async for ev in self._handle_tool_skip(tool_call, decision, span=span):
                    yield ev
                return

            tool_started = True
            async for ev in self._invoke_tool(
                tool_call, tool_instance, tool_input, decision, span=span
            ):
                yield ev

        except asyncio.CancelledError:
            cancel = str(
                get_user_cancellation_message(CancellationReason.TOOL_INTERRUPTED)
            )
            logger.info(
                "Tool call cancelled tool=%s tool_call_id=%s outcome=cancelled",
                tool_call.tool_name,
                tool_call.call_id,
            )
            self.stats.tool_calls_failed += 1
            yield ToolResultEvent(
                tool_name=tool_call.tool_name,
                tool_class=tool_call.tool_class,
                error=cancel,
                cancelled=True,
                tool_call_id=tool_call.call_id,
            )
            async for ev in self._finalize_cancelled_tool(
                tool_call,
                tool_input,
                decision,
                cancel,
                span=span,
                tool_started=tool_started,
            ):
                yield ev
            raise

        except Exception as exc:
            # One prefix for both: the model reads `error`, the client `display`.
            failure = f"{tool_instance.get_name()} failed: "
            error_msg = f"<{TOOL_ERROR_TAG}>{failure}{exc}</{TOOL_ERROR_TAG}>"
            if isinstance(exc, ToolPermissionError):
                logger.info(
                    "Tool call denied tool=%s tool_call_id=%s outcome=denied",
                    tool_call.tool_name,
                    tool_call.call_id,
                )
                self.stats.tool_calls_agreed -= 1
                self.stats.tool_calls_rejected += 1
            else:
                logger.warning(
                    "Tool call failed tool=%s tool_call_id=%s outcome=error error=%s",
                    tool_call.tool_name,
                    tool_call.call_id,
                    type(exc).__name__,
                )
                self.stats.tool_calls_failed += 1
            yield ToolResultEvent(
                tool_name=tool_call.tool_name,
                tool_class=tool_call.tool_class,
                error=error_msg,
                error_display=(
                    f"{failure}{exc.display}" if isinstance(exc, ToolError) else None
                ),
                tool_call_id=tool_call.call_id,
            )
            async for ev in self._run_post_tool_and_finalize(
                tool_call,
                PostToolFinalization(
                    tool_input=tool_input,
                    tool_status="failure",
                    response_status="failure",
                    decision=decision,
                    span=span,
                    tool_error=str(exc),
                    initial_text=error_msg,
                ),
            ):
                yield ev

    async def _invoke_tool(
        self,
        tool_call: ResolvedToolCall,
        tool_instance: BaseTool,
        tool_input: dict[str, Any],
        decision: ToolDecision,
        *,
        span: trace.Span,
    ) -> AsyncGenerator[ToolResultEvent | ToolStreamEvent | HookEvent]:
        self.stats.tool_calls_agreed += 1

        snapshot = await asyncio.to_thread(
            tool_instance.get_file_snapshot, tool_call.validated_args
        )
        if snapshot is not None:
            self.checkpoint_recorder.add_snapshot(snapshot)

        start_time = time.perf_counter()
        logger.debug(
            "Tool call starting tool=%s tool_call_id=%s",
            tool_call.tool_name,
            tool_call.call_id,
        )
        result_model = None
        async for item in tool_instance.invoke(
            ctx=InvokeContext(
                tool_call_id=tool_call.call_id,
                agent_manager=self.agent_manager,
                session_dir=self.session_logger.session_dir,
                launch_context=self.launch_context,
                interaction_requests=self._request_broker,
                subagent_runner=self._turn.subagent_runner,
                sampling_callback=self._sampling_handler,
                plan_file_path=self._plan_session.plan_file_path,
                switch_agent_callback=self.switch_agent,
                request_clear_context_callback=self._request_clear_context,
                skill_manager=self.skill_manager,
                is_skill_loaded=self._skill_already_loaded,
                scratchpad_dir=self.scratchpad_dir,
                permission_store=self._permission_store,
                hook_config_result=self._hook_config_result,
                session_id=self.session_id,
                mcp_pool=self._mcp_pool,
                tool_io=self._turn.tool_io,
            ),
            **tool_call.args_dict,
        ):
            if isinstance(item, ToolStreamEvent):
                yield item
            else:
                result_model = item

        duration = time.perf_counter() - start_time
        if result_model is None:
            raise ToolError("Tool did not yield a result")

        result_dict = result_model.model_dump(mode="json")
        text = "\n".join(f"{k}: {v}" for k, v in result_dict.items())
        extra = tool_instance.get_result_extra(result_model)
        if extra:
            text += "\n\n" + extra

        result_cancelled = (
            isinstance(result_model, CancellableToolResult) and result_model.cancelled
        )
        result_event = ToolResultEvent(
            tool_name=tool_call.tool_name,
            tool_class=tool_call.tool_class,
            result=result_model,
            cancelled=result_cancelled,
            duration=duration,
            tool_call_id=tool_call.call_id,
        )
        result_event = result_event.model_copy(
            update={
                "presentation": ToolUIDataAdapter(
                    tool_call.tool_class, harness_files=self.harness_files
                ).get_result_presentation(result_event)
            }
        )
        yield result_event
        async for ev in self._run_post_tool_and_finalize(
            tool_call,
            PostToolFinalization(
                tool_input=tool_input,
                tool_status="cancelled" if result_cancelled else "success",
                response_status="success",
                decision=decision,
                span=span,
                tool_output=result_dict,
                tool_presentation=result_event.presentation,
                duration_ms=duration * 1000.0,
                initial_text=text,
            ),
        ):
            yield ev
        self.stats.tool_calls_succeeded += 1
        logger.info(
            "Tool call completed tool=%s tool_call_id=%s duration_ms=%d outcome=%s",
            tool_call.tool_name,
            tool_call.call_id,
            int(duration * 1000),
            "cancelled" if result_cancelled else "success",
        )

    async def _should_execute_tool(
        self, tool: BaseTool, args: BaseModel, tool_call_id: str
    ) -> ToolDecision:
        async with self._permission_store.lock:
            tool_name = tool.get_name()
            config_perm = self.tool_manager.get_tool_config(tool_name).permission

            # The configured NEVER mode is an administrator/user hard boundary.
            # A tool may relax ordinary invocations (for example, allowing a
            # read-only screenshot without a prompt), but it must not turn a
            # disabled tool back on.
            if config_perm is ToolPermission.NEVER:
                return ToolDecision(
                    verdict=ToolExecutionResponse.SKIP,
                    approval_type=ToolPermission.NEVER,
                    feedback=f"Tool '{tool_name}' is permanently disabled",
                )

            ctx = tool.resolve_permission(args)

            if ctx is None:
                ctx = PermissionContext(permission=config_perm)

            # Auto-approval suppresses interactive ASK prompts; it must never
            # override an explicit hard denial from semantic tool policy.
            if ctx.permission is ToolPermission.NEVER:
                return ToolDecision(
                    verdict=ToolExecutionResponse.SKIP,
                    approval_type=ToolPermission.NEVER,
                    feedback=ctx.reason
                    or f"Tool '{tool_name}' is permanently disabled",
                )
            if self.bypass_tool_permissions:
                return ToolDecision(
                    verdict=ToolExecutionResponse.EXECUTE,
                    approval_type=ToolPermission.ALWAYS,
                )

            match ctx.permission:
                case ToolPermission.ALWAYS:
                    return ToolDecision(
                        verdict=ToolExecutionResponse.EXECUTE,
                        approval_type=ToolPermission.ALWAYS,
                    )
                case _:
                    uncovered = [
                        rp
                        for rp in ctx.required_permissions
                        if not self._permission_store.covers(tool_name, rp)
                    ]
                    if ctx.required_permissions and not uncovered:
                        return ToolDecision(
                            verdict=ToolExecutionResponse.EXECUTE,
                            approval_type=ToolPermission.ALWAYS,
                        )
                    return await self._ask_approval(
                        tool_name, args, tool_call_id, uncovered
                    )

    async def _ask_approval(
        self,
        tool_name: str,
        args: BaseModel,
        tool_call_id: str,
        required_permissions: list[RequiredPermission],
    ) -> ToolDecision:
        response, feedback = await self._request_broker.request_approval(
            tool_name, args, tool_call_id, required_permissions
        )

        match response:
            case ApprovalResponse.YES:
                verdict = ToolExecutionResponse.EXECUTE
            case _:
                verdict = ToolExecutionResponse.SKIP

        return ToolDecision(
            verdict=verdict, approval_type=ToolPermission.ASK, feedback=feedback
        )

    def _handle_tool_response(
        self,
        tool_call: ResolvedToolCall,
        text: str,
        status: Literal["success", "failure", "skipped"],
        decision: ToolDecision | None = None,
        result: dict[str, Any] | None = None,
        persisted_result: PersistedToolResult | None = None,
        span: trace.Span | None = None,
    ) -> None:
        message = LLMMessage.model_validate(
            self.format_handler.create_tool_response_message(tool_call, text)
        )
        self.messages.append(
            message.model_copy(update={"tool_result": persisted_result})
            if persisted_result is not None
            else message
        )
        if not self._is_subagent:
            self._pending_working_memory_records.append((
                tool_call.tool_name,
                tool_call.validated_args.model_dump_json(),
                status,
                text,
            ))

        if span is not None:
            set_tool_result(span, text)
        self.telemetry_client.send_tool_call_finished(
            tool_call=tool_call,
            agent_profile_name=self.agent_profile.name,
            model=self.config.get_active_model().alias,
            status=status,
            decision=decision,
            result=result,
            message_id=self._current_user_message_id,
        )

    def _flush_working_memory(self) -> None:
        if self._is_subagent or not self._pending_working_memory_records:
            self._pending_working_memory_records.clear()
            return
        working_context = list(self.messages)
        latest: LLMMessage | None = None
        for tool, action, status, result in self._pending_working_memory_records:
            latest = build_working_memory_message(
                working_context, tool=tool, action=action, status=status, result=result
            )
            working_context.append(latest)
        self._pending_working_memory_records.clear()
        if latest is not None:
            self.messages.append(latest)

    def _tool_failure_event(
        self,
        tool_call: ResolvedToolCall,
        error_msg: str,
        decision: ToolDecision | None = None,
        cancelled: bool = False,
        span: trace.Span | None = None,
    ) -> ToolResultEvent:
        """Create a ToolResultEvent for a failed tool and record the failure."""
        self._handle_tool_response(tool_call, error_msg, "failure", decision, span=span)
        return ToolResultEvent(
            tool_name=tool_call.tool_name,
            tool_class=tool_call.tool_class,
            error=error_msg,
            cancelled=cancelled,
            tool_call_id=tool_call.call_id,
        )

    def _messages_for_backend(
        self, messages: Sequence[LLMMessage], active_model: ModelConfig
    ) -> Sequence[LLMMessage]:
        messages = select_model_context(messages)
        if active_model.supports_images:
            return messages
        if not any(m.images for m in messages):
            return messages
        return [
            m.model_copy(update={"images": None}) if m.images else m for m in messages
        ]

    def count_history_images_unsupported_by_active_model(self) -> int:
        try:
            active_model = self.config.get_active_model()
        except ValueError:
            return 0
        if active_model.supports_images:
            return 0
        return sum(1 for m in self._current_model_context() if m.images)

    async def _complete(
        self,
        *,
        model: ModelConfig,
        messages: Sequence[LLMMessage],
        tools: list[AvailableTool] | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        call_type: TelemetryCallType | None,
        max_tokens: int | None = None,
        _allow_fallback: bool = True,
    ) -> LLMChunk:
        """Make one accounted, non-streaming model call.

        Sends request telemetry, calls the backend, updates stats, and maps
        backend errors. Does NOT append to self.messages, check for refusal, or
        log success — those are the caller's concern. This is the single path
        every non-streaming call (including compaction) goes through, so usage
        accounting can never be skipped.
        """
        if _allow_fallback:
            selected_model = await self._preflight_model(model)
            if selected_model.alias != model.alias:
                return await self._complete(
                    model=selected_model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    call_type=call_type,
                    max_tokens=max_tokens,
                    _allow_fallback=False,
                )

        provider = self.config.get_provider_for_model(model)
        backend_metadata = self._build_backend_metadata(call_type)
        backend_messages = self._messages_for_backend(messages, model)

        last_user_message = next(
            (
                m
                for m in reversed(backend_messages)
                if m.role == Role.user and not m.injected
            ),
            None,
        )
        self.telemetry_client.send_request_sent(
            model=model.alias,
            nb_context_chars=sum(len(m.content or "") for m in backend_messages),
            nb_context_messages=len(backend_messages),
            nb_prompt_chars=len(last_user_message.content or "")
            if last_user_message
            else 0,
            call_type=backend_metadata.call_type,
            message_id=backend_metadata.message_id,
            attachment_counts=build_attachment_counts(
                last_user_message, supports_images=model.supports_images
            ),
        )

        start_time = time.perf_counter()
        try:
            logger.debug(
                "Model call starting model=%s provider=%s messages=%d tools=%d thinking=%s",
                model.alias,
                provider.name,
                len(backend_messages),
                len(tools) if tools else 0,
                model.thinking,
            )
            async with self._backend_for_call(model) as backend:
                result = await backend.complete(
                    model=model,
                    messages=backend_messages,
                    temperature=model.temperature,
                    tools=tools,
                    tool_choice=tool_choice,
                    extra_headers=self._get_extra_headers(provider),
                    max_tokens=max_tokens
                    if max_tokens is not None
                    else self._max_tokens,
                    metadata=backend_metadata.model_dump(exclude_none=True),
                )
            end_time = time.perf_counter()

            if result.usage is None:
                raise AgentLoopLLMResponseError(
                    "Usage data missing in non-streaming completion response"
                )
            self._update_stats(usage=result.usage, time_seconds=end_time - start_time)

            if result.correlation_id:
                self.telemetry_client.last_correlation_id = result.correlation_id

            processed_message = self.format_handler.process_api_response_message(
                result.message
            )
            return LLMChunk(
                message=processed_message, usage=result.usage, stop=result.stop
            )

        except Exception as e:
            _log_model_call_failure(
                model.alias,
                provider.name,
                e,
                int((time.perf_counter() - start_time) * 1000),
            )
            if isinstance(e, RefusalError):
                raise
            if (
                _allow_fallback
                and (fallback := await self._fallback_after_error(model, e)) is not None
            ):
                return await self._complete(
                    model=fallback,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    call_type=call_type,
                    max_tokens=max_tokens,
                    _allow_fallback=False,
                )
            if _should_raise_rate_limit_error(e):
                raise RateLimitError(provider.name, model.name) from e
            if _is_context_too_long_error(e):
                raise ContextTooLongError(provider.name, model.name) from e
            if _is_response_too_long_error(e):
                raise ResponseTooLongError(provider.name, model.name) from e
            if isinstance(e, BackendError) and e.is_invalid_model:
                raise
            if _is_non_retryable_error(e):
                raise

            raise RuntimeError(
                f"API error from {provider.name} (model: {model.name}): {e}"
            ) from e

    async def _chat(
        self,
        model_override: ModelConfig | None = None,
        *,
        call_type: TelemetryCallType | None = None,
    ) -> LLMChunk:
        active_model = model_override or self.config.get_active_model()
        provider = self.config.get_provider_for_model(active_model)
        start_time = time.perf_counter()
        result = await self._complete(
            model=active_model,
            messages=self.messages,
            tools=self.format_handler.get_available_tools(self.tool_manager),
            tool_choice=self.format_handler.get_tool_choice(),
            call_type=call_type,
        )
        self.messages.append(result.message)
        if result.stop and result.stop.is_refusal:
            raise _refusal_error(provider.name, active_model.name, result)
        _usage = result.usage
        log_model_call_success(
            active_model.alias,
            int((time.perf_counter() - start_time) * 1000),
            prompt_tokens=_usage.prompt_tokens if _usage else 0,
            completion_tokens=_usage.completion_tokens if _usage else 0,
            cached_tokens=_usage.cached_tokens if _usage else 0,
        )
        return result

    async def _chat_streaming(  # noqa: PLR0912, PLR0915
        self, model_override: ModelConfig | None = None, *, _allow_fallback: bool = True
    ) -> AsyncGenerator[LLMChunk]:
        active_model = model_override or self.config.get_active_model()
        if _allow_fallback:
            selected_model = await self._preflight_model(active_model)
            if selected_model.alias != active_model.alias:
                async for chunk in self._chat_streaming(
                    selected_model, _allow_fallback=False
                ):
                    yield chunk
                return

        provider = self.config.get_provider_for_model(active_model)
        backend_metadata = self._build_backend_metadata()

        available_tools = self.format_handler.get_available_tools(self.tool_manager)
        tool_choice = self.format_handler.get_tool_choice()
        backend_messages = self._messages_for_backend(self.messages, active_model)

        last_user_message = self._last_user_message_from(backend_messages)
        self.telemetry_client.send_request_sent(
            model=active_model.alias,
            nb_context_chars=sum(len(m.content or "") for m in backend_messages),
            nb_context_messages=len(backend_messages),
            nb_prompt_chars=len(last_user_message.content or "")
            if last_user_message
            else 0,
            call_type=backend_metadata.call_type,
            message_id=backend_metadata.message_id,
            attachment_counts=build_attachment_counts(
                last_user_message, supports_images=active_model.supports_images
            ),
        )

        chunk_agg: LLMChunk | None = None
        start_time = time.perf_counter()
        try:
            logger.debug(
                "Model call starting model=%s provider=%s messages=%d tools=%d streaming=%s",
                active_model.alias,
                provider.name,
                len(backend_messages),
                len(available_tools) if available_tools else 0,
                True,
            )
            usage = LLMUsage()
            async with self._backend_for_call(active_model) as backend:
                async for chunk in backend.complete_streaming(
                    model=active_model,
                    messages=backend_messages,
                    temperature=active_model.temperature,
                    tools=available_tools,
                    tool_choice=tool_choice,
                    extra_headers=self._get_extra_headers(provider),
                    max_tokens=self._max_tokens,
                    metadata=backend_metadata.model_dump(exclude_none=True),
                ):
                    if chunk.correlation_id:
                        self.telemetry_client.last_correlation_id = chunk.correlation_id
                    processed_message = (
                        self.format_handler.process_api_response_message(chunk.message)
                    )
                    processed_chunk = LLMChunk(
                        message=processed_message, usage=chunk.usage, stop=chunk.stop
                    )
                    chunk_agg = (
                        processed_chunk
                        if chunk_agg is None
                        else chunk_agg + processed_chunk
                    )
                    usage += chunk.usage or LLMUsage()
                    yield processed_chunk
            end_time = time.perf_counter()

            if chunk_agg is None or (
                provider.emits_finish_reason and chunk_agg.stop is None
            ):
                raise IncompleteStreamError(provider.name, active_model.name)
            aggregate_message = chunk_agg.message
            if (
                (aggregate_message.reasoning_content or "").strip()
                and not (aggregate_message.content or "").strip()
                and not aggregate_message.tool_calls
            ):
                raise IncompleteStreamError(
                    provider.name,
                    active_model.name,
                    reason="a final answer or tool call after reasoning",
                )
            if chunk_agg.usage is None:
                raise AgentLoopLLMResponseError(
                    "Usage data missing in final chunk of streamed completion"
                )
            self._update_stats(usage=usage, time_seconds=end_time - start_time)

            self.messages.append(chunk_agg.message)
            if chunk_agg.stop and chunk_agg.stop.is_refusal:
                raise _refusal_error(provider.name, active_model.name, chunk_agg)

            log_model_call_success(
                active_model.alias,
                int((end_time - start_time) * 1000),
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                cached_tokens=usage.cached_tokens if usage else 0,
            )

        except Exception as e:
            _log_model_call_failure(
                active_model.alias,
                provider.name,
                e,
                int((time.perf_counter() - start_time) * 1000),
            )
            if isinstance(e, RefusalError):
                raise
            if (
                _allow_fallback
                and chunk_agg is None
                and (fallback := await self._fallback_after_error(active_model, e))
                is not None
            ):
                async for chunk in self._chat_streaming(
                    fallback, _allow_fallback=False
                ):
                    yield chunk
                return
            if isinstance(e, BackendError | IncompleteStreamError):
                self._record_interrupted_assistant(chunk_agg)
            if isinstance(e, IncompleteStreamError):
                raise
            if _should_raise_rate_limit_error(e):
                raise RateLimitError(provider.name, active_model.name) from e
            if _is_context_too_long_error(e):
                raise ContextTooLongError(provider.name, active_model.name) from e
            if _is_response_too_long_error(e):
                raise ResponseTooLongError(provider.name, active_model.name) from e
            if isinstance(e, BackendError) and e.is_invalid_model:
                raise
            if _is_non_retryable_error(e):
                raise

            raise RuntimeError(
                f"API error from {provider.name} (model: {active_model.name}): {e}"
            ) from e

    def _record_interrupted_assistant(self, chunk: LLMChunk | None) -> None:
        if chunk is None or not chunk.message.content:
            return
        self.messages.append(
            LLMMessage(
                role=Role.assistant,
                content=chunk.message.content,
                message_id=chunk.message.message_id,
            )
        )

    def _update_stats(self, usage: LLMUsage, time_seconds: float) -> None:
        self.stats.last_turn_duration = time_seconds
        self.stats.last_turn_prompt_tokens = usage.prompt_tokens
        self.stats.last_turn_completion_tokens = usage.completion_tokens
        self.stats.last_turn_cached_tokens = usage.cached_tokens
        self.stats.session_prompt_tokens += usage.prompt_tokens
        self.stats.session_completion_tokens += usage.completion_tokens
        self.stats.session_cached_tokens += usage.cached_tokens
        self.stats.context_tokens = usage.prompt_tokens + usage.completion_tokens
        if time_seconds > 0 and usage.completion_tokens > 0:
            self.stats.tokens_per_second = usage.completion_tokens / time_seconds

    def _clean_message_history(self) -> None:
        ACCEPTABLE_HISTORY_SIZE = 2
        if len(self.messages) < ACCEPTABLE_HISTORY_SIZE:
            return
        self._fill_missing_tool_responses()

    def _fill_missing_tool_responses(self) -> None:
        i = 1
        while i < len(self.messages):  # noqa: PLR1702
            msg = self.messages[i]

            if msg.role == "assistant" and msg.tool_calls:
                expected_responses = len(msg.tool_calls)

                if expected_responses > 0:
                    responded_ids: set[str] = set()
                    j = i + 1
                    while j < len(self.messages) and self.messages[j].role == "tool":
                        tool_call_id = self.messages[j].tool_call_id
                        if tool_call_id is not None:
                            responded_ids.add(tool_call_id)
                        j += 1

                    if len(responded_ids) < expected_responses:
                        insertion_point = j

                        for tool_call_data in msg.tool_calls:
                            if (tool_call_data.id or "") in responded_ids:
                                continue

                            empty_response = LLMMessage(
                                role=Role.tool,
                                tool_call_id=tool_call_data.id or "",
                                name=(
                                    (tool_call_data.function.name or "")
                                    if tool_call_data.function
                                    else ""
                                ),
                                content=str(
                                    get_user_cancellation_message(
                                        CancellationReason.TOOL_NO_RESPONSE
                                    )
                                ),
                            )

                            self.messages.insert(insertion_point, empty_response)
                            insertion_point += 1

                    i = i + 1 + expected_responses
                    continue

            i += 1

    async def _reset_session(self, keep_parent: bool = True) -> None:
        old_session_id = self.session_id
        self.emit_session_closed_telemetry()
        suffix = extract_suffix(self.session_id)
        session_id = generate_session_id(suffix=suffix)
        lease_root = (
            self._session_lease.path.parent.parent
            if self._session_lease is not None
            else Path(self.config.session_logging.save_dir)
        )
        lease = (
            await asyncio.to_thread(SessionLease(lease_root, session_id).acquire)
            if self.config.session_logging.enabled
            else None
        )
        parent_session_id = (
            self.parent_session_id
            if keep_parent and self._is_subagent
            else old_session_id
            if keep_parent
            else None
        )
        try:
            self.session_logger.reset_session(
                session_id, parent_session_id=parent_session_id
            )
        except BaseException:
            if lease is not None:
                await asyncio.to_thread(lease.release)
            raise
        self.session_id = session_id
        self.parent_session_id = parent_session_id
        if not self._is_subagent:
            self._model_failover_state.reset()
        self.replace_session_lease(lease)
        await self.initialize_experiments()
        self.emit_new_session_telemetry()

    def replace_session_lease(self, lease: SessionLease | None) -> None:
        previous = self._session_lease
        self._session_lease = lease
        if previous is not None:
            previous.release()

    def rebind_to_session(
        self,
        session_id: str,
        session_dir: Path,
        loaded_messages: list[LLMMessage],
        *,
        session_metadata: SessionMetadata,
        parent_session_id: str | None = None,
        stats: AgentStats | None = None,
    ) -> None:
        """Swap session identity in-place, reusing expensive runtime infrastructure.

        Kept (session-independent): MCP/connector pools, the tool and skill
        registries, git context, config, and the backend client.

        Reimported from the resumed session: session ID, parent, message history,
        stats, and the session-logger binding. Experiment variants are reapplied
        separately via ``hydrate_experiments_from_session`` after this returns.

        Reset so nothing leaks across the session boundary: tool-permission
        approvals, checkpoint/rewind state, the plan session, per-turn middleware
        and tool state, and the scratchpad directory.

        Atomicity: the only intentionally fail-able work (scratchpad creation)
        runs before any mutation. The commit section is designed to be infallible
        — pure assignments and resets — so a resume either fully applies or leaves
        the loop untouched. A bug in any commit step would leave the loop
        half-rebound; callers should treat an unexpected raise as fatal.
        """
        # Prepare — no mutation of the live loop.
        previous_scratchpad = self.scratchpad_dir
        scratchpad_dir = None if self._is_subagent else init_scratchpad(session_id)

        # Commit — assignments and in-place resets only, from here on infallible.
        self._cancel_experiments_task()
        self.session_id = session_id
        self.parent_session_id = parent_session_id
        self.scratchpad_dir = scratchpad_dir
        self.session_logger.apply_resumed_session(
            session_id, session_dir, session_metadata
        )
        # Atomically preserve any system prompt the deferred-init thread may
        # have inserted between snapshot and commit, instead of snapshotting
        # system messages outside the lock and racing update_system_prompt.
        self.messages.reset_preserving_system(loaded_messages)
        if stats is not None:
            self.stats = stats
        else:
            self.stats = AgentStats.create_fresh(self.stats)
            self._apply_active_model_pricing()
        self._reset_session_scoped_state()
        self._apply_active_model_pricing()
        cleanup_scratchpad(previous_scratchpad)

    def _cancel_experiments_task(self) -> None:
        # A fresh session (opened for ``--resume`` before the picker) may still
        # be evaluating experiments; drop it so it cannot overwrite the resumed
        # session's hydrated variants or persist a fresh evaluation onto them.
        # Clear _await_experiment_model so awaiting_experiment_model returns False
        # immediately — the rebind discards this init lifecycle entirely.
        self._await_experiment_model = False
        task = self._experiments_task
        self._experiments_task = None
        if task is not None and not task.done():
            task.cancel()

    def _apply_active_model_pricing(self) -> None:
        try:
            active_model = self.config.get_active_model()
        except ValueError:
            return
        self.stats.update_pricing(
            active_model.input_price,
            active_model.output_price,
            active_model.cached_input_price,
        )

    def _emit_deferred_new_session_telemetry(self) -> None:
        # The picker's throwaway session deferred its new-session event; if it is
        # actually used (picker cancelled/emptied), emit it now, exactly once.
        if self._deferred_new_session_telemetry:
            self._deferred_new_session_telemetry = False
            self.emit_new_session_telemetry()

    def _reset_session_scoped_state(self) -> None:
        # A resume discards the fresh picker session; none of its pending
        # telemetry events must fire against the rebound (resumed) session.
        self._deferred_new_session_telemetry = False
        self._pending_new_session_telemetry = False
        self._ready_telemetry_pending = False
        # Clear any duration the picker recorded so it doesn't leak into the
        # resumed session. ``_init_start_time`` is intentionally kept: the
        # metric measures ``__init__ -> ready``.
        self._last_init_duration_ms = None
        self._permission_store.reset()
        self.checkpoint_recorder.reset()
        self.middleware_pipeline.reset()
        self.tool_manager.reset_all()
        self._plan_session = PlanSession()
        self._user_plan = None
        self._teleport_service = None
        self._pending_injected_messages = []
        self._pending_working_memory_records.clear()
        self._automatic_web_queries.clear()
        self._pending_clear_context = False
        self._current_user_message_id = None
        self._is_user_prompt_call = False
        self._reactive_recovery_used = False
        self._autonomy_advisor_completed = False
        self._autonomy_bootstrap_blocked = False
        self._autonomy_coordinator = None
        self._autonomy_evidence = []
        self._autonomy_objective = ""
        self._autonomy_plan = None
        self._autonomy_todos = []
        self._autonomy_completed_tasks = set()
        self._autonomy_task_results = {}
        self._autonomy_scheduler_needs_resume = False
        self._automatic_todo_write_succeeded = False
        if not self._is_subagent:
            self._model_failover_state.reset()

    @requires_init
    async def clear_history(self) -> None:
        await self.session_logger.save_interaction(
            self.messages,
            self.stats,
            self.config,
            self.tool_manager,
            self.agent_profile,
        )
        self.messages.reset(self.messages[:1])

        self.stats = AgentStats.create_fresh(self.stats)
        self.stats.trigger_listeners()
        self._apply_active_model_pricing()

        self.middleware_pipeline.reset()
        self.tool_manager.reset_all()
        await self._reset_session(keep_parent=False)

    @requires_init
    async def compact(self, extra_instructions: str = "") -> str:
        # Summary generation and the context boundary live in the manager; the loop
        # keeps the surrounding lifecycle (clean history, save, middleware).
        try:
            self._clean_message_history()
            await self._save_messages()
            summary = await self.compaction_manager.compact(extra_instructions)
            self.middleware_pipeline.reset(reset_reason=ResetReason.COMPACT)
            return summary
        except Exception:
            await self._save_messages()
            raise

    async def _request_clear_context(self) -> None:
        """Signal that the context should be cleared at the next turn boundary.

        The actual clear is deferred so the in-flight tool turn can finish
        appending its tool-result before ``self.messages`` is wiped.
        """
        self._pending_clear_context = True

    async def _clear_context_after_plan_accept(self) -> AsyncGenerator[BaseEvent, None]:
        """Clear the conversation after a plan accept, then re-seed the plan.

        Runs at a turn boundary (never mid-tool) so the tool-result message has
        already landed before ``self.messages`` is reset. The approved plan is
        re-injected as a system-reminder so the implementing agent keeps going,
        and emitted in a ``ContextClearedEvent`` with its path so the UI can keep
        the plan on screen.
        """
        self._pending_clear_context = False
        self._pending_injected_messages.clear()
        plan_content = self._plan_session.read()
        await self.clear_history()
        if not plan_content:
            yield ContextClearedEvent(plan_file_path=None)
            return
        self.messages.append(
            LLMMessage(
                role=Role.user,
                content=(
                    f"<{VIBE_WARNING_TAG}>The conversation context was cleared "
                    f"after the plan was approved. Implement the approved plan "
                    f"below -- it is the source of truth:\n\n{plan_content}"
                    f"</{VIBE_WARNING_TAG}>"
                ),
                injected=True,
            )
        )
        yield ContextClearedEvent(plan_file_path=self._plan_session.plan_file_path)

    @requires_init
    async def switch_agent(self, agent_name: str) -> None:
        if agent_name == self.agent_profile.name:
            return
        await self.reload_with_initial_messages(
            reset_middleware=False, switch_to_agent=agent_name
        )

    @requires_init
    async def reload_with_initial_messages(
        self,
        max_turns: int | None = None,
        max_price: float | None = None,
        reset_middleware: bool = True,
        switch_to_agent: str | None = None,
        reload_hooks: bool = False,
    ) -> None:
        self._reload_generation += 1
        generation = self._reload_generation

        await self.session_logger.save_interaction(
            self.messages,
            self.stats,
            self.config,
            self.tool_manager,
            self.agent_profile,
        )

        # A newer reload superseded us while we were saving; don't mutate state.
        if generation != self._reload_generation:
            return

        if max_turns is not None:
            self._max_turns = max_turns
        if max_price is not None:
            self._max_price = max_price
        self._ensure_remote_registries()

        # Resolve the config the reloaded objects should reflect. For an agent switch
        # this is the target agent's config, computed without mutating the active
        # profile -- so an in-flight turn keeps seeing the old, self-consistent config
        # until the synchronous commit flips profile and objects together.
        target_config = (
            self.agent_manager.preview_config(switch_to_agent)
            if switch_to_agent is not None
            else self.config
        )

        # Off-loop: skill discovery and system prompt I/O. reload() is awaited within a
        # turn, so that turn is suspended here -- nothing mutates the shared state this
        # reads, and the new objects are built locally before the commit below.
        prepared = await asyncio.to_thread(
            self._prepare_reload, target_config, reload_hooks
        )

        # A newer reload superseded us; let it own the commit.
        if generation != self._reload_generation:
            return

        # Synchronous swap: no await, so an in-flight turn can't observe a partial
        # update. Keep it that way -- don't make it async or move it off-thread.
        self._commit_reload(prepared, reset_middleware, switch_to_agent)

    def _prepare_reload(
        self, target_config: VibeConfigSchema, reload_hooks: bool
    ) -> _PreparedReload:
        config_source = _SwappableConfigSource(lambda: target_config)
        tool_manager = ToolManager(
            config_source.get,
            mcp_registry=self.mcp_registry,
            connector_registry=self.connector_registry,
            permission_getter=self._permission_store.get_tool_permission,
            local_managed_shell_runtime_enabled=self._local_managed_shell_runtime_enabled,
            cwd=self.cwd,
            harness_files=self.harness_files,
            scratchpad_dir=self.scratchpad_dir,
            terminal_runtime=self.tool_manager.terminal_runtime,
        )
        skill_manager = SkillManager(
            config_source.get, harness_files=self.harness_files
        )
        system_prompt = self._render_system_prompt(
            skill_manager, target_config, tool_manager
        )
        hook_config_result = (
            load_hooks_from_fs(harness_files=self.harness_files)
            if reload_hooks
            else self._hook_config_result
        )
        return _PreparedReload(
            backend=self.backend_factory(target_config),
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            system_prompt=system_prompt,
            config_source=config_source,
            hook_config_result=hook_config_result,
        )

    def _commit_reload(
        self,
        prepared: _PreparedReload,
        reset_middleware: bool,
        switch_to_agent: str | None,
    ) -> None:
        if switch_to_agent is not None:
            self.agent_manager.switch_profile(switch_to_agent)
        # Now that the profile is live, the prepared managers should track the live
        # config so later refreshes (refresh_config) propagate to them.
        prepared.config_source.point_to(lambda: self.config)
        self.backend = prepared.backend
        self.tool_manager = prepared.tool_manager
        self.skill_manager = prepared.skill_manager
        self.messages.update_system_prompt(prepared.system_prompt)
        self._hook_config_result = prepared.hook_config_result
        self._hooks_manager = (
            HooksManager(prepared.hook_config_result.hooks, cwd=self.cwd)
            if prepared.hook_config_result is not None
            else None
        )
        self.hook_config_issues = (
            prepared.hook_config_result.issues
            if prepared.hook_config_result is not None
            else []
        )
        self.hooks_count = (
            len(prepared.hook_config_result.hooks)
            if prepared.hook_config_result is not None
            else 0
        )

        if len(self.messages) == 1:
            self.stats.reset_context_state()

        try:
            active_model = self.config.get_active_model()
            self.stats.update_pricing(
                active_model.input_price,
                active_model.output_price,
                active_model.cached_input_price,
            )
        except ValueError:
            pass

        if reset_middleware:
            self._setup_middleware()
