from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

REDACTED_CONFIG_VALUE = "[redacted]"

_SAFE_REFERENCE_KEYS = frozenset({
    "api_key_env",
    "api_key_env_var",
    "api_key_header",
    "api_key_format",
})
_OPAQUE_VALUE_MAPS = frozenset({"env", "extra_headers", "headers"})
_SENSITIVE_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "x_api_key",
})


def _normalized_key(key: object) -> str:
    if not isinstance(key, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _is_sensitive_key(key: object) -> bool:
    normalized = _normalized_key(key)
    if not normalized or normalized in _SAFE_REFERENCE_KEYS:
        return False
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(("_api_key", "_password", "_secret", "_token"))


def _redact_mapping_values(value: Mapping[object, Any]) -> dict[object, str]:
    return {key: REDACTED_CONFIG_VALUE for key in value}


def session_safe_config_snapshot(value: Any) -> Any:
    """Return a JSON-compatible config snapshot without credential material.

    Session metadata is diagnostic/resume context, not a secret store. Keep
    credential references such as environment-variable names, while removing
    direct secrets and every value in provider/MCP header and process-env maps.
    """
    if isinstance(value, Mapping):
        redacted: dict[object, Any] = {}
        for key, item in value.items():
            normalized = _normalized_key(key)
            if _is_sensitive_key(key):
                redacted[key] = REDACTED_CONFIG_VALUE
            elif normalized in _OPAQUE_VALUE_MAPS and isinstance(item, Mapping):
                redacted[key] = _redact_mapping_values(item)
            else:
                redacted[key] = session_safe_config_snapshot(item)
        return redacted
    if isinstance(value, list):
        return [session_safe_config_snapshot(item) for item in value]
    if isinstance(value, tuple):
        return tuple(session_safe_config_snapshot(item) for item in value)
    return value


__all__ = ["REDACTED_CONFIG_VALUE", "session_safe_config_snapshot"]
