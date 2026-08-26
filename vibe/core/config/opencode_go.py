from __future__ import annotations

from dataclasses import dataclass

from vibe.core.config.models import ModelConfig, ProviderConfig
from vibe.core.types import Backend
from vibe.opencode_go import (
    OPENCODE_GO_API_KEY_ENV_VAR,
    OPENCODE_GO_MODEL_PREFIX,
    OPENCODE_GO_RECOMMENDED_REVIEW_MODEL,
)

_OPENAI_PROVIDER = "opencode-go-openai"
_ANTHROPIC_PROVIDER = "opencode-go-anthropic"
_RESPONSES_PROVIDER = "opencode-go-responses"


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    model_id: str
    display_name: str
    provider: str = _OPENAI_PROVIDER
    supports_images: bool = False


_CATALOG = (
    _CatalogEntry("grok-4.6", "Grok 4.6", _RESPONSES_PROVIDER),
    _CatalogEntry("grok-4.5", "Grok 4.5", _RESPONSES_PROVIDER),
    _CatalogEntry("gpt-5.6-luna", "GPT 5.6 Luna", _RESPONSES_PROVIDER),
    _CatalogEntry("glm-5.3", "GLM-5.3"),
    _CatalogEntry("glm-5.2", "GLM-5.2"),
    _CatalogEntry("glm-5.1", "GLM-5.1"),
    _CatalogEntry("glm-5", "GLM-5"),
    _CatalogEntry("kimi-k3", "Kimi K3"),
    _CatalogEntry("kimi-k2.7-code", "Kimi K2.7 Code"),
    _CatalogEntry("kimi-k2.6", "Kimi K2.6"),
    _CatalogEntry("kimi-k2.5", "Kimi K2.5"),
    _CatalogEntry("longcat-2.0", "LongCat-2.0"),
    _CatalogEntry("mimo-v2.5", "MiMo-V2.5"),
    _CatalogEntry("mimo-v2.5-pro", "MiMo-V2.5-Pro"),
    _CatalogEntry("mimo-v2-pro", "MiMo-V2-Pro"),
    _CatalogEntry("mimo-v2-omni", "MiMo-V2-Omni", supports_images=True),
    _CatalogEntry("minimax-m3", "MiniMax M3", _ANTHROPIC_PROVIDER),
    _CatalogEntry("minimax-m2.7", "MiniMax M2.7", _ANTHROPIC_PROVIDER),
    _CatalogEntry("minimax-m2.5", "MiniMax M2.5", _ANTHROPIC_PROVIDER),
    _CatalogEntry(
        "muse-spark-1.2-contributor", "Muse Spark 1.2 Contributor", _RESPONSES_PROVIDER
    ),
    _CatalogEntry("qwen3.8-max", "Qwen3.8 Max", _ANTHROPIC_PROVIDER),
    _CatalogEntry("qwen3.7-max", "Qwen3.7 Max", _ANTHROPIC_PROVIDER),
    _CatalogEntry("qwen3.7-plus", "Qwen3.7 Plus", _ANTHROPIC_PROVIDER),
    _CatalogEntry("qwen3.6-plus", "Qwen3.6 Plus", _ANTHROPIC_PROVIDER),
    _CatalogEntry("qwen3.5-plus", "Qwen3.5 Plus", _ANTHROPIC_PROVIDER),
    _CatalogEntry("deepseek-v4-pro", "DeepSeek V4 Pro"),
    _CatalogEntry("deepseek-v4-flash", "DeepSeek V4 Flash"),
    _CatalogEntry(
        "deepseek-v4-flash-vision-exp",
        "DeepSeek V4 Flash Vision Exp",
        supports_images=True,
    ),
    _CatalogEntry("hy3", "Hy3"),
    _CatalogEntry("hy3-preview", "Hy3 Preview"),
    _CatalogEntry("ox-alpha-free", "Ox Alpha Free"),
)


OPENCODE_GO_PROVIDERS = (
    ProviderConfig(
        name=_OPENAI_PROVIDER,
        api_base="https://opencode.ai/zen/go/v1",
        api_key_env_var=OPENCODE_GO_API_KEY_ENV_VAR,
        api_style="openai",
        backend=Backend.GENERIC,
        supports_reasoning_effort=False,
    ),
    ProviderConfig(
        name=_ANTHROPIC_PROVIDER,
        api_base="https://opencode.ai/zen/go",
        api_key_env_var=OPENCODE_GO_API_KEY_ENV_VAR,
        api_style="anthropic",
        backend=Backend.GENERIC,
        supports_reasoning_effort=False,
    ),
    ProviderConfig(
        name=_RESPONSES_PROVIDER,
        api_base="https://opencode.ai/zen/go/v1",
        api_key_env_var=OPENCODE_GO_API_KEY_ENV_VAR,
        api_style="openai-responses",
        backend=Backend.GENERIC,
    ),
)


OPENCODE_GO_MODELS = tuple(
    ModelConfig(
        name=entry.model_id,
        provider=entry.provider,
        alias=f"{OPENCODE_GO_MODEL_PREFIX}{entry.model_id}",
        display_name=f"{entry.display_name} (OpenCode Go)",
        temperature=0.2,
        thinking=("max" if entry.model_id == "deepseek-v4-flash" else "off"),
        supports_images=entry.supports_images,
    )
    for entry in _CATALOG
)


__all__ = [
    "OPENCODE_GO_API_KEY_ENV_VAR",
    "OPENCODE_GO_MODELS",
    "OPENCODE_GO_MODEL_PREFIX",
    "OPENCODE_GO_PROVIDERS",
    "OPENCODE_GO_RECOMMENDED_REVIEW_MODEL",
]
