from __future__ import annotations

from vibe.core.subagents import (
    SUBAGENT_TRUNCATION_MARKER,
    SubagentRunAccumulator,
    TaskResult,
)
from vibe.core.tools.builtins.bash import Bash, CapturedShellResult
from vibe.core.types import AssistantEvent, ToolResultEvent


def test_subagent_run_accumulates_response_and_tool_progress() -> None:
    accumulator = SubagentRunAccumulator()

    assert (
        accumulator.observe(
            AssistantEvent(content="Found the issue"), tool_call_id="task-1"
        )
        is None
    )
    progress = accumulator.observe(
        ToolResultEvent(
            tool_name="bash",
            tool_class=Bash,
            result=CapturedShellResult(command="pwd", stdout="/repo", stderr=""),
            tool_call_id="bash-1",
        ),
        tool_call_id="task-1",
    )

    assert progress is not None
    assert progress.tool_call_id == "task-1"
    assert progress.message == "bash: Ran pwd"
    assert accumulator.build_result(turns_used=1) == TaskResult(
        response="Found the issue",
        turns_used=1,
        completed=True,
        original_chars=len("Found the issue"),
    )


def test_subagent_run_combines_observed_and_runtime_failures() -> None:
    accumulator = SubagentRunAccumulator()
    accumulator.observe(
        AssistantEvent(content="Partial", stopped_by_middleware=True),
        tool_call_id="task-1",
    )
    accumulator.record_error("child failed")

    assert accumulator.build_result(turns_used=2, completed=False) == TaskResult(
        response="Partial\n[Subagent error: child failed]",
        turns_used=2,
        completed=False,
        original_chars=len("Partial\n[Subagent error: child failed]"),
    )


def test_subagent_response_is_bounded_with_unicode_head_and_tail() -> None:
    max_chars = 96
    content = "начало-" + "🙂" * 200 + "-конец"
    accumulator = SubagentRunAccumulator(max_chars=max_chars)

    accumulator.observe(AssistantEvent(content=content), tool_call_id="task-1")
    result = accumulator.build_result(turns_used=1)

    assert result.truncated is True
    assert result.original_chars == len(content)
    assert len(result.response) == max_chars
    assert result.response.startswith("начало-")
    assert result.response.endswith("-конец")
    assert SUBAGENT_TRUNCATION_MARKER in result.response


def test_subagent_response_tail_updates_without_exceeding_budget() -> None:
    accumulator = SubagentRunAccumulator(max_chars=80)
    chunks = ["head", "x" * 10_000, "final-tail"]
    for chunk in chunks:
        accumulator.observe(AssistantEvent(content=chunk), tool_call_id="task-1")

    result = accumulator.build_result(turns_used=3)

    assert len(result.response) == 80
    assert result.response.startswith("head")
    assert result.response.endswith("final-tail")
    assert result.original_chars == sum(map(len, chunks))
