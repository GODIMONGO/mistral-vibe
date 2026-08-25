from __future__ import annotations

from typing import TYPE_CHECKING

from vibe.core.agents.diagnostics import excluded_agent_message
from vibe.core.agents.models import (
    BUILTIN_AGENTS,
    AgentProfile,
    AgentType,
    BuiltinAgentName,
)
from vibe.core.agents.registry import AgentRegistry, apply_profile_overrides
from vibe.core.config.harness_files import (
    HarnessFilesManager,
    get_harness_files_manager,
)
from vibe.core.config.layers.agent_profile import AgentProfileLayer
from vibe.core.config.orchestrator import ConfigOrchestrator
from vibe.core.utils import name_matches
from vibe.observability.logging import logger

if TYPE_CHECKING:
    from vibe.core.config import VibeConfigSchema


class AgentManager:
    def __init__(
        self,
        orchestrator: ConfigOrchestrator[VibeConfigSchema],
        initial_agent: str = BuiltinAgentName.ACCEPT_EDITS,
        allow_subagent: bool = False,
        harness_files: HarnessFilesManager | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._registry = AgentRegistry(
            orchestrator, harness_files or get_harness_files_manager()
        )

        if custom_names := [n for n in self._discovered if n not in BUILTIN_AGENTS]:
            logger.info(
                "Discovered custom agents %s in %s",
                " ".join(custom_names),
                " ".join(str(p) for p in self._registry.search_paths),
            )

        profile = self.available_agents.get(initial_agent)
        if profile is None:
            if initial_agent in self._discovered:
                raise ValueError(
                    excluded_agent_message(initial_agent, self.config, self._discovered)
                )
            raise ValueError(f"Agent '{initial_agent}' not found.")
        if not allow_subagent and profile.agent_type != AgentType.AGENT:
            raise ValueError(
                f"Agent '{initial_agent}' is a {profile.agent_type} and cannot be used"
                f" as the primary agent. Only agents of type 'agent' can be selected"
                f" with --agent."
            )
        self.active_profile = profile
        self._install_profile(profile)

    @property
    def _discovered(self) -> dict[str, AgentProfile]:
        return self._registry.discovered

    @property
    def config(self) -> VibeConfigSchema:
        return self._orchestrator.config

    @property
    def available_agents(self) -> dict[str, AgentProfile]:
        return {
            name: profile
            for name, profile in self._discovered.items()
            if self._is_agent_available(name, profile)
        }

    def _is_agent_available(self, name: str, profile: AgentProfile) -> bool:
        if profile.install_required and name not in self.config.installed_agents:
            return False
        if enabled := self.config.enabled_agents:
            return name_matches(name, enabled)
        return not name_matches(name, self.config.disabled_agents)

    def switch_profile(self, name: str) -> None:
        self.active_profile = self.get_agent(name)
        self._install_profile(self.active_profile)

    def preview_config(self, name: str) -> VibeConfigSchema:
        candidate = self._orchestrator.copy()
        profile = self.get_agent(name)
        apply_profile_overrides(candidate, self._resolved_profile_overrides(profile))
        return candidate.config

    def _install_profile(self, profile: AgentProfile) -> None:
        apply_profile_overrides(
            self._orchestrator, self._resolved_profile_overrides(profile)
        )

    def _resolved_profile_overrides(self, profile: AgentProfile) -> dict[str, object]:
        base_config = self._config_without_active_profile()
        overrides: dict[str, object] = dict(profile.overrides)
        if autonomy_override := overrides.get("autonomy"):
            if isinstance(autonomy_override, dict):
                autonomy = base_config.autonomy.model_dump(mode="python")
                autonomy.update(autonomy_override)
                overrides["autonomy"] = autonomy
        role_model = self._role_model_alias(profile, base_config)
        if role_model:
            overrides["active_model"] = role_model
        return overrides

    def _config_without_active_profile(self) -> VibeConfigSchema:
        candidate = self._orchestrator.copy()
        for index, layer in enumerate(candidate.layers):
            if layer.name == AgentProfileLayer.NAME:
                candidate.remove_layer(index)
                candidate.rebuild()
                break
        return candidate.config

    @staticmethod
    def _role_model_alias(profile: AgentProfile, config: VibeConfigSchema) -> str:
        match profile.name:
            case BuiltinAgentName.GOAL_ADVISOR:
                return config.resolve_goal_advisor_model().alias
            case BuiltinAgentName.REVIEWER:
                return config.resolve_reviewer_model().alias
            case _:
                return ""

    def get_agent(self, name: str) -> AgentProfile:
        if agent := self.available_agents.get(name):
            return agent
        if name in self._discovered:
            raise ValueError(
                excluded_agent_message(name, self.config, self._discovered)
            )
        raise ValueError(f"Agent '{name}' not found")

    def get_subagents(self) -> list[AgentProfile]:
        return [
            a
            for a in self.available_agents.values()
            if a.agent_type == AgentType.SUBAGENT
        ]

    def get_agent_order(self) -> list[str]:
        builtin_order: list[str] = [
            BuiltinAgentName.ASK,
            BuiltinAgentName.PLAN,
            BuiltinAgentName.ACCEPT_EDITS,
            BuiltinAgentName.AUTO_APPROVE,
        ]
        primary_agents = [
            name
            for name, agent in self.available_agents.items()
            if agent.agent_type == AgentType.AGENT and agent.cycleable
        ]
        order = [name for name in builtin_order if name in primary_agents]
        custom = sorted(name for name in primary_agents if name not in builtin_order)
        return order + custom

    def next_agent(self, current: AgentProfile) -> AgentProfile:
        order = self.get_agent_order()
        idx = order.index(current.name) if current.name in order else -1
        return self.available_agents[order[(idx + 1) % len(order)]]
