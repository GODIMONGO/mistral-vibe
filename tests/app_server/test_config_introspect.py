from __future__ import annotations

from collections.abc import Callable

import pytest

from vibe.app_server._config_introspect import (
    DEFAULT_ORIGIN,
    HIDDEN_SETTINGS,
    POPULAR_SETTINGS,
    REDACTED_VALUE,
    build_field_wires,
    classify_annotation,
    collect_layer_values,
    redact_config_value,
)
from vibe.app_server.protocol import ConfigFieldKind, ConfigLayerValueWire
from vibe.core.config.layers.overrides import OverridesLayer
from vibe.core.config.layers.user import UserConfigLayer
from vibe.core.config.models import ModelConfig, OtelRedactionMode, ProviderConfig
from vibe.core.config.vibe_schema import VibeConfigSchema


def test_popular_settings_are_valid_fields() -> None:
    unknown = POPULAR_SETTINGS - set(VibeConfigSchema.model_fields)
    assert not unknown, f"POPULAR_SETTINGS names not in schema: {sorted(unknown)}"


def test_hidden_settings_are_valid_fields() -> None:
    unknown = HIDDEN_SETTINGS - set(VibeConfigSchema.model_fields)
    assert not unknown, f"HIDDEN_SETTINGS names not in schema: {sorted(unknown)}"


@pytest.mark.parametrize(
    ("annotation", "kind"),
    [
        (bool, ConfigFieldKind.BOOL),
        (int, ConfigFieldKind.INT),
        (float, ConfigFieldKind.FLOAT),
        (str, ConfigFieldKind.STR),
        (str | None, ConfigFieldKind.STR),
        (list[str], ConfigFieldKind.LIST),
        (list[int], ConfigFieldKind.LIST),
        (dict[str, int], ConfigFieldKind.COMPLEX),
        (list[ModelConfig], ConfigFieldKind.COMPLEX),
        (ModelConfig | None, ConfigFieldKind.COMPLEX),
    ],
)
def test_classify_annotation(annotation: object, kind: ConfigFieldKind) -> None:
    assert classify_annotation(annotation)[0] is kind


def test_classify_enum_lists_choices() -> None:
    kind, choices = classify_annotation(OtelRedactionMode)
    assert kind is ConfigFieldKind.ENUM
    assert set(choices) == {mode.value for mode in OtelRedactionMode}


def test_build_field_wires_covers_schema_and_defaults(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    config = make_config()
    by_name = {wire.name: wire for wire in build_field_wires(config, {})}

    assert by_name.keys() == set(type(config).model_fields) - HIDDEN_SETTINGS
    assert not HIDDEN_SETTINGS & by_name.keys()
    assert by_name["autocopy_to_clipboard"].kind is ConfigFieldKind.BOOL
    assert by_name["otel_redaction"].kind is ConfigFieldKind.ENUM
    assert by_name["models"].kind is ConfigFieldKind.COMPLEX
    assert by_name["theme"].path == "/theme"
    assert all(wire.origin == DEFAULT_ORIGIN for wire in by_name.values())


def test_tracing_settings_are_public_and_described(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    config = make_config()
    by_name = {wire.name: wire for wire in build_field_wires(config, {})}

    for name in ("enable_otel", "otel_endpoint", "otel_redaction"):
        assert name in by_name
        assert by_name[name].description


def test_build_field_wires_resolves_layers(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    config = make_config()
    wires = build_field_wires(
        config, {"theme": [ConfigLayerValueWire(layer="user-toml", value="dracula")]}
    )
    by_name = {wire.name: wire for wire in wires}

    # Model/theme choices are resolved client-side, so the server keeps them STR.
    assert by_name["active_model"].kind is ConfigFieldKind.STR
    assert by_name["theme"].origin == "user-toml"
    assert by_name["theme"].layer_values[-1].layer == DEFAULT_ORIGIN


def test_redact_config_value_preserves_credential_references() -> None:
    value = {
        "api_key_env_var": "SMART_API_KEY",
        "extra_headers": {
            "Authorization": "Bearer server-secret",
            "X-Api-Key": "header-secret",
            "User-Agent": "vibe-test",
        },
    }

    redacted = redact_config_value(value)

    assert redacted["api_key_env_var"] == "SMART_API_KEY"
    assert redacted["extra_headers"] == {
        "Authorization": REDACTED_VALUE,
        "X-Api-Key": REDACTED_VALUE,
        "User-Agent": "vibe-test",
    }


def test_build_field_wires_redacts_provider_headers_and_layers(
    make_config: Callable[..., VibeConfigSchema],
) -> None:
    secret = "sentinel-provider-secret"
    provider = ProviderConfig(
        name="smart",
        api_base="https://smart.example/v1",
        api_key_env_var="SMART_API_KEY",
        extra_headers={"Authorization": f"Bearer {secret}"},
    )
    config = make_config(providers=[provider])
    layer_value = provider.model_dump(mode="json")
    wires = build_field_wires(
        config,
        {"providers": [ConfigLayerValueWire(layer="user-toml", value=[layer_value])]},
    )
    providers = next(wire for wire in wires if wire.name == "providers")
    serialized = providers.model_dump_json()

    assert secret not in serialized
    assert "SMART_API_KEY" in serialized
    assert REDACTED_VALUE in serialized


@pytest.mark.asyncio
async def test_collect_layer_values_groups_fields_by_priority(tmp_path) -> None:
    missing = UserConfigLayer(path=tmp_path / "missing.toml", name="missing")
    low = OverridesLayer(data={"theme": "a"}, name="user-toml")
    high = OverridesLayer(data={"theme": "b", "api_timeout": 1.0}, name="overrides")

    values = await collect_layer_values([missing, low, high])

    assert [(entry.layer, entry.value) for entry in values["theme"]] == [
        ("overrides", "b"),
        ("user-toml", "a"),
    ]
    assert [(entry.layer, entry.value) for entry in values["api_timeout"]] == [
        ("overrides", 1.0)
    ]
