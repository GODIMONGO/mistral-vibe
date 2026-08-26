from __future__ import annotations

from vibe.app_server._projection import project_harness
from vibe.core.harness import HarnessCapability, HarnessPhase, create_default_harness


def test_harness_projection_is_backend_neutral_and_complete() -> None:
    runtime = create_default_harness()

    view = project_harness(runtime)

    assert view.phase == HarnessPhase.IDLE.value
    assert HarnessCapability.AGENT_LOOP.value in view.capabilities
    assert HarnessCapability.SUBAGENTS.value in view.capabilities
    assert [plugin.name for plugin in view.plugins] == [
        "session",
        "system-prompt",
        "model",
        "tools",
        "orchestration",
        "context",
        "hooks",
    ]
    assert all(plugin.version == "builtin" for plugin in view.plugins)
