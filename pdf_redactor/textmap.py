"""Page representation: text plus the position of every character.

This is the cornerstone of the tool: *detection* (regex) and *redaction*
(rectangles to erase) work on exactly the same string, which guarantees that
anything detected can also be redacted.

No PDF dependency here: a future backend (DOCX, OCR'd images, ...) can provide
a `PageText` without geometry (`boxes=[]`) for detection only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]


@dataclass(frozen=True)
class CharBox:
    """Position of a single character on the page."""

    char: str
    rect: Rect
    line_id: int


@dataclass
class PageText:
    """Page text plus the position of every character.

    `boxes` is index-aligned with `text`. A `None` entry is a synthetic
    separator (line break) with no geometry.
    """

    number: int  # 0-based index
    text: str
    boxes: List[Optional[CharBox]] = field(default_factory=list)
    has_images: bool = False
    label: str = ""  # e.g. "page 3"
    from_ocr: bool = False  # text reconstructed by OCR (scanned page)

    def __post_init__(self) -> None:
        if self.boxes and len(self.boxes) != len(self.text):
            raise ValueError(
                "textmap mismatch: %d characters for %d positions"
                % (len(self.text), len(self.boxes))
            )
        if not self.label:
            self.label = f"page {self.number + 1}"

    @property
    def has_geometry(self) -> bool:
        return bool(self.boxes)

    def context(self, start: int, end: int, width: int = 40) -> str:
        """Readable excerpt around a match, shown when prompting the user."""
        left = self.text[max(0, start - width) : start].replace("\n", " ")
        mid = self.text[start:end].replace("\n", " ")
        right = self.text[end : end + width].replace("\n", " ")
        prefix = "…" if start - width > 0 else ""
        suffix = "…" if end + width < len(self.text) else ""
        return f"{prefix}{left}[{mid}]{right}{suffix}".strip()

    def rects_for(self, start: int, end: int, pad_x: float = 0.4) -> List[Rect]:
        """Rectangles covering text[start:end], one per physical line."""
        if not self.boxes:
            return []
        per_line: dict[int, List[float]] = {}
        for i in range(start, min(end, len(self.boxes))):
            box = self.boxes[i]
            if box is None:
                continue
            current = per_line.get(box.line_id)
            if current is None:
                per_line[box.line_id] = list(box.rect)
            else:
                current[0] = min(current[0], box.rect[0])
                current[1] = min(current[1], box.rect[1])
                current[2] = max(current[2], box.rect[2])
                current[3] = max(current[3], box.rect[3])
        out: List[Rect] = []
        for _, r in sorted(per_line.items()):
            if r[2] - r[0] <= 0 or r[3] - r[1] <= 0:
                continue  # degenerate box (non-breaking space, empty glyph)
            out.append((r[0] - pad_x, r[1], r[2] + pad_x, r[3]))
        return out


def expand_for_pixels(rect: Rect, ratio: float = 0.15, minimum: float = 1.0) -> Rect:
    """Safety margin applied before erasing pixels.

    Two reasons to overshoot:
    - OCR boxes hug the glyphs (accents and descenders stick out);
    - on an already-OCR'd scan the text layer is not perfectly aligned with the
      image, so erasing flush with the box would leave the top of the letters
      readable.

    The margin is proportional to the line height, so it follows the font size
    instead of being tuned for one specific document.
    """
    dy = max(minimum, (rect[3] - rect[1]) * ratio)
    return (rect[0] - minimum, rect[1] - dy, rect[2] + minimum, rect[3] + dy)


def shrink_vertically(rect: Rect, ratio: float = 0.18, minimum: float = 2.0) -> Rect:
    """Reduce a rectangle's height, leaving its width untouched.

    Required before redacting text: the box of one line touches the boxes of
    the lines above and below, and the engine removes every glyph that
    *touches* the area. Without this inset, redacting one word also erases the
    words directly above and below it, at the same horizontal position.
    """
    height = rect[3] - rect[1]
    inset = min(height * ratio, max(0.0, (height - minimum) / 2))
    return (rect[0], rect[1] + inset, rect[2], rect[3] - inset)


class PageTextBuilder:
    """Builds a PageText by appending characters and separators."""

    def __init__(self, number: int, label: str = "") -> None:
        self.number = number
        self.label = label
        self._chars: List[str] = []
        self._boxes: List[Optional[CharBox]] = []
        self._line_id = 0
        self.has_images = False

    def new_line(self) -> None:
        self._line_id += 1

    def add_char(self, char: str, rect: Rect) -> None:
        # A "character" can span several code points (ligatures decomposed by
        # the engine): the same box is repeated to keep the mapping aligned.
        for c in char:
            self._chars.append(c)
            self._boxes.append(CharBox(c, rect, self._line_id))

    def add_separator(self, sep: str = "\n") -> None:
        for c in sep:
            self._chars.append(c)
            self._boxes.append(None)

    def build(self, from_ocr: bool = False) -> PageText:
        return PageText(
            number=self.number,
            text="".join(self._chars),
            boxes=self._boxes,
            has_images=self.has_images,
            label=self.label,
            from_ocr=from_ocr,
        )


def _same_line(a: Rect, b: Rect, min_ratio: float = 0.6) -> bool:
    """True when two rectangles visually belong to the same line.

    Consecutive lines have glyph boxes that *touch*; merging on mere contact
    would produce a block spanning several lines and wipe out neighbouring
    text. A substantial vertical overlap is therefore required.
    """
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    if overlap <= 0:
        return False
    smallest = min(a[3] - a[1], b[3] - b[1])
    return smallest > 0 and overlap / smallest >= min_ratio


def merge_line_rects(
    items: Sequence[Tuple[Rect, int]], gap: float = 2.0
) -> List[Tuple[Rect, int]]:
    """Merge adjacent rectangles that belong to the same line.

    `items` pairs each rectangle with a length (number of masked characters),
    later used to size the truncation pattern. Lengths of merged rectangles are
    summed.

    Sorting by ordinate then abscissa means only the current band has to be
    compared against: O(n log n) instead of the O(n²) of a naive pairwise
    merge, which matters on documents with hundreds of redacted areas.
    """
    if not items:
        return []

    bands: List[List[Tuple[Rect, int]]] = []
    for rect, length in sorted(items, key=lambda it: (round(it[0][1], 1), it[0][0])):
        if bands and _same_line(bands[-1][-1][0], rect):
            bands[-1].append((rect, length))
        else:
            bands.append([(rect, length)])

    merged: List[Tuple[Rect, int]] = []
    for band in bands:
        # Ordinates within a band differ slightly (mixed fonts), so the global
        # sort does not guarantee left-to-right order. Without this re-sort a
        # right-hand rectangle followed by a left-hand one would be merged into
        # one area covering — and erasing — all the text between them.
        band.sort(key=lambda it: it[0][0])
        current_rect, current_len = band[0]
        for rect, length in band[1:]:
            if rect[0] - gap <= current_rect[2]:  # horizontally contiguous
                current_rect = (
                    min(current_rect[0], rect[0]),
                    min(current_rect[1], rect[1]),
                    max(current_rect[2], rect[2]),
                    max(current_rect[3], rect[3]),
                )
                current_len += length
            else:
                merged.append((current_rect, current_len))
                current_rect, current_len = rect, length
        merged.append((current_rect, current_len))
    return merged

