from __future__ import annotations

import qrcode
from qrcode.constants import ERROR_CORRECT_L
from qrcode.exceptions import DataOverflowError
from rich.text import Text

MAX_TERMINAL_QR_VALUE_CHARS = 256
_QR_STYLE = "black on white"


def render_terminal_qr(value: str) -> Text | None:
    if not value or len(value) > MAX_TERMINAL_QR_VALUE_CHARS:
        return None

    qr = qrcode.QRCode(
        version=None, error_correction=ERROR_CORRECT_L, box_size=1, border=2
    )
    try:
        qr.add_data(value)
        qr.make(fit=True)
    except DataOverflowError:
        return None

    matrix = qr.get_matrix()
    lines: list[str] = []
    for row_index in range(0, len(matrix), 2):
        top = matrix[row_index]
        bottom = (
            matrix[row_index + 1] if row_index + 1 < len(matrix) else [False] * len(top)
        )
        lines.append(
            "".join(
                (
                    "█"
                    if top_dark and bottom_dark
                    else "▀"
                    if top_dark
                    else "▄"
                    if bottom_dark
                    else " "
                )
                for top_dark, bottom_dark in zip(top, bottom, strict=True)
            )
        )

    return Text("\n".join(lines), style=_QR_STYLE)


__all__ = ["MAX_TERMINAL_QR_VALUE_CHARS", "render_terminal_qr"]
