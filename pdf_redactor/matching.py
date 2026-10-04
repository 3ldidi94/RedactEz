"""Tolerant pattern building, to find a term inside a PDF.

Text extracted from a PDF rarely matches what the user typed: line breaks in
the middle of a name, non-breaking spaces, typographic dashes, curly
apostrophes, accents sometimes omitted when typing. The pattern built here
stays *aligned character by character* with the page text, which is required to
map matches back to the rectangles that must be redacted.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterator, List, Sequence, Tuple


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _build_accent_groups() -> Dict[str, str]:
    """Base letter -> every lower-case Latin letter that folds to it.

    Generated from Unicode (Latin-1 Supplement and Latin Extended-A/B) rather
    than listed by hand, so "Šimon", "Łódź" or "Dvořák" fold as well as French
    names do. A few letters have no decomposition and are added explicitly.
    """
    groups: Dict[str, set] = {}
    for code in range(0x00C0, 0x0250):
        char = chr(code).lower()
        base = strip_accents(char)
        if len(base) == 1 and "a" <= base <= "z" and base != char:
            groups.setdefault(base, {base}).add(char)
    for base, extra in {"a": "æ", "o": "øœ", "l": "ł", "d": "đ"}.items():
        groups.setdefault(base, {base}).update(extra)
    return {base: "".join(sorted(chars)) for base, chars in groups.items()}


# Accepted variants for a base character (accent folding).
_ACCENT_GROUPS = _build_accent_groups()
# Reverse index: accented (or base) letter -> its base letter.
_BASE_OF = {c: base for base, group in _ACCENT_GROUPS.items() for c in group}

# Characters commonly substituted in PDFs.
_EQUIVALENTS = {
    "-": "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212",
    "'": "'\u2018\u2019\u02bc\u00b4`",
    '"': '"\u201c\u201d\u00ab\u00bb',
    ".": ".\u2024",
}

_WHITESPACE = r"[\s\u00a0\u202f\u2007\u2009]+"
# Possible hyphenation at end of line: dash followed by a line break.
_SOFT_BREAK = r"(?:[-\u2010\u2011]?[\s\u00a0]*\n[\s\u00a0]*)?"


def normalize(value: str) -> str:
    """Canonical form of a term, used as a deduplication key."""
    value = unicodedata.normalize("NFC", value)
    value = re.sub(_WHITESPACE, " ", value)
    return value.strip()


def fold(value: str) -> str:
    """Aggressive form: no accents, lower case, collapsed spaces.

    Used for comparison and verification only, never for indexing text.
    """
    return re.sub(r"[\s\u00a0]+", " ", strip_accents(value).lower()).strip()


def _char_class(char: str, fold_accents: bool, ignore_case: bool) -> str:
    variants = {char}
    if fold_accents:
        base = _BASE_OF.get(char.lower(), "")
        variants |= set(_ACCENT_GROUPS.get(base, ""))
    if char in _EQUIVALENTS:
        variants |= set(_EQUIVALENTS[char])
    if ignore_case:
        variants |= {c.upper() for c in variants} | {c.lower() for c in variants}
    # Drop case mappings that expand to several characters ("ß" -> "SS").
    variants = {c for c in variants if len(c) == 1}
    if len(variants) == 1:
        return re.escape(char)
    return "[" + "".join(re.escape(c) for c in sorted(variants)) + "]"


def build_pattern(
    term: str,
    *,
    fold_accents: bool = True,
    ignore_case: bool = True,
    whole_word: bool = False,
    allow_line_breaks: bool = True,
) -> re.Pattern:
    """Compile a pattern that finds `term` despite layout quirks."""
    term = normalize(term)
    if not term:
        raise ValueError("empty term")

    # A hyphenation break is allowed between any two characters of a word.
    joiner = _SOFT_BREAK if allow_line_breaks else ""
    parts: List[str] = [
        joiner.join(_char_class(c, fold_accents, ignore_case) for c in chunk)
        for chunk in term.split(" ")
        if chunk
    ]
    body = _WHITESPACE.join(parts)

    if whole_word:
        body = rf"(?<![\w]){body}(?![\w])"
    return re.compile(body)


def iter_matches(text: str, pattern: re.Pattern) -> Iterator[Tuple[int, int, str]]:
    """Non-overlapping, non-empty matches."""
    pos = 0
    length = len(text)
    while pos <= length:
        match = pattern.search(text, pos)
        if match is None:
            return
        start, end = match.span()
        if end == start:
            pos = start + 1
            continue
        yield start, end, match.group(0)
        pos = end


def find_residual(
    text: str, terms: Sequence[str], whole_word: bool = False
) -> List[str]:
    """Terms still present in `text`: the post-redaction check.

    Deliberately stricter than redaction: case and accents are always ignored,
    so a variant left alone by --case-sensitive or --no-accent-folding is still
    reported. Only whole-word mode is honoured, otherwise "Martin" would always
    be "found" inside an untouched "Martinez".
    """
    return [
        term
        for term in terms
        if build_pattern(term, whole_word=whole_word).search(text)
    ]


def contains(haystack: str, term: str) -> bool:
    """Tolerant containment test (accents, case, spacing)."""
    return fold(term) in fold(haystack)
