from __future__ import annotations

from types import MappingProxyType

import pytest

from vibe.core.harness import (
    HarnessCapability,
    HarnessDecision,
    HarnessEvent,
    HarnessPhase,
    HarnessPlugin,
    HarnessRuntime,
    create_default_harness,
)


@pytest.mark.asyncio
async def test_interceptors_form_priority_ordered_waterfall() -> None:
    calls: list[str] = []

    def plugin(name: str, priority: int) -> HarnessPlugin:
        async def intercept(event: HarnessEvent, next_call):
            calls.append(f"{name}:before")
            decision = await next_call(event.with_context(name))
            calls.append(f"{name}:after")
            return decision

        return HarnessPlugin(
            name=name,
            priority=priority,
            capabilities=frozenset({HarnessCapability.AGENT_LOOP}),
            interceptors=MappingProxyType({HarnessPhase.PRE_STEP: intercept}),
        )

    runtime = HarnessRuntime((plugin("late", 20), plugin("early", 10)))

    decision = await runtime.run(
        HarnessPhase.PRE_STEP, session_id="session", objective="fix tests"
    )

    assert calls == ["early:before", "late:before", "late:after", "early:after"]
    assert decision.event.context == ("early", "late")


@pytest.mark.asyncio
async def test_interceptor_must_delegate_or_stop() -> None:
    async def broken(event: HarnessEvent, _next_call):
        return HarnessDecision(event=event)

    runtime = HarnessRuntime((
        HarnessPlugin(
            name="broken",
            capabilities=frozenset(),
            interceptors=MappingProxyType({HarnessPhase.PRE_STEP: broken}),
        ),
    ))

    with pytest.raises(RuntimeError, match=r"must call next\(\)"):
        await runtime.run(HarnessPhase.PRE_STEP, session_id="session")


@pytest.mark.asyncio
async def test_stopping_interceptor_can_require_another_agent_step() -> None:
    async def gate(event: HarnessEvent, next_call):
        downstream = await next_call(event)
        return HarnessDecision(
            event=downstream.event, continue_prompt="Run the missing verification."
        )

    runtime = HarnessRuntime((
        HarnessPlugin(
            name="verification-gate",
            capabilities=frozenset({HarnessCapability.REVIEW}),
            interceptors=MappingProxyType({HarnessPhase.TURN_STOPPING: gate}),
        ),
    ))

    decision = await runtime.run(HarnessPhase.TURN_STOPPING, session_id="session")

    assert decision.continue_prompt == "Run the missing verification."


def test_default_harness_exposes_composable_agent_capabilities() -> None:
    snapshot = create_default_harness().snapshot()

    assert {plugin.name for plugin in snapshot.plugins} == {
        "session",
        "system-prompt",
        "model",
        "tools",
        "orchestration",
        "context",
        "hooks",
    }
    assert HarnessCapability.AGENT_LOOP in snapshot.capabilities
    assert HarnessCapability.SUBAGENTS in snapshot.capabilities
    assert HarnessCapability.DURABLE_MEMORY in snapshot.capabilities
    assert HarnessCapability.REVIEW in snapshot.capabilities


def test_registration_is_reversible_and_duplicate_names_fail() -> None:
    runtime = HarnessRuntime()
    plugin = HarnessPlugin(name="custom", capabilities=frozenset())

    dispose = runtime.register(plugin)
    with pytest.raises(ValueError, match="already registered"):
        runtime.register(plugin)

    dispose()

    assert runtime.snapshot().plugins == ()
