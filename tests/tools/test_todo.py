from __future__ import annotations

import pytest

from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.todo import Todo, TodoConfig, TodoItem, TodoState


def _tool() -> Todo:
    return Todo(config_getter=TodoConfig, state=TodoState())


def test_todo_accepts_dependency_graph() -> None:
    todos = [
        TodoItem(id="inspect", content="Inspect"),
        TodoItem(id="implement", content="Implement", depends_on=["inspect"]),
        TodoItem(id="verify", content="Verify", depends_on=["implement"]),
    ]

    result = _tool()._write_todos(todos)

    assert result.todos == todos


def test_todo_rejects_unknown_dependency() -> None:
    todos = [TodoItem(id="implement", content="Implement", depends_on=["missing"])]

    with pytest.raises(ToolError, match="unknown dependencies: missing"):
        _tool()._write_todos(todos)


def test_todo_rejects_dependency_cycle() -> None:
    todos = [
        TodoItem(id="a", content="A", depends_on=["b"]),
        TodoItem(id="b", content="B", depends_on=["a"]),
    ]

    with pytest.raises(ToolError, match="contains a cycle"):
        _tool()._write_todos(todos)
