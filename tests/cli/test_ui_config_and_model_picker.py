from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import OptionList, Static

from tests.conftest import build_test_vibe_app, build_test_vibe_config
from vibe.app_server.config import THINKING_LEVELS
from vibe.app_server.protocol import (
    ConfigFieldKind,
    ConfigFieldsReadResponse,
    ConfigFieldWire,
    ConfigLayerValueWire,
)
from vibe.cli.textual_ui.app import BottomApp
from vibe.cli.textual_ui.widgets.effort_picker import EffortPickerApp
from vibe.cli.textual_ui.widgets.model_picker import ModelPickerApp
from vibe.cli.textual_ui.widgets.model_settings import ModelSettingsApp
from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic
from vibe.cli.textual_ui.widgets.thinking_picker import ThinkingPickerApp
from vibe.core.config import AutonomyConfig, ModelConfig


def _model_configs() -> list[ModelConfig]:
    return [
        ModelConfig(name="model-a", provider="mistral", alias="alpha"),
        ModelConfig(name="model-b", provider="mistral", alias="beta"),
        ModelConfig(name="model-c", provider="mistral", alias="gamma"),
    ]


def _make_config_with_models(**kwargs):
    kwargs.setdefault("autonomy", AutonomyConfig())
    return build_test_vibe_config(
        models=_model_configs(), active_model="alpha", **kwargs
    )


def _make_unpinned_config(**kwargs):
    # active_model="" is the unpinned/default sentinel.
    return build_test_vibe_config(models=_model_configs(), active_model="", **kwargs)


# --- /model command ---


@pytest.mark.asyncio
async def test_model_command_opens_control_center() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)

        handled = await app._handle_command("/model")
        await pilot.pause(0.2)

        assert handled
        assert app._current_bottom_app == BottomApp.ModelSettings
        assert app.query_one(ModelSettingsApp).query_one(OptionList).option_count == 6


@pytest.mark.asyncio
async def test_model_control_center_enter_opens_main_picker_without_selecting() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._handle_command("/model")
        await pilot.pause(0.2)

        await pilot.press("enter")
        await pilot.pause(0.3)

        assert app._current_bottom_app == BottomApp.ModelPicker
        assert len(app.query(ModelSettingsApp)) == 0
        assert app.query_one(ModelPickerApp)._title == "Select Main Model"


@pytest.mark.asyncio
async def test_model_control_center_can_select_a_different_main_model() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._handle_command("/model")
        await pilot.pause(0.2)

        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("down", "enter")
        await pilot.pause(0.4)

        assert app.config.active_model.alias == "beta"


@pytest.mark.asyncio
async def test_model_control_center_opens_advisor_picker() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._handle_command("/model")
        await pilot.pause(0.2)

        await pilot.press("down", "enter")
        await pilot.pause(0.3)

        assert app._current_bottom_app == BottomApp.ModelPicker
        assert app.query_one(ModelPickerApp)._title == "Select Advisor Model"


@pytest.mark.asyncio
async def test_model_control_center_opens_thinking_picker() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._handle_command("/model")
        await pilot.pause(0.2)

        await pilot.press("down", "down", "down", "enter")
        await pilot.pause(0.3)

        assert app._current_bottom_app == BottomApp.ThinkingPicker
        assert len(app.query(ThinkingPickerApp)) == 1


@pytest.mark.asyncio
async def test_model_command_can_open_advisor_picker_directly() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)

        handled = await app._handle_command("/model advisor")
        await pilot.pause(0.2)

        assert handled
        assert app._current_bottom_app == BottomApp.ModelPicker
        assert app.query_one(ModelPickerApp)._title == "Select Advisor Model"


@pytest.mark.asyncio
async def test_advisor_picker_persists_role_without_switching_main() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._model_command(cmd_args="advisor")
        await pilot.pause(0.2)

        with patch.object(
            app.app_server.resources.config, "set_autonomy_model", new=AsyncMock()
        ) as set_role:
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(0.3)

        set_role.assert_awaited_once_with("advisor", "beta")
        assert app.config.active_model.alias == "alpha"


def test_ui_unpinned_sentinel_matches_schema() -> None:
    # The textual layer keeps its own copy of the sentinel to avoid importing
    # vibe.core (see test_app_server_boundary); guard against value drift so
    # "select Default" always persists a value the schema treats as unpinned.
    from vibe.cli.textual_ui.constants import UNPINNED_ACTIVE_MODEL as ui_value
    from vibe.core.config.vibe_schema import UNPINNED_ACTIVE_MODEL as schema_value

    assert ui_value == schema_value


@pytest.mark.asyncio
async def test_model_opens_model_picker() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.ModelPicker
        assert len(app.query(ModelPickerApp)) == 1


@pytest.mark.asyncio
async def test_model_picker_shows_all_models() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        picker = app.query_one(ModelPickerApp)
        assert [model.alias for model in picker._models] == ["alpha", "beta", "gamma"]
        assert picker._current_model == "alpha"


@pytest.mark.asyncio
async def test_model_picker_shows_display_name_but_persists_alias() -> None:
    models = [
        ModelConfig(name="model-a", provider="mistral", alias="alpha"),
        ModelConfig(
            name="zai-glm-5-2",
            provider="mistral",
            alias="glm-5-2",
            display_name="glm-5.2 (Mistral Hosted)",
        ),
    ]
    config = build_test_vibe_config(models=models, active_model="alpha")
    app = build_test_vibe_app(config=config)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        picker = app.query_one(ModelPickerApp)
        assert [model.display_name for model in picker._models] == [
            "alpha",
            "glm-5.2 (Mistral Hosted)",
        ]
        option_list = picker.query_one(OptionList)
        # Row 2 is the routed model, offset by the leading Default row.
        assert "glm-5.2 (Mistral Hosted)" in str(
            option_list.get_option_at_index(2).prompt
        )

        # Selecting it still persists the alias, not the label.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.config.active_model.alias == "glm-5-2"


@pytest.mark.asyncio
async def test_model_picker_escape_returns_to_input() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        await pilot.press("escape")
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.Input
        assert len(app.query(ModelPickerApp)) == 0


@pytest.mark.asyncio
async def test_model_picker_escape_does_not_save() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        with patch.object(
            app.app_server.resources.config, "update", new=AsyncMock()
        ) as update_config:
            await pilot.press("escape")
            await pilot.pause(0.2)

            update_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_picker_select_model() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        # Navigate down to "beta" and select
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.config.active_model.alias == "beta"
        assert app._current_bottom_app == BottomApp.Input
        assert len(app.query(ModelPickerApp)) == 0


@pytest.mark.asyncio
async def test_model_picker_select_current_model() -> None:
    """Selecting the already-active model still saves (idempotent)."""
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.config.active_model.alias == "alpha"
        assert app._current_bottom_app == BottomApp.Input


@pytest.mark.asyncio
async def test_model_picker_blocked_when_active_model_enforced() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        enforced_response = ConfigFieldsReadResponse(
            fields=[
                ConfigFieldWire(
                    name="active_model",
                    kind=ConfigFieldKind.ENUM,
                    description="",
                    value="alpha",
                    path="/active_model",
                    layer_values=[ConfigLayerValueWire(layer="admin", value="alpha")],
                )
            ],
            targets=["user-toml"],
        )

        with patch.object(
            app.app_server.resources.config,
            "read_fields",
            new=AsyncMock(return_value=enforced_response),
        ):
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(0.2)

        assert app.config.active_model.alias == "alpha"
        assert app._current_bottom_app == BottomApp.Input
        assert len(app.query(ModelPickerApp)) == 0


@pytest.mark.asyncio
async def test_model_picker_offers_default_row() -> None:
    """A leading Default row precedes the configured models."""
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        picker = app.query_one(ModelPickerApp)
        option_list = picker.query_one(OptionList)
        # Default + the three configured models.
        assert option_list.option_count == 4
        # A pinned model pre-highlights that model, not the Default row.
        assert picker._is_pinned is True
        assert option_list.highlighted == 1  # "alpha", offset by Default row


@pytest.mark.asyncio
async def test_model_picker_default_row_current_when_unpinned() -> None:
    app = build_test_vibe_app(config=_make_unpinned_config())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        picker = app.query_one(ModelPickerApp)
        assert picker._is_pinned is False
        # The Default row (index 0) is pre-highlighted for an unpinned user.
        assert picker.query_one(OptionList).highlighted == 0


@pytest.mark.asyncio
async def test_model_picker_select_default_unpins() -> None:
    """Selecting Default from a pinned config clears the pin (persists "")."""
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        # Highlight starts on pinned "alpha" (index 1); move up to Default.
        await pilot.press("up")
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.config.active_model_pinned is False
        assert app._current_bottom_app == BottomApp.Input
        assert len(app.query(ModelPickerApp)) == 0


@pytest.mark.asyncio
async def test_model_picker_select_default_persists_empty_alias() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_model()
        await pilot.pause(0.2)

        with patch.object(
            app.app_server.resources.config, "update", new=AsyncMock()
        ) as update_config:
            await pilot.press("up")
            await pilot.press("enter")
            await pilot.pause(0.2)

        update_config.assert_awaited_once_with({"active_model": ""})


# --- /thinking command ---


@pytest.mark.asyncio
async def test_thinking_opens_thinking_picker() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_thinking()
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.ThinkingPicker
        assert len(app.query(ThinkingPickerApp)) == 1


@pytest.mark.asyncio
async def test_thinking_picker_shows_all_levels() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_thinking()
        await pilot.pause(0.2)

        picker = app.query_one(ThinkingPickerApp)
        assert picker._thinking_levels == THINKING_LEVELS
        assert picker._current_thinking == "off"
        assert "challenges the strategy" in str(
            picker.query_one(".thinkingpicker-subtitle", NoMarkupStatic).render()
        )


@pytest.mark.asyncio
async def test_thinking_picker_escape_returns_to_input() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_thinking()
        await pilot.pause(0.2)

        await pilot.press("escape")
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.Input
        assert len(app.query(ThinkingPickerApp)) == 0


@pytest.mark.asyncio
async def test_thinking_picker_select_level() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_thinking()
        await pilot.pause(0.2)

        # Navigate down to "low" (second item) and select
        await pilot.press("down")
        with (
            patch.object(app, "_reload_config", new=AsyncMock()),
            patch.object(
                app.app_server.resources.config, "set_thinking", new=AsyncMock()
            ) as set_thinking,
        ):
            await pilot.press("enter")
            await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.Input
        assert len(app.query(ThinkingPickerApp)) == 0
        set_thinking.assert_awaited_once_with("low")


@pytest.mark.asyncio
async def test_thinking_picker_select_high() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._show_thinking()
        await pilot.pause(0.2)

        # Navigate to "high" (4th item = 3 downs from "off")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        with (
            patch.object(app, "_reload_config", new=AsyncMock()),
            patch.object(
                app.app_server.resources.config, "set_thinking", new=AsyncMock()
            ) as set_thinking,
        ):
            await pilot.press("enter")
            await pilot.pause(0.2)

        set_thinking.assert_awaited_once_with("high")


@pytest.mark.asyncio
async def test_effort_command_persists_model_and_subagent_intensity() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await app._effort_command("max")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "max", 4, "high", "medium", "off", False, False, True
        )


@pytest.mark.asyncio
async def test_effort_command_accepts_independent_direct_values() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await app._effort_command("low 12 max high medium")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "low", 12, "max", "high", "medium", False, False, True
        )


@pytest.mark.asyncio
async def test_effort_command_enables_gauntlet_independently() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await app._effort_command("medium 4 high max low on")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "medium", 4, "high", "max", "low", True, False, True
        )


@pytest.mark.asyncio
async def test_effort_command_disables_personal_experience_independently() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await app._effort_command("medium 4 high max low off off off")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "medium", 4, "high", "max", "low", False, False, False
        )


@pytest.mark.asyncio
async def test_effort_boost_direct_form_enforces_maximum_profile() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await app._effort_command("boost")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "max", 16, "max", "max", "max", True, True, True
        )


@pytest.mark.asyncio
async def test_effort_command_opens_slider_picker() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.EffortPicker
        picker = app.query_one(EffortPickerApp)
        assert "Model think" in str(
            picker.query_one("#effortpicker-thinking", Static).render()
        )
        assert "Vibe think" in str(
            picker.query_one("#effortpicker-vibe-thinking", Static).render()
        )
        assert "0 extra passes" in str(
            picker.query_one("#effortpicker-vibe-thinking", Static).render()
        )
        assert "strategic reasoning" in str(
            picker.query_one(".effortpicker-subtitle", NoMarkupStatic).render()
        )
        assert "4/16" in str(
            picker.query_one("#effortpicker-subagents", Static).render()
        )
        assert "Accuracy" in str(
            picker.query_one("#effortpicker-accuracy", Static).render()
        )
        assert "Web search" in str(
            picker.query_one("#effortpicker-web-search", Static).render()
        )
        assert "Experience" in str(
            picker.query_one("#effortpicker-experience", Static).render()
        )
        assert "Gauntlet" in str(
            picker.query_one("#effortpicker-gauntlet", Static).render()
        )
        assert "BOOST" in str(picker.query_one("#effortpicker-boost", Static).render())
        assert "UltraCode" in str(
            picker.query_one("#effortpicker-ultracode", Static).render()
        )


@pytest.mark.asyncio
async def test_effort_picker_escape_returns_to_input() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.Input
        assert len(app.query(EffortPickerApp)) == 0


@pytest.mark.asyncio
async def test_effort_picker_applies_independent_minimums() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)
        await pilot.press("down", "down", "left", "left", "left", "left")
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await pilot.press("enter")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "off", 0, "high", "medium", "off", False, False, True
        )


@pytest.mark.asyncio
async def test_effort_picker_adjusts_vibe_thinking_independently() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)
        await pilot.press("down", "right")
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await pilot.press("enter")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "off", 4, "high", "medium", "low", False, False, True
        )


@pytest.mark.asyncio
async def test_effort_picker_adjusts_accuracy_and_web_search_independently() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)
        await pilot.press("down", "down", "down", "right", "down", "right")
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await pilot.press("enter")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "off", 4, "max", "high", "off", False, False, True
        )


@pytest.mark.asyncio
async def test_effort_picker_toggles_gauntlet_independently() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)
        await pilot.press("g")
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await pilot.press("enter")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "off", 4, "high", "medium", "off", True, False, True
        )


@pytest.mark.asyncio
async def test_effort_picker_ultracode_sets_everything_to_maximum() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await pilot.press("u")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "max", 16, "max", "max", "max", True, True, True
        )


@pytest.mark.asyncio
async def test_effort_picker_boost_is_a_separate_quality_level() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._effort_command()
        await pilot.pause(0.2)
        await pilot.press("b")
        with patch.object(
            app.app_server.resources.config, "set_effort", new=AsyncMock()
        ) as set_effort:
            await pilot.press("enter")
            await pilot.pause(0.2)

        set_effort.assert_awaited_once_with(
            "max", 16, "max", "max", "max", True, True, True
        )


@pytest.mark.asyncio
async def test_ultracode_without_objective_opens_shared_effort_picker() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._ultracode_command()
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.EffortPicker
        picker = app.query_one(EffortPickerApp)
        assert "› UltraCode" in str(
            picker.query_one("#effortpicker-ultracode", Static).render()
        )


@pytest.mark.asyncio
async def test_boost_without_objective_selects_separate_effort_row() -> None:
    app = build_test_vibe_app(config=_make_config_with_models())

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app._boost_command()
        await pilot.pause(0.2)

        assert app._current_bottom_app == BottomApp.EffortPicker
        picker = app.query_one(EffortPickerApp)
        assert "› BOOST" in str(
            picker.query_one("#effortpicker-boost", Static).render()
        )
