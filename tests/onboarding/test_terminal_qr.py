from __future__ import annotations

from vibe.setup.onboarding.terminal_qr import (
    MAX_TERMINAL_QR_VALUE_CHARS,
    render_terminal_qr,
)


def test_render_terminal_qr_is_bounded_and_terminal_safe() -> None:
    rendered = render_terminal_qr("https://console.mistral.ai/vibe/sign-in/process-1")

    assert rendered is not None
    text = rendered.plain
    assert text
    assert set(text) <= {" ", "▀", "▄", "█", "\n"}
    assert max(map(len, text.splitlines())) <= 64


def test_render_terminal_qr_rejects_oversized_value() -> None:
    assert render_terminal_qr("x" * (MAX_TERMINAL_QR_VALUE_CHARS + 1)) is None
