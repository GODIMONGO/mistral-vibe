from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum, auto
from types import MappingProxyType


class HarnessPhase(StrEnum):
    """Stable lifecycle seams exposed by the agent harness."""

    IDLE = auto()
    TURN_START = auto()
    PRE_STEP = auto()
    MODEL_REQUEST = auto()
    TOOL_DISPATCH = auto()
    TOOL_RESULT = auto()
    TURN_STOPPING = auto()
    TURN_END = auto()
    COMPACTION = auto()
    CLOSED = auto()


class HarnessCapability(StrEnum):
    """Composable services that may be contributed by harness plugins."""

    AGENT_LOOP = auto()
    SESSION_LOG = auto()
    SYSTEM_PROMPT = auto()
    MODEL_BACKEND = auto()
    STREAMING = auto()
    TOOLS = auto()
    PERMISSIONS = auto()
    SUBAGENTS = auto()
    SKILLS = auto()
    HOOKS = auto()
    MCP = auto()
    CONNECTORS = auto()
    COMPACTION = auto()
    FAST_MEMORY = auto()
    DURABLE_MEMORY = auto()
    WEB_RESEARCH = auto()
    PLANNING = auto()
    REVIEW = auto()
    CHECKPOINTS = auto()


@dataclass(frozen=True, slots=True)
class HarnessEvent:
    """Dependency-neutral input passed through one harness phase waterfall."""

    phase: HarnessPhase
    session_id: str
    sequence: int
    step: int = 0
    objective: str = ""
    tool_name: str | None = None
    outcome: str | None = None
    context: tuple[str, ...] = ()
    metadata: Mapping[str, str | int | float | bool | None] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def with_context(self, *items: str) -> HarnessEvent:
        clean = tuple(item.strip() for item in items if item.strip())
        return replace(self, context=(*self.context, *clean))


@dataclass(frozen=True, slots=True)
class HarnessDecision:
    """Result of a phase waterfall.

    ``continue_prompt`` is meaningful for ``TURN_STOPPING`` and lets a plugin
    require one more evidence-gathering step without owning the agent loop.
    ``stop_reason`` fails the phase closed and is surfaced by the caller.
    """

    event: HarnessEvent
    continue_prompt: str | None = None
    stop_reason: str | None = None


type HarnessNext = Callable[[HarnessEvent], Awaitable[HarnessDecision]]
type HarnessInterceptor = Callable[
    [HarnessEvent, HarnessNext], Awaitable[HarnessDecision]
]


@dataclass(frozen=True, slots=True)
class HarnessPlugin:
    name: str
    capabilities: frozenset[HarnessCapability]
    interceptors: Mapping[HarnessPhase, HarnessInterceptor] = field(
        default_factory=lambda: MappingProxyType({}), compare=False, repr=False
    )
    version: str = "builtin"
    priority: int = 100

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ValueError("Harness plugin name must be non-empty and normalized")


@dataclass(frozen=True, slots=True)
class HarnessSnapshot:
    phase: HarnessPhase
    sequence: int
    plugins: tuple[HarnessPlugin, ...]
    capabilities: frozenset[HarnessCapability]


class HarnessRuntime:
    """Session-scoped plugin runtime around the replaceable agent driver.

    Registrations are deterministic and reversible. Interceptors form a
    middleware waterfall: each interceptor must delegate exactly once by
    calling ``next(event)``. This keeps orchestration policy outside the model
    backend and makes the same runtime usable by TUI, ACP, and programmatic
    clients.
    """

    def __init__(self, plugins: Sequence[HarnessPlugin] = ()) -> None:
        self._plugins: OrderedDict[str, HarnessPlugin] = OrderedDict()
        self._phase = HarnessPhase.IDLE
        self._sequence = 0
        for plugin in plugins:
            self.register(plugin)

    @property
    def phase(self) -> HarnessPhase:
        return self._phase

    def register(self, plugin: HarnessPlugin) -> Callable[[], None]:
        if plugin.name in self._plugins:
            raise ValueError(f"Harness plugin already registered: {plugin.name}")
        self._plugins[plugin.name] = plugin
        self._plugins = OrderedDict(
            sorted(self._plugins.items(), key=lambda item: (item[1].priority, item[0]))
        )

        def dispose() -> None:
            self._plugins.pop(plugin.name, None)

        return dispose

    def snapshot(self) -> HarnessSnapshot:
        plugins = tuple(self._plugins.values())
        return HarnessSnapshot(
            phase=self._phase,
            sequence=self._sequence,
            plugins=plugins,
            capabilities=frozenset(
                capability for plugin in plugins for capability in plugin.capabilities
            ),
        )

    async def run(
        self,
        phase: HarnessPhase,
        *,
        session_id: str,
        step: int = 0,
        objective: str = "",
        tool_name: str | None = None,
        outcome: str | None = None,
        metadata: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> HarnessDecision:
        if self._phase is HarnessPhase.CLOSED:
            raise RuntimeError("Harness runtime is closed")
        self._sequence += 1
        self._phase = phase
        event = HarnessEvent(
            phase=phase,
            session_id=session_id,
            sequence=self._sequence,
            step=step,
            objective=objective,
            tool_name=tool_name,
            outcome=outcome,
            metadata=MappingProxyType(dict(metadata or {})),
        )
        interceptors = tuple(
            interceptor
            for plugin in self._plugins.values()
            if (interceptor := plugin.interceptors.get(phase)) is not None
        )

        async def dispatch(index: int, current: HarnessEvent) -> HarnessDecision:
            if index == len(interceptors):
                return HarnessDecision(event=current)
            called = False

            async def call_next(next_event: HarnessEvent) -> HarnessDecision:
                nonlocal called
                if called:
                    raise RuntimeError(
                        "Harness interceptor called next() more than once"
                    )
                called = True
                if next_event.phase is not phase:
                    raise ValueError("Harness interceptor cannot change the phase")
                return await dispatch(index + 1, next_event)

            decision = await interceptors[index](current, call_next)
            if not called and decision.stop_reason is None:
                raise RuntimeError(
                    "Harness interceptor must call next() or return a stop_reason"
                )
            return decision

        return await dispatch(0, event)

    def finish_turn(self) -> None:
        if self._phase is not HarnessPhase.CLOSED:
            self._phase = HarnessPhase.IDLE

    def close(self) -> None:
        self._phase = HarnessPhase.CLOSED
        self._plugins.clear()


def create_default_harness(
    extra_plugins: Sequence[HarnessPlugin] = (),
) -> HarnessRuntime:
    """Compose Powerstral's default harness from explicit capability plugins."""
    builtin_plugins = (
        HarnessPlugin(
            name="session",
            priority=10,
            capabilities=frozenset({
                HarnessCapability.SESSION_LOG,
                HarnessCapability.CHECKPOINTS,
            }),
        ),
        HarnessPlugin(
            name="system-prompt",
            priority=20,
            capabilities=frozenset({
                HarnessCapability.SYSTEM_PROMPT,
                HarnessCapability.SKILLS,
            }),
        ),
        HarnessPlugin(
            name="model",
            priority=30,
            capabilities=frozenset({
                HarnessCapability.MODEL_BACKEND,
                HarnessCapability.STREAMING,
            }),
        ),
        HarnessPlugin(
            name="tools",
            priority=40,
            capabilities=frozenset({
                HarnessCapability.TOOLS,
                HarnessCapability.PERMISSIONS,
                HarnessCapability.MCP,
                HarnessCapability.CONNECTORS,
            }),
        ),
        HarnessPlugin(
            name="orchestration",
            priority=50,
            capabilities=frozenset({
                HarnessCapability.AGENT_LOOP,
                HarnessCapability.PLANNING,
                HarnessCapability.SUBAGENTS,
                HarnessCapability.REVIEW,
            }),
        ),
        HarnessPlugin(
            name="context",
            priority=60,
            capabilities=frozenset({
                HarnessCapability.COMPACTION,
                HarnessCapability.FAST_MEMORY,
                HarnessCapability.DURABLE_MEMORY,
                HarnessCapability.WEB_RESEARCH,
            }),
        ),
        HarnessPlugin(
            name="hooks", priority=70, capabilities=frozenset({HarnessCapability.HOOKS})
        ),
    )
    return HarnessRuntime((*builtin_plugins, *extra_plugins))
