"""Watermark stamped on produced documents.

Goal: a redacted copy must never be mistaken for the original. The watermark is
applied by default; `--no-watermark` disables it and `--watermark TEXT`
customises it.

It is drawn last, after redaction, for two reasons: it must not be removed by
`apply_redactions`, and it must sit on top of the redacted areas.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import date
from typing import Optional, Tuple

DEFAULT_TEXT = "REDACTED COPY"
DEFAULT_COLOR = (0.72, 0.11, 0.11)
DEFAULT_OPACITY = 0.11


def _ascii_fallback(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def footer_text(text: str = DEFAULT_TEXT) -> str:
    return f"{text} — {date.today():%Y-%m-%d} — working copy, not the original"


def apply(
    pymupdf,
    page,
    text: str = DEFAULT_TEXT,
    *,
    opacity: float = DEFAULT_OPACITY,
    color: Tuple[float, float, float] = DEFAULT_COLOR,
    footer: Optional[str] = None,
) -> Optional[str]:
    """Stamp the watermark. Returns a warning message, or None.

    Never aborts processing: a missing watermark is a cosmetic defect, not a
    data leak.
    """
    rect = page.rect
    if rect.is_empty or rect.width < 20 or rect.height < 20:
        return None

    try:
        return _draw(pymupdf, page, rect, text, opacity, color, footer)
    except Exception:
        # Font missing the requested glyphs: retry with plain ASCII.
        try:
            return _draw(
                pymupdf,
                page,
                rect,
                _ascii_fallback(text),
                opacity,
                color,
                _ascii_fallback(footer) if footer else None,
            )
        except Exception as exc:
            return f"watermark not stamped on page {page.number + 1} ({exc})"


def _draw(pymupdf, page, rect, text, opacity, color, footer) -> Optional[str]:
    diagonal = math.hypot(rect.width, rect.height)
    angle = math.degrees(math.atan2(rect.height, rect.width))

    font = pymupdf.Font("helv")
    unit = pymupdf.get_text_length(text, fontname="helv", fontsize=1) or 0.5
    fontsize = max(10.0, min(96.0, diagonal * 0.70 / unit))
    width = unit * fontsize

    writer = pymupdf.TextWriter(rect)
    start = pymupdf.Point(
        rect.x0 + (rect.width - width) / 2, rect.y0 + rect.height / 2 + fontsize * 0.35
    )
    writer.append(start, text, font=font, fontsize=fontsize)

    pivot = pymupdf.Point(rect.x0 + rect.width / 2, rect.y0 + rect.height / 2)
    # Rising direction (bottom-left to top-right) in PDF coordinates.
    matrix = pymupdf.Matrix(angle)
    writer.write_text(
        page, color=color, opacity=opacity, morph=(pivot, matrix), overlay=True
    )

    if footer:
        _draw_footer(pymupdf, page, rect, footer, color)
    return None


def _draw_footer(pymupdf, page, rect, footer, color) -> None:
    fontsize = 6.5
    width = pymupdf.get_text_length(footer, fontname="helv", fontsize=fontsize)
    if width > rect.width - 24:
        fontsize *= (rect.width - 24) / width
    writer = pymupdf.TextWriter(rect)
    writer.append(
        pymupdf.Point(rect.x0 + 12, rect.y1 - 8),
        footer,
        font=pymupdf.Font("helv"),
        fontsize=max(4.5, fontsize),
    )
    writer.write_text(page, color=color, opacity=0.55, overlay=True)
