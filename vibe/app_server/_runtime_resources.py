from __future__ import annotations

from collections.abc import Callable, Mapping

from vibe.app_server._model import validate_wire
from vibe.app_server.client_state import ClientSessionState
from vibe.app_server.config import (
    ACCURACY_TEMPERATURES,
    MAX_PARALLEL_SUBAGENTS,
    MIN_PARALLEL_SUBAGENTS,
    AccuracyLevel,
    ConfigView,
    ProxySettingsView,
    ThinkingLevel,
    WebSearchActivity,
)
from vibe.app_server.connection import AppServerResourceConnection
from vibe.app_server.models import (
    AccountView,
    AgentStatsSnapshot,
    AgentSummary,
    ConfigIssue,
    ConnectorCounts,
    DebugLogPage,
    HarnessRuntimeView,
    IdentityView,
    MCPState,
    SessionLogSummary,
    SkillSummary,
    ToolSummary,
)
from vibe.app_server.protocol import (
    AccountReadParams,
    AccountReadResponse,
    AgentInstallParams,
    AgentsListParams,
    AgentsListResponse,
    AgentSwitchParams,
    AppServerResponseError,
    ConfigApiKeyWriteParams,
    ConfigFieldsReadParams,
    ConfigFieldsReadResponse,
    ConfigMutationResponse,
    ConfigProxyReadParams,
    ConfigProxyReadResponse,
    ConfigProxyWriteParams,
    ConfigReloadParams,
    ConfigSchemaReadParams,
    ConfigSchemaReadResponse,
    ConfigWriteOpWire,
    ConfigWriteParams,
    ConfigWriteResponse,
    DiagnosticsLogsReadParams,
    DiagnosticsLogsReadResponse,
    EmptyResponse,
    IdentityReadParams,
    IdentityReadResponse,
    Notification,
    ProtocolError,
    ProtocolErrorCode,
    RuntimeMutationResponse,
    RuntimeReadParams,
    RuntimeReadResponse,
    RuntimeSnapshot,
    RuntimeUpdatedParams,
    SessionReadyWaitParams,
    SessionReadyWaitResponse,
)

_MEDIUM_SUBAGENT_CEILING = 2
_HIGH_SUBAGENT_CEILING = 4


def _escape_json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


class ConfigResource:
    def __init__(
        self, connection: AppServerResourceConnection, state: ClientSessionState
    ) -> None:
        self._connection = connection
        self._state = state
        self._subscribers: list[Callable[[ConfigView], None]] = []

    @property
    def current(self) -> ConfigView:
        return self._state.config

    def subscribe(self, callback: Callable[[ConfigView], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def publish_change(self, previous: ConfigView) -> None:
        if previous == self.current:
            return
        for callback in list(self._subscribers):
            callback(self.current)

    def _apply_runtime(self, snapshot: RuntimeSnapshot) -> None:
        previous = self.current
        self._state.apply_runtime(snapshot)
        self.publish_change(previous)

    async def read_schema(self) -> ConfigSchemaReadResponse:
        client = await self._connection.connect()
        return validate_wire(
            ConfigSchemaReadResponse,
            await client.request("config/schema", ConfigSchemaReadParams()),
        )

    async def read_fields(self) -> ConfigFieldsReadResponse:
        client = await self._connection.connect()
        return validate_wire(
            ConfigFieldsReadResponse,
            await client.request(
                "config/fields/read",
                ConfigFieldsReadParams(session_id=self._state.session_id),
            ),
        )

    async def write(
        self, ops: list[ConfigWriteOpWire], *, reason: str, reload_runtime: bool = False
    ) -> ConfigWriteResponse:
        client = await self._connection.connect()
        response = validate_wire(
            ConfigWriteResponse,
            await client.request(
                "config/write",
                ConfigWriteParams(
                    session_id=self._state.session_id,
                    ops=ops,
                    reason=reason,
                    reload_runtime=reload_runtime,
                ),
            ),
        )
        if not response.rejected and not response.failures:
            self._apply_runtime(response.runtime)
        return response

    async def update(
        self, changes: Mapping[str, object], *, reload_runtime: bool = False
    ) -> None:
        ops = [
            ConfigWriteOpWire.model_validate({
                "op": "set",
                "path": f"/{key}",
                "value": value,
            })
            for key, value in changes.items()
        ]
        response = await self.write(
            ops, reason="app-server config update", reload_runtime=reload_runtime
        )
        if response.rejected:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INVALID_PARAMS,
                    message="Invalid configuration edit",
                )
            )
        if response.failures:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INTERNAL_ERROR,
                    message="; ".join(response.failures),
                )
            )

    async def set_thinking(self, level: ThinkingLevel) -> None:
        response = await self.write(
            [
                ConfigWriteOpWire(
                    op="set",
                    path=(
                        f"/models/{_escape_json_pointer_token(self.current.active_model.alias)}"
                        "/thinking"
                    ),
                    value=level,
                )
            ],
            reason="app-server thinking update",
        )
        if response.rejected:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INVALID_PARAMS,
                    message="Invalid configuration edit",
                )
            )
        if response.failures:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INTERNAL_ERROR,
                    message="; ".join(response.failures),
                )
            )

    async def set_autonomy_models(
        self, *, goal_advisor_model: str, reviewer_model: str
    ) -> None:
        response = await self.write(
            [
                ConfigWriteOpWire(
                    op="set",
                    path="/autonomy/goal_advisor_model",
                    value=goal_advisor_model,
                ),
                ConfigWriteOpWire(
                    op="set", path="/autonomy/reviewer_model", value=reviewer_model
                ),
            ],
            reason="app-server autonomy model update",
            reload_runtime=True,
        )
        if response.rejected:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INVALID_PARAMS,
                    message="Invalid advisor/reviewer model selection",
                )
            )
        if response.failures:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INTERNAL_ERROR,
                    message="; ".join(response.failures),
                )
            )

    async def set_autonomy_model(self, role: str, alias: str) -> None:
        if role not in {"advisor", "reviewer"}:
            raise ValueError(f"Unsupported autonomy model role: {role}")
        current = self.current.autonomy
        await self.set_autonomy_models(
            goal_advisor_model=(
                alias if role == "advisor" else current.goal_advisor_model
            ),
            reviewer_model=(alias if role == "reviewer" else current.reviewer_model),
        )

    async def configure_opencode_go(self, review_model: str) -> None:
        active_alias = _escape_json_pointer_token(self.current.active_model.alias)
        review_alias = _escape_json_pointer_token(review_model)
        response = await self.write(
            [
                ConfigWriteOpWire(
                    op="set", path="/autonomy/goal_advisor_model", value=review_model
                ),
                ConfigWriteOpWire(
                    op="set", path="/autonomy/reviewer_model", value=review_model
                ),
                ConfigWriteOpWire(
                    op="set", path=f"/models/{active_alias}/thinking", value="medium"
                ),
                ConfigWriteOpWire(
                    op="set", path=f"/models/{review_alias}/thinking", value="max"
                ),
                ConfigWriteOpWire(
                    op="set", path=f"/models/{review_alias}/temperature", value=0.0
                ),
            ],
            reason="app-server OpenCode Go role preset",
            reload_runtime=True,
        )
        if response.rejected:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INVALID_PARAMS,
                    message="Invalid OpenCode Go role preset",
                )
            )
        if response.failures:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INTERNAL_ERROR,
                    message="; ".join(response.failures),
                )
            )

    async def set_effort(
        self,
        level: ThinkingLevel,
        max_parallel_subagents: int,
        accuracy: AccuracyLevel,
        web_search_activity: WebSearchActivity,
        vibe_thinking: ThinkingLevel,
        gauntlet_loop: bool,
        boost_mode: bool = False,
        personal_experience: bool | None = None,
    ) -> None:
        if boost_mode:
            level = "max"
            max_parallel_subagents = MAX_PARALLEL_SUBAGENTS
            accuracy = "max"
            web_search_activity = "max"
            vibe_thinking = "max"
            gauntlet_loop = True
            personal_experience = True
        if (
            not MIN_PARALLEL_SUBAGENTS
            <= max_parallel_subagents
            <= MAX_PARALLEL_SUBAGENTS
        ):
            raise ValueError(
                "max_parallel_subagents must be between "
                f"{MIN_PARALLEL_SUBAGENTS} and {MAX_PARALLEL_SUBAGENTS}"
            )
        if gauntlet_loop and max_parallel_subagents == 0:
            raise ValueError("gauntlet_loop requires at least one subagent")
        aliases = {
            self.current.active_model.alias,
            self.current.autonomy.goal_advisor_model,
            self.current.autonomy.reviewer_model,
        }
        ops = [
            ConfigWriteOpWire(
                op="set",
                path=f"/models/{_escape_json_pointer_token(alias)}/thinking",
                value=level,
            )
            for alias in sorted(aliases - {""})
        ]
        ops.extend(
            ConfigWriteOpWire(
                op="set",
                path=f"/models/{_escape_json_pointer_token(alias)}/temperature",
                value=ACCURACY_TEMPERATURES[accuracy],
            )
            for alias in sorted(aliases - {""})
        )
        if max_parallel_subagents <= 1:
            aggressiveness = "low"
        elif max_parallel_subagents <= _MEDIUM_SUBAGENT_CEILING:
            aggressiveness = "medium"
        elif max_parallel_subagents <= _HIGH_SUBAGENT_CEILING:
            aggressiveness = "high"
        else:
            aggressiveness = "max"
        ops.extend([
            ConfigWriteOpWire(
                op="set",
                path="/autonomy/max_parallel_subagents",
                value=max_parallel_subagents,
            ),
            ConfigWriteOpWire(
                op="set",
                path="/tools/swarm/max_parallel",
                value=max(1, max_parallel_subagents),
            ),
            ConfigWriteOpWire(
                op="set", path="/autonomy/aggressiveness", value=aggressiveness
            ),
            ConfigWriteOpWire(
                op="set", path="/autonomy/enabled", value=max_parallel_subagents > 0
            ),
            ConfigWriteOpWire(
                op="set",
                path="/autonomy/web_search_activity",
                value=web_search_activity,
            ),
            ConfigWriteOpWire(
                op="set", path="/autonomy/vibe_thinking", value=vibe_thinking
            ),
            ConfigWriteOpWire(
                op="set", path="/autonomy/gauntlet_loop", value=gauntlet_loop
            ),
            ConfigWriteOpWire(op="set", path="/autonomy/boost_mode", value=boost_mode),
        ])
        if personal_experience is not None:
            ops.append(
                ConfigWriteOpWire(
                    op="set",
                    path="/autonomy/personal_experience",
                    value=personal_experience,
                )
            )
        if boost_mode:
            ops.extend([
                ConfigWriteOpWire(
                    op="set", path="/autonomy/require_worker", value=True
                ),
                ConfigWriteOpWire(
                    op="set", path="/autonomy/require_review", value=True
                ),
            ])
        response = await self.write(
            ops, reason="app-server effort update", reload_runtime=True
        )
        if response.rejected:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INVALID_PARAMS,
                    message="Invalid effort configuration edit",
                )
            )
        if response.failures:
            raise AppServerResponseError(
                ProtocolError(
                    code=ProtocolErrorCode.INTERNAL_ERROR,
                    message="; ".join(response.failures),
                )
            )

    async def reload(self, *, reload_runtime: bool = True) -> int:
        client = await self._connection.connect()
        response = validate_wire(
            ConfigMutationResponse,
            await client.request(
                "config/reload",
                ConfigReloadParams(
                    session_id=self._state.session_id, reload_runtime=reload_runtime
                ),
            ),
        )
        self._apply_runtime(response.runtime)
        return response.stripped_history_images

    async def write_api_key(self, model_alias: str, api_key: str) -> int:
        client = await self._connection.connect()
        response = validate_wire(
            ConfigMutationResponse,
            await client.request(
                "config/apiKey/write",
                ConfigApiKeyWriteParams(
                    session_id=self._state.session_id,
                    model_alias=model_alias,
                    api_key=api_key,
                ),
            ),
        )
        self._apply_runtime(response.runtime)
        return response.stripped_history_images

    async def read_proxy(self) -> ProxySettingsView:
        client = await self._connection.connect()
        response = validate_wire(
            ConfigProxyReadResponse,
            await client.request(
                "config/proxy/read",
                ConfigProxyReadParams(session_id=self._state.session_id),
            ),
        )
        return response.settings

    async def update_proxy(self, changes: Mapping[str, str | None]) -> None:
        client = await self._connection.connect()
        validate_wire(
            EmptyResponse,
            await client.request(
                "config/proxy/write",
                ConfigProxyWriteParams(
                    session_id=self._state.session_id, changes=dict(changes)
                ),
            ),
        )


class AccountResource:
    def __init__(
        self, connection: AppServerResourceConnection, state: ClientSessionState
    ) -> None:
        self._connection = connection
        self._state = state
        self._current: AccountView | None = None

    @property
    def current(self) -> AccountView | None:
        return self._current

    async def read(self) -> AccountView:
        self._current = None
        client = await self._connection.connect()
        response = validate_wire(
            AccountReadResponse,
            await client.request(
                "account/read", AccountReadParams(session_id=self._state.session_id)
            ),
        )
        self._current = response.account
        return response.account


class IdentityResource:
    def __init__(
        self, connection: AppServerResourceConnection, state: ClientSessionState
    ) -> None:
        self._connection = connection
        self._state = state
        self._current: IdentityView | None = None

    @property
    def current(self) -> IdentityView | None:
        return self._current

    async def read(self) -> IdentityView | None:
        self._current = None
        client = await self._connection.connect()
        response = validate_wire(
            IdentityReadResponse,
            await client.request(
                "identity/read", IdentityReadParams(session_id=self._state.session_id)
            ),
        )
        self._current = response.identity
        return response.identity


class AgentResource:
    def __init__(
        self, connection: AppServerResourceConnection, state: ClientSessionState
    ) -> None:
        self._connection = connection
        self._state = state

    @property
    def active(self) -> AgentSummary:
        return self._state.active_agent

    @property
    def all(self) -> list[AgentSummary]:
        return self._state.agents

    def next(self, current_name: str | None = None) -> AgentSummary:
        return self._state.next_agent(current_name)

    async def read(self) -> None:
        client = await self._connection.connect()
        response = validate_wire(
            AgentsListResponse,
            await client.request(
                "agents/list", AgentsListParams(session_id=self._state.session_id)
            ),
        )
        self._state.apply_agents(response)

    async def switch(self, agent_name: str) -> AgentSummary:
        client = await self._connection.connect()
        response = validate_wire(
            RuntimeMutationResponse,
            await client.request(
                "session/agent/update",
                AgentSwitchParams(
                    session_id=self._state.session_id, agent_name=agent_name
                ),
            ),
        )
        self._state.apply_runtime(response.runtime)
        return response.runtime.active_agent

    async def set_installed(self, agent_name: str, *, installed: bool) -> None:
        client = await self._connection.connect()
        method = "agents/install" if installed else "agents/uninstall"
        response = validate_wire(
            AgentsListResponse,
            await client.request(
                method,
                AgentInstallParams(
                    session_id=self._state.session_id, agent_name=agent_name
                ),
            ),
        )
        self._state.apply_agents(response)


class RuntimeResource:
    def __init__(
        self, connection: AppServerResourceConnection, state: ClientSessionState
    ) -> None:
        self._connection = connection
        self._state = state
        self._session_last_init_duration_ms: int | None = None

    @property
    def session_init_duration_ms(self) -> int | None:
        return self._session_last_init_duration_ms

    @property
    def skills(self) -> list[SkillSummary]:
        return self._state.skills

    @property
    def tools(self) -> list[ToolSummary]:
        return self._state.tools

    @property
    def stats(self) -> AgentStatsSnapshot:
        return self._state.stats

    @property
    def context_window(self) -> int:
        return self._state.context_window

    @property
    def issues(self) -> list[ConfigIssue]:
        return self._state.issues

    @property
    def hooks_count(self) -> int:
        return self._state.hooks_count

    @property
    def connectors(self) -> ConnectorCounts:
        return self._state.connectors

    @property
    def mcp(self) -> MCPState:
        return self._state.mcp

    @property
    def harness(self) -> HarnessRuntimeView:
        return self._state.harness

    @property
    def session_log(self) -> SessionLogSummary:
        return self._state.session_log

    @property
    def ready(self) -> bool:
        return self._state.ready

    @property
    def custom_skills_count(self) -> int:
        return self._state.custom_skills_count

    def get_skill(self, name: str) -> SkillSummary | None:
        return self._state.get_skill(name)

    def has_tool(self, name: str) -> bool:
        return self._state.has_tool(name)

    async def read_logs(self, *, limit: int = 100, offset: int = 0) -> DebugLogPage:
        client = await self._connection.connect()
        response = validate_wire(
            DiagnosticsLogsReadResponse,
            await client.request(
                "diagnostics/logs/read",
                DiagnosticsLogsReadParams(
                    session_id=self._state.session_id, limit=limit, offset=offset
                ),
            ),
        )
        return response.logs

    async def wait_until_ready(self) -> None:
        client = await self._connection.connect()
        response = validate_wire(
            SessionReadyWaitResponse,
            await client.request(
                "session/ready/wait",
                SessionReadyWaitParams(session_id=self._state.session_id),
            ),
        )
        self._state.ready = True
        self._session_last_init_duration_ms = response.init_duration_ms
        await self.refresh()

    async def refresh(self) -> None:
        client = await self._connection.connect()
        response = validate_wire(
            RuntimeReadResponse,
            await client.request(
                "runtime/read", RuntimeReadParams(session_id=self._state.session_id)
            ),
        )
        self._state.apply_runtime_read(response)

    async def consume_notification(self, notification: Notification) -> bool:
        if notification.method != "runtime/updated":
            return False
        params = validate_wire(RuntimeUpdatedParams, notification.params)
        if params.session_id != self._state.session_id:
            return False
        self._state.apply_runtime(params.runtime)
        return True
