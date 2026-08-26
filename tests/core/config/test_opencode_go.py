from __future__ import annotations

from vibe.core.config.opencode_go import (
    OPENCODE_GO_MODELS,
    OPENCODE_GO_PROVIDERS,
    OPENCODE_GO_RECOMMENDED_REVIEW_MODEL,
)


def test_opencode_go_catalog_exposes_every_documented_endpoint_model() -> None:
    aliases = {model.alias for model in OPENCODE_GO_MODELS}

    assert len(aliases) == 31
    assert OPENCODE_GO_RECOMMENDED_REVIEW_MODEL in aliases
    assert "opencode-go/grok-4.6" in aliases
    assert "opencode-go/minimax-m3" in aliases
    assert "opencode-go/deepseek-v4-flash-vision-exp" in aliases


def test_opencode_go_catalog_routes_each_api_protocol_correctly() -> None:
    providers = {provider.name: provider for provider in OPENCODE_GO_PROVIDERS}
    models = {model.alias: model for model in OPENCODE_GO_MODELS}

    assert providers[models["opencode-go/deepseek-v4-flash"].provider].api_style == (
        "openai"
    )
    assert providers[models["opencode-go/minimax-m3"].provider].api_style == (
        "anthropic"
    )
    assert providers[models["opencode-go/grok-4.6"].provider].api_style == (
        "openai-responses"
    )


def test_deepseek_flash_defaults_to_max_role_thinking() -> None:
    model = next(
        model
        for model in OPENCODE_GO_MODELS
        if model.alias == OPENCODE_GO_RECOMMENDED_REVIEW_MODEL
    )

    assert model.thinking == "max"
