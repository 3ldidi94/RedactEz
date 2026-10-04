"""Progress bar, with no external dependency.

Disables itself automatically outside a terminal (pipe, CI, redirection) so it
never pollutes logs: in that case nothing is drawn and only the final summary
is printed.
"""

from __future__ import annotations

import sys
import time
from typing import Optional, TextIO

_FULL = "█"
_EMPTY = "░"
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ProgressBar:
    """`[████░░░░] 45% (9/20) detail`, redrawn in place."""

    def __init__(
        self,
        total: int,
        label: str = "",
        stream: Optional[TextIO] = None,
        enabled: bool = True,
        width: int = 24,
        min_interval: float = 0.06,
    ) -> None:
        self.total = max(0, int(total))
        self.label = label
        self.stream = stream or sys.stderr
        self.width = width
        self.min_interval = min_interval
        self.current = 0
        self.detail = ""
        self._last_draw = 0.0
        self._last_len = 0
        self._tick = 0
        self._closed = False
        self.enabled = bool(
            enabled
            and self.total > 0
            and getattr(self.stream, "isatty", lambda: False)()
        )

    # ------------------------------------------------------------------ API
    def __enter__(self) -> "ProgressBar":
        self.draw(force=True)
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def update(self, step: int = 1, detail: Optional[str] = None) -> None:
        self.current = min(self.total, self.current + step)
        if detail is not None:
            self.detail = detail
        self.draw()

    def set_detail(self, detail: str) -> None:
        self.detail = detail
        self.draw()

    def close(self) -> None:
        """Erase the bar; the caller then prints its own summary."""
        if self._closed:
            return
        self._closed = True
        if self.enabled:
            self.stream.write("\r" + " " * self._last_len + "\r")
            self.stream.flush()

    # ------------------------------------------------------------- rendering
    def draw(self, force: bool = False) -> None:
        if not self.enabled or self._closed:
            return
        now = time.monotonic()
        if not force and now - self._last_draw < self.min_interval:
            if self.current < self.total:
                return
        self._last_draw = now
        self._tick += 1

        ratio = self.current / self.total if self.total else 1.0
        filled = int(round(ratio * self.width))
        spinner = (
            _SPINNER[self._tick % len(_SPINNER)] if self.current < self.total else "✓"
        )
        counter = f"({self.current}/{self.total})"
        head = (
            f"{spinner} {self.label} {_FULL * filled}{_EMPTY * (self.width - filled)} "
            f"{ratio * 100:3.0f}% {counter}"
        )
        try:
            columns = max(40, min(_terminal_width(), 120))
        except Exception:
            columns = 100
        room = columns - len(head) - 2
        detail = ""
        if self.detail and room > 6:
            detail = " " + (
                self.detail
                if len(self.detail) <= room
                else "…" + self.detail[-(room - 1) :]
            )
        line = head + detail
        padding = max(0, self._last_len - len(line))
        self.stream.write("\r" + line + " " * padding)
        self.stream.flush()
        self._last_len = len(line)


def _terminal_width() -> int:
    import shutil

    return shutil.get_terminal_size((100, 24)).columns


class NullProgress:
    """Inert bar: same interface, draws nothing."""

    def __enter__(self) -> "NullProgress":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def update(self, step: int = 1, detail: Optional[str] = None) -> None:
        return None

    def set_detail(self, detail: str) -> None:
        return None

    def close(self) -> None:
        return None

    def draw(self, force: bool = False) -> None:
        return None
