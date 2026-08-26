from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from vibe.app_server._execution import SessionExecution
from vibe.app_server._model import ProtocolModel
from vibe.app_server._projection import project_history, project_session_log
from vibe.app_server._root_session import SessionHandoff, rebind_history
from vibe.app_server._runtime import AgentRuntimeFactory, close_agent_loop
from vibe.app_server._session_history import SessionHistory
from vibe.app_server._state import build_public_state
from vibe.app_server._streaming import BoundedEventQueue, stream_until_complete
from vibe.app_server._turns import DeliverCallback, TurnController
from vibe.app_server.models import (
    CallbackOutput,
    OpenCallbackState,
    PublicCallbackEntry,
    PublicHistoryEntry,
    PublicSessionState,
    PublicTurn,
    PublicTurnStatus,
    TextContentBlock,
)
from vibe.app_server.protocol import (
    CallbackResultError,
    TurnInterruptParams,
    TurnStartParams,
)
from vibe.core.agent_loop import AgentLoop
from vibe.core.session.saved_sessions import delete_saved_session
from vibe.core.subagents import (
    DEFAULT_SUBAGENT_RESULT_MAX_CHARS,
    SubagentRunAccumulator,
    SubagentRunnerPort,
    SwarmResult,
    TaskArgs,
    TaskResult,
    prepare_subagent_prompt,
)
from vibe.core.tools.base import InvokeContext
from vibe.core.tools.io_port import ToolIOPort
from vibe.core.types import BaseEvent, ChildSessionLink, Role, ToolStreamEvent
from vibe.observability.logging import logger

type Notify = Callable[[str, ProtocolModel], Awaitable[None]]
type EventWatermark = Callable[[str], int]


@dataclass(slots=True)
class SessionRuntime:
    agent_loop: AgentLoop
    turns: TurnController
    execution: SessionExecution
    history: SessionHistory
    _closed: bool = field(default=False, init=False, repr=False)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for cleanup in (self.turns.close, lambda: close_agent_loop(self.agent_loop)):
            try:
                await cleanup()
            except BaseException as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Failed to close session runtime", errors)


@dataclass(frozen=True, slots=True)
class _SwarmQueueItem:
    index: int
    event: ToolStreamEvent | None = None
    result: TaskResult | None = None
    terminal: bool = False


class SessionRuntimeRegistry(SubagentRunnerPort):
    def __init__(
        self,
        notify_child: Notify,
        deliver_callback: DeliverCallback,
        event_watermark: EventWatermark,
        tool_io: ToolIOPort | None = None,
        runtime_factory: AgentRuntimeFactory | None = None,
    ) -> None:
        self._notify_child = notify_child
        self._deliver_callback = deliver_callback
        self._event_watermark = event_watermark
        self._tool_io = tool_io
        self._runtime_factory = runtime_factory or AgentRuntimeFactory()
        self._root: SessionRuntime | None = None
        self._children: dict[str, SessionRuntime] = {}
        self._child_links: dict[str, tuple[SessionRuntime, str]] = {}
        self._ensure_child_lock = asyncio.Lock()
        self._child_cache_lock = asyncio.Lock()

    def bind_root(self, runtime: SessionRuntime) -> None:
        if self._root is not None:
            raise RuntimeError("A root session runtime is already registered")
        self._root = runtime

    def release_root(self, runtime: SessionRuntime) -> None:
        if self._root is runtime:
            self._root = None

    def references_child(self, session_id: str) -> bool:
        return (
            session_id in self._children
            or self._resolve_child_link(session_id) is not None
        )

    async def ensure_child(self, session_id: str) -> bool:
        if session_id in self._children:
            return True
        async with self._ensure_child_lock:
            if session_id in self._children:
                return True
            resolved = self._resolve_child_link(session_id)
            if resolved is None:
                return False
            parent_runtime, root_agent_loop, child_dir, link = resolved
            child: AgentLoop | None = None
            try:
                child = await self._runtime_factory.resume_child(
                    root_agent_loop, link.agent, link.session_id, child_dir
                )
                runtime = self._build_child_runtime(
                    child, base_history=project_history(child)
                )
            except Exception as exc:
                logger.warning(
                    "Failed to ensure child session session_id=%s path=%s",
                    session_id,
                    child_dir,
                    exc_info=exc,
                )
                if child is not None:
                    await self._discard_child(child)
                return False
            self._children[child.session_id] = runtime
            self._child_links[child.session_id] = (parent_runtime, link.tool_call_id)
            await self._trim_idle_children(
                root_agent_loop, keep_session_id=child.session_id
            )
        return True

    def _resolve_child_link(
        self, session_id: str
    ) -> tuple[SessionRuntime, AgentLoop, Path, ChildSessionLink] | None:
        parent_runtime = self._root
        if parent_runtime is None:
            return None
        root_agent_loop = parent_runtime.agent_loop
        metadata = root_agent_loop.session_logger.session_metadata
        parent_dir = root_agent_loop.session_logger.session_dir
        if metadata is None or parent_dir is None:
            return None
        parent_root = parent_dir.resolve()
        link = next(
            (
                link
                for link in metadata.child_sessions
                if link.session_id == session_id and link.relative_path is not None
            ),
            None,
        )
        if link is None or link.relative_path is None:
            return None
        child_dir = (parent_dir / link.relative_path).resolve()
        if not child_dir.is_relative_to(parent_root) or not child_dir.is_dir():
            return None
        return parent_runtime, root_agent_loop, child_dir, link

    def child_belongs_to(self, session_id: str, root_session_id: str) -> bool:
        child = self._children.get(session_id)
        return (
            child is not None and child.agent_loop.parent_session_id == root_session_id
        )

    def handoff_root(self, old_session_id: str, new_session_id: str) -> None:
        for runtime in self._children.values():
            if runtime.agent_loop.parent_session_id == old_session_id:
                runtime.agent_loop.parent_session_id = new_session_id

    async def handoff_active_turn(
        self,
        old_session_id: str,
        *,
        current_history: list[PublicHistoryEntry],
        callbacks: list[PublicCallbackEntry],
        active_turn: PublicTurn,
        completed_turns: list[PublicTurn],
        history_limit: int = 200,
    ) -> SessionHandoff:
        runtime = self._require_child(old_session_id)
        new_session_id = runtime.agent_loop.session_id
        if old_session_id == new_session_id:
            raise RuntimeError("Session handoff did not change the session ID")
        if new_session_id in self._children:
            raise RuntimeError(f"Child session is already registered: {new_session_id}")
        parent, tool_call_id = self._child_links[old_session_id]

        await parent.agent_loop.replace_child_session(
            old_session_id, runtime.agent_loop, tool_call_id
        )
        self._children.pop(old_session_id)
        self._children[new_session_id] = runtime
        self._child_links.pop(old_session_id)
        self._child_links[new_session_id] = (parent, tool_call_id)
        runtime.history.replace(rebind_history(runtime.history.base, new_session_id))
        for child in self._children.values():
            if child.agent_loop.parent_session_id == old_session_id:
                child.agent_loop.parent_session_id = new_session_id
        await parent.turns.replace_subagent(
            tool_call_id, old_session_id, new_session_id
        )
        state = build_public_state(
            runtime.agent_loop,
            history=runtime.history.base,
            current_history=current_history,
            callbacks=callbacks,
            active_turn=active_turn,
            completed_turns=completed_turns,
            history_limit=history_limit,
        )
        state = state.model_copy(
            update={"event_id": self._event_watermark(new_session_id)}
        )
        return SessionHandoff(
            old_session_id=old_session_id,
            new_session_id=new_session_id,
            state=state,
            session_log=project_session_log(runtime.agent_loop),
        )

    def public_state(
        self,
        session_id: str,
        history_limit: int,
        *,
        turns_limit: int | None = None,
        include_history: bool = True,
        include_turns: bool = True,
    ) -> PublicSessionState:
        runtime = self._require_child(session_id)
        callbacks = [
            entry
            for entry in self.history(session_id)
            if isinstance(entry, PublicCallbackEntry)
        ]
        state = build_public_state(
            runtime.agent_loop,
            history=runtime.history.base,
            current_history=runtime.turns.history,
            callbacks=callbacks,
            active_turn=runtime.turns.active_turn,
            completed_turns=runtime.turns.completed_turns,
            history_limit=history_limit,
            turns_limit=turns_limit,
            include_history=include_history,
            include_turns=include_turns,
        )
        return state.model_copy(update={"event_id": self._event_watermark(session_id)})

    def history(self, session_id: str) -> list[PublicHistoryEntry]:
        runtime = self._require_child(session_id)
        return runtime.history.all(runtime.turns.history)

    def turns(self, session_id: str) -> list[PublicTurn]:
        return self._require_child(session_id).turns.turns

    def active_callbacks(self) -> list[PublicCallbackEntry]:
        return [
            callback
            for runtime in self._children.values()
            for callback in runtime.turns.callbacks
            if isinstance(callback.state, OpenCallbackState)
        ]

    async def answer_callback(
        self, session_id: str, callback_id: str, output: CallbackOutput
    ) -> str:
        return await self._require_child(session_id).turns.answer_callback(
            callback_id, output
        )

    async def reject_callback(
        self, session_id: str, callback_id: str, error: CallbackResultError
    ) -> str:
        return await self._require_child(session_id).turns.reject_callback(
            callback_id, error
        )

    async def close(self) -> None:
        await self.close_children()

    async def run(
        self, args: TaskArgs, ctx: InvokeContext, *, max_result_chars: int = 0
    ) -> AsyncGenerator[ToolStreamEvent | TaskResult, None]:
        async for event in self._run_child(
            args,
            ctx,
            max_result_chars=self._result_char_budget(ctx, max_result_chars),
            link_tool_call_id=ctx.tool_call_id,
            project_parent=True,
        ):
            yield event

    async def run_many(
        self,
        args: list[TaskArgs],
        ctx: InvokeContext,
        *,
        max_parallel: int,
        max_result_chars: int = 0,
    ) -> AsyncGenerator[ToolStreamEvent | SwarmResult, None]:
        if max_parallel <= 0:
            raise ValueError("max_parallel must be positive")
        result_chars = self._result_char_budget(ctx, max_result_chars)
        queue = BoundedEventQueue[_SwarmQueueItem]()
        semaphore = asyncio.Semaphore(max_parallel)

        async def worker(index: int, task_args: TaskArgs) -> None:
            result: TaskResult | None = None
            try:
                async with semaphore:
                    async for event in self._run_child(
                        task_args,
                        ctx,
                        max_result_chars=result_chars,
                        link_tool_call_id=f"{ctx.tool_call_id}:swarm:{index}",
                        project_parent=False,
                    ):
                        if isinstance(event, TaskResult):
                            result = event
                        else:
                            await queue.put(_SwarmQueueItem(index=index, event=event))
                if result is None:
                    accumulator = SubagentRunAccumulator(max_chars=result_chars)
                    accumulator.record_error("Subagent returned no result")
                    result = accumulator.build_result(turns_used=0, completed=False)
            except Exception as exc:
                accumulator = SubagentRunAccumulator(max_chars=result_chars)
                accumulator.record_error(str(exc))
                result = accumulator.build_result(turns_used=0, completed=False)
            finally:
                current = asyncio.current_task()
                if result is not None and (current is None or not current.cancelling()):
                    await queue.put(
                        _SwarmQueueItem(index=index, result=result, terminal=True)
                    )

        tasks = [
            asyncio.create_task(
                worker(index, task_args),
                name=f"vibe-swarm-worker:{ctx.tool_call_id}:{index}",
            )
            for index, task_args in enumerate(args)
        ]
        results: list[TaskResult | None] = [None] * len(args)
        completed = 0
        try:
            while completed < len(args):
                item = await queue.get()
                if item.terminal:
                    results[item.index] = item.result
                    completed += 1
                    continue
                if item.event is not None:
                    task_args = args[item.index]
                    yield item.event.model_copy(
                        update={
                            "tool_name": "swarm",
                            "tool_call_id": ctx.tool_call_id,
                            "message": (
                                f"[{item.index + 1}:{task_args.agent}] "
                                f"{item.event.message}"
                            ),
                        }
                    )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

        ordered_results = [result for result in results if result is not None]
        yield SwarmResult(
            results=ordered_results,
            completed_count=sum(result.completed for result in ordered_results),
        )

    async def _run_child(  # noqa: PLR0915
        self,
        args: TaskArgs,
        ctx: InvokeContext,
        *,
        max_result_chars: int,
        link_tool_call_id: str,
        project_parent: bool,
    ) -> AsyncGenerator[ToolStreamEvent | TaskResult, None]:
        parent = self._runtime(ctx.session_id)
        child = await self._runtime_factory.create_child(parent.agent_loop, args.agent)
        progress = BoundedEventQueue[ToolStreamEvent]()
        accumulator = SubagentRunAccumulator(max_chars=max_result_chars)

        async def consume_event(event: BaseEvent) -> None:
            if update := accumulator.observe(event, tool_call_id=ctx.tool_call_id):
                await progress.put(update)

        runtime = self._build_child_runtime(child, event_sink=consume_event)
        if args.max_turns is not None:
            child.set_max_turns(args.max_turns)
        self._children[child.session_id] = runtime
        self._child_links[child.session_id] = (parent, link_tool_call_id)
        await self._trim_idle_children(
            parent.agent_loop, keep_session_id=child.session_id
        )
        link_recorded = False
        projection_started = False
        try:
            await child.persist_empty_session()
            await parent.agent_loop.record_child_session(child, link_tool_call_id)
            link_recorded = True
            if project_parent:
                projection_started = True
                await parent.turns.link_subagent(link_tool_call_id, child.session_id)
        except BaseException:
            self._children.pop(child.session_id, None)
            self._child_links.pop(child.session_id, None)
            if projection_started:
                with suppress(Exception):
                    await parent.turns.unlink_subagent(
                        link_tool_call_id, child.session_id
                    )
            if link_recorded:
                with suppress(Exception):
                    await parent.agent_loop.forget_child_session(
                        child.session_id, link_tool_call_id
                    )
            with suppress(Exception):
                await runtime.close()
            with suppress(Exception):
                await delete_saved_session(
                    child.session_id, child.config.session_logging
                )
            raise

        response, start = runtime.turns.start(
            TurnStartParams(
                session_id=child.session_id,
                message=[
                    TextContentBlock(text=prepare_subagent_prompt(args.task, ctx))
                ],
            )
        )
        start()
        completion = asyncio.create_task(
            runtime.turns.wait_for_turn(response.turn.id),
            name=f"vibe-subagent-turn:{child.session_id}",
        )
        turn: PublicTurn | None = None
        try:
            async with asyncio.timeout(args.timeout_seconds):
                async for item in stream_until_complete(
                    progress, completion, event_task_name="vibe-subagent-progress"
                ):
                    yield item
                turn = await completion
        except TimeoutError:
            if runtime.turns.active_turn is not None:
                runtime.turns.interrupt(
                    TurnInterruptParams(
                        session_id=child.session_id, expected_turn_id=response.turn.id
                    )
                )
            accumulator.record_error(
                f"Subagent deadline exceeded after {args.timeout_seconds:g}s"
            )
            with suppress(asyncio.CancelledError, RuntimeError):
                await completion
        except (asyncio.CancelledError, GeneratorExit):
            if runtime.turns.active_turn is not None:
                runtime.turns.interrupt(
                    TurnInterruptParams(
                        session_id=child.session_id, expected_turn_id=response.turn.id
                    )
                )
            with suppress(asyncio.CancelledError, RuntimeError):
                await completion
            raise

        if turn is not None and turn.error is not None:
            accumulator.record_error(turn.error.message)
        turns_used = sum(message.role is Role.assistant for message in child.messages)
        await self._trim_idle_children(
            parent.agent_loop, keep_session_id=child.session_id
        )
        yield accumulator.build_result(
            turns_used=turns_used,
            completed=(turn is not None and turn.status is PublicTurnStatus.COMPLETED),
        )

    async def _trim_idle_children(
        self, root: AgentLoop, *, keep_session_id: str
    ) -> None:
        autonomy = root.config.autonomy
        if not autonomy.enabled:
            return
        async with self._child_cache_lock:
            excess = len(self._children) - autonomy.max_live_child_runtimes
            if excess <= 0:
                return
            evicted: list[tuple[str, SessionRuntime]] = []
            for session_id, runtime in self._children.items():
                if excess <= 0:
                    break
                if session_id == keep_session_id:
                    continue
                if (
                    runtime.turns.active_turn is not None
                    or runtime.execution.active is not None
                ):
                    continue
                evicted.append((session_id, runtime))
                excess -= 1
            for session_id, _runtime in evicted:
                self._children.pop(session_id, None)
                self._child_links.pop(session_id, None)
            for session_id, runtime in evicted:
                try:
                    await runtime.close()
                except BaseException as exc:
                    logger.warning(
                        "Failed to close evicted child runtime session_id=%s",
                        session_id,
                        exc_info=exc,
                    )

    @staticmethod
    def _result_char_budget(ctx: InvokeContext, override: int) -> int:
        if override > 0:
            return override
        config = ctx.agent_manager.config if ctx.agent_manager is not None else None
        autonomy = getattr(config, "autonomy", None)
        configured = getattr(autonomy, "max_subagent_result_chars", None)
        if isinstance(configured, int) and configured > 0:
            return configured
        return DEFAULT_SUBAGENT_RESULT_MAX_CHARS

    def _build_child_runtime(
        self,
        child: AgentLoop,
        *,
        base_history: list[PublicHistoryEntry] | None = None,
        event_sink: Callable[[BaseEvent], Awaitable[None]] | None = None,
    ) -> SessionRuntime:
        execution = SessionExecution()
        turns = TurnController(
            child,
            self._notify_child,
            self._deliver_callback,
            execution,
            self,
            self._tool_io,
            event_sink,
            self,
        )
        return SessionRuntime(
            child, turns, execution, SessionHistory(base_history or [])
        )

    def _runtime(self, session_id: str | None) -> SessionRuntime:
        if session_id is None:
            raise RuntimeError("Subagent parent session is missing")
        root = self._root
        if root is not None and root.agent_loop.session_id == session_id:
            return root
        child = self._children.get(session_id)
        if child is None:
            raise RuntimeError(f"Subagent parent session not found: {session_id}")
        return child

    def _require_child(self, session_id: str) -> SessionRuntime:
        child = self._children.get(session_id)
        if child is None:
            raise KeyError(session_id)
        return child

    async def close_children(self) -> None:
        async with self._ensure_child_lock:
            children = list(self._children.values())
            self._children.clear()
            self._child_links.clear()
        errors: list[BaseException] = []
        for runtime in children:
            try:
                await runtime.close()
            except BaseException as exc:
                errors.append(exc)
        if not errors:
            return
        if len(errors) == 1:
            raise errors[0]
        raise BaseExceptionGroup("Failed to close child session runtimes", errors)

    @staticmethod
    async def _discard_child(child: AgentLoop) -> None:
        with suppress(Exception):
            await close_agent_loop(child)
