"""Terminal output: colours, symbols, messages and tables.

Everything goes to standard error, leaving standard output free for piping.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Optional, Sequence, TextIO


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"


class UI:
    """User-facing output, centralised so it stays consistent."""

    def __init__(
        self,
        stream: Optional[TextIO] = None,
        quiet: bool = False,
        verbose: bool = False,
        color: str = "auto",
    ) -> None:
        self.stream = stream or sys.stderr
        self.quiet = quiet
        self.verbose = verbose
        self.color = self.resolve_color(color)

    # -------------------------------------------------------------- colours
    def resolve_color(self, mode: str) -> bool:
        if mode == "always":
            return True
        if mode == "never":
            return False
        if os.environ.get("NO_COLOR") is not None:
            return False
        if os.environ.get("TERM") == "dumb":
            return False
        return bool(getattr(self.stream, "isatty", lambda: False)())

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(styles) + text + Style.RESET

    @property
    def width(self) -> int:
        try:
            return max(40, min(shutil.get_terminal_size((100, 24)).columns, 120))
        except OSError:
            return 100

    # ------------------------------------------------------------- messages
    def _write(self, line: str = "") -> None:
        print(line, file=self.stream, flush=True)

    def blank(self) -> None:
        if not self.quiet:
            self._write()

    def title(self, text: str) -> None:
        if self.quiet:
            return
        self._write()
        self._write(self.paint(text, Style.BOLD))
        self._write(self.paint("─" * min(len(text), self.width), Style.DIM))

    def step(self, text: str) -> None:
        if not self.quiet:
            self._write(f"{self.paint('›', Style.CYAN)} {text}")

    def detail(self, text: str) -> None:
        if self.verbose and not self.quiet:
            self._write(self.paint(f"    {text}", Style.DIM))

    def success(self, text: str) -> None:
        if not self.quiet:
            self._write(f"{self.paint('✓', Style.GREEN)} {text}")

    def warn(self, text: str) -> None:
        self._write(f"{self.paint('⚠', Style.YELLOW)} {text}")

    def error(self, text: str) -> None:
        self._write(f"{self.paint('✖', Style.RED)} {text}")

    def hint(self, text: str) -> None:
        """Actionable advice, printed even in quiet mode after an error."""
        self._write(self.paint(f"  → {text}", Style.DIM))

    def prompt(self, text: str) -> str:
        """Question asked to the user (written on the terminal, not stderr)."""
        return input(text)

    def raw(self, text: str) -> None:
        if not self.quiet:
            self._write(text)

    # --------------------------------------------------------------- tables
    def table(self, rows: Sequence[Sequence[str]], aligns: str = "") -> None:
        """Aligned table without borders (readable and easy to copy)."""
        if self.quiet or not rows:
            return
        columns = max(len(row) for row in rows)
        widths = [0] * columns
        for row in rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(_plain(cell)))
        for row in rows:
            cells = []
            for index, cell in enumerate(row):
                padding = widths[index] - len(_plain(cell))
                align = aligns[index] if index < len(aligns) else "l"
                cells.append(
                    " " * padding + cell if align == "r" else cell + " " * padding
                )
            self._write("  " + "  ".join(cells).rstrip())


def _plain(text: str) -> str:
    """Visible length: strips ANSI escape sequences."""
    out, index = [], 0
    while index < len(text):
        if text[index] == "\033":
            while index < len(text) and text[index] != "m":
                index += 1
            index += 1
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: width - 1] + ellipsis


def plural(count: int, singular: str, plural_form: Optional[str] = None) -> str:
    word = singular if abs(count) <= 1 else (plural_form or singular + "s")
    return f"{count} {word}"

