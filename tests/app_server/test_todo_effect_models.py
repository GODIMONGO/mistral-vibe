from __future__ import annotations

from vibe.app_server._effect_models import TodoEffectInput, TodoEffectOutput


def test_todo_dependency_graph_survives_public_wire_projection() -> None:
    payload = {
        "action": "write",
        "todos": [{"id": "verify", "content": "Verify", "dependsOn": ["implement"]}],
    }

    projected = TodoEffectInput.model_validate(payload)
    wire = projected.model_dump(mode="json", by_alias=True)
    output = TodoEffectOutput.model_validate({"todos": wire["todos"]})

    assert wire["todos"][0]["dependsOn"] == ["implement"]
    assert output.todos[0].depends_on == ["implement"]
