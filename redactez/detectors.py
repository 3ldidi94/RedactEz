"""Detection of personal-data candidates in a text.

Each rule is a regex plus, where possible, a validator (checksum) to cut down
on false positives. Rules recognise both French and English keywords: the
codebase is English, but the documents this tool was built for are often
French, and street types or field labels are language specific.

IMPORTANT: automatic detection is never exhaustive. That is why the tool asks
the user to confirm and lets terms be added by hand. This module must never be
treated as a guarantee of anonymisation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .matching import build_pattern, iter_matches, normalize
from .textmap import PageText

Confidence = str  # "high" | "medium" | "low"
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

_MONTHS = (
    "janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    "septembre|octobre|novembre|décembre|decembre|"
    "january|february|march|april|may|june|july|august|"
    "september|october|november|december"
)
_UPPER = "A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸÆŒ"
_LOWER = "a-zàâäçéèêëîïôöùûüÿæœ"
_NAME = rf"[{_UPPER}][{_UPPER}{_LOWER}'’\-]+"
# Between the words of one name: spaces only. With \s a name ran onto the next
# line ("Mme Jane DOE" + "Né le ..." gave "Jane DOE Né").
_GAP = r"[ \t ]+"


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------
def _luhn(digits: str) -> bool:
    digits = re.sub(r"\D", "", digits)
    if len(digits) < 12:
        return False
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_valid(value: str) -> bool:
    compact = re.sub(r"[\s\u00a0]", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    converted = "".join(
        str(ord(c) - 55) if c.isalpha() else c for c in rearranged
    )
    try:
        return int(converted) % 97 == 1
    except ValueError:
        return False


def _fr_ssn_valid(value: str) -> bool:
    """French social security number (NIR): validate the 2-digit checksum."""
    compact = re.sub(r"[\s\u00a0.\-]", "", value).upper()
    if len(compact) == 13:  # without checksum: accept, cannot verify
        return bool(re.fullmatch(r"[12][0-9]{5}(?:[0-9]{2}|2[AB])[0-9]{6}", compact))
    if len(compact) != 15:
        return False
    body, key = compact[:13], compact[13:]
    if not key.isdigit():
        return False
    corsica = {"2A": "19", "2B": "18"}
    numeric = body
    for code, replacement in corsica.items():
        if body[5:7] == code:
            numeric = body[:5] + replacement + body[7:]
            break
    if not numeric.isdigit():
        return False
    return int(key) == 97 - (int(numeric) % 97)


def _luhn_exact(length: int) -> Callable[[str], bool]:
    """Luhn on exactly `length` digits (SIREN = 9, SIRET = 14)."""

    def check(value: str) -> bool:
        digits = re.sub(r"\D", "", value)
        if len(digits) != length or len(set(digits)) == 1:
            return False
        total = 0
        for index, char in enumerate(reversed(digits)):
            n = int(char) * (2 if index % 2 else 1)
            total += n - 9 if n > 9 else n
        return total % 10 == 0

    return check


def _fr_tax_id_valid(value: str) -> bool:
    """French tax number (SPI): 13 digits, key = first 10 digits mod 511."""
    digits = re.sub(r"\D", "", value)
    if len(digits) != 13 or digits[0] not in "0123":
        return False
    return int(digits[:10]) % 511 == int(digits[10:])


def _be_nrn_valid(value: str) -> bool:
    """Belgian national register number: mod 97 key, born before or after 2000."""
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11:
        return False
    base, key = int(digits[:9]), int(digits[9:])
    return key in (97 - base % 97, 97 - (2_000_000_000 + base) % 97)


def _us_ssn_valid(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def _date_plausible(value: str) -> bool:
    match = re.match(r"^\s*(\d{1,2})\D+(\d{1,2})\D+(\d{2,4})", value)
    if not match:
        return True  # spelled-out dates are already constrained by the regex
    day, month = int(match.group(1)), int(match.group(2))
    return 1 <= day <= 31 and 1 <= month <= 12


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern
    confidence: Confidence = "high"
    group: int = 0  # capturing group holding the sensitive value
    validator: Optional[Callable[[str], bool]] = None
    description: str = ""
    enabled_by_default: bool = True


RULES: List[Rule] = [
    Rule(
        kind="EMAIL",
        pattern=re.compile(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
        ),
        description="email address",
    ),
    Rule(
        kind="FR_SSN",
        pattern=re.compile(
            r"(?<![0-9])[12][\s.\-]?[0-9]{2}[\s.\-]?(?:0[1-9]|1[0-2]|[2-9][0-9])"
            r"[\s.\-]?(?:[0-9]{2}|2[AB])[\s.\-]?[0-9]{3}[\s.\-]?[0-9]{3}"
            r"(?:[\s.\-]?[0-9]{2})?(?![0-9])"
        ),
        validator=_fr_ssn_valid,
        description="French social security number (NIR)",
    ),
    Rule(
        kind="IBAN",
        pattern=re.compile(
            r"(?<![A-Z0-9])[A-Z]{2}[0-9]{2}(?:[\s\u00a0]?[A-Z0-9]{2,4}){2,8}(?![A-Z0-9])"
        ),
        validator=_iban_valid,
        description="IBAN",
    ),
    Rule(
        kind="CREDIT_CARD",
        pattern=re.compile(r"(?<![0-9])(?:[0-9]{4}[\s\-]?){3}[0-9]{1,4}(?![0-9])"),
        validator=_luhn,
        description="payment card number",
    ),
    Rule(
        kind="PHONE",
        pattern=re.compile(
            r"(?<![0-9A-Za-z+])(?:(?:\+|00)[\s.\-]?[1-9][0-9]{0,2}[\s.\-]?)?"
            r"(?:\(0\)[\s.\-]?)?0?[1-9](?:[\s.\-]?[0-9]){8}(?![0-9])"
        ),
        confidence="medium",
        description="phone number",
    ),
    Rule(
        kind="DATE",
        pattern=re.compile(
            r"(?<![0-9/])(?:[0-3]?[0-9][/.\-][0-1]?[0-9][/.\-](?:19|20)?[0-9]{2}"
            rf"|[0-3]?[0-9](?:er|st|nd|rd|th)?\s+(?:{_MONTHS})\s+(?:19|20)[0-9]{{2}})"
            r"(?![0-9/])",
            re.IGNORECASE,
        ),
        confidence="medium",
        validator=_date_plausible,
        description="date (birth date, etc.)",
    ),
    Rule(
        kind="LABELED_FIELD",
        # "Nom : DUPONT", "Date of birth: 12/03/1980", "Address: ..."
        # The label must start a word: "Hôtel :" is not "tél :", "username:"
        # is not "name:".
        pattern=re.compile(
            r"(?<![\w])(?:nom(?:\s+de\s+famille|\s+d[e']usage|\s+de\s+naissance)?|pr[ée]nom(?:s)?"
            r"|n[ée](?:\(e\))?\s+le|date\s+de\s+naissance|lieu\s+de\s+naissance"
            r"|adresse(?:\s+postale)?|domicile|domicili[ée](?:\s+[àa])?"
            r"|t[ée]l(?:[ée]phone)?|courriel|nationalit[ée]|matricule|identifiant"
            r"|num[ée]ro\s+de\s+s[ée]curit[ée]\s+sociale"
            r"|(?:full|first|last|maiden|family)\s+name|name|surname|forename"
            r"|passeport|carte\s+d['’]identit[ée]|cni|num[ée]ro\s+fiscal"
            r"|permis\s+de\s+conduire"
            r"|date\s+of\s+birth|place\s+of\s+birth|born(?:\s+on)?|address"
            r"|phone(?:\s+number)?|e[\-\s]?mail|nationality|ssn|social\s+security"
            r"|passport(?:\s+(?:no\.?|number))?|id(?:entity)?\s+card(?:\s+number)?"
            r"|tax\s+id|driver['’]?s?\s+licen[cs]e(?:\s+number)?"
            r"|national\s+insurance(?:\s+number)?)"
            r"\s*[:：]\s*(?P<value>[^\n]{2,80})",
            re.IGNORECASE,
        ),
        group=1,
        confidence="high",
        description="value following a personal-data label",
    ),
    Rule(
        kind="ADDRESS",
        pattern=re.compile(
            r"(?<![\w])[0-9]{1,4}\s?(?:bis|ter|quater)?[,\s]+"
            r"(?:rue|avenue|av\.|boulevard|bd\.?|impasse|all[ée]e|chemin|place|"
            r"route|quai|cours|voie|square|r[ée]sidence|lieu[\-\s]dit|villa|passage|"
            r"street|st\.?|road|rd\.?|drive|dr\.?|lane|court|way|close|terrace)"
            r"[^\n,;.:«»()]{2,45}"
            rf"(?:,?[ \u00a0]*[0-9]{{5}}[ \u00a0]+{_NAME}(?:[ \u00a0\-]{_NAME}){{0,2}})?",
            re.IGNORECASE,
        ),
        confidence="medium",
        description="postal address",
    ),
    Rule(
        kind="POSTAL_CODE_CITY",
        pattern=re.compile(
            rf"(?<![0-9])[0-9]{{5}}[ \u00a0]+{_NAME}(?:[ \u00a0\-]{_NAME}){{0,2}}"
        ),
        confidence="low",
        description="postal code followed by a city",
    ),
    Rule(
        kind="NAME_WITH_TITLE",
        pattern=re.compile(
            rf"(?:M\.|MM\.|Mme|Mmes|Mlle|Monsieur|Madame|Mademoiselle|Me|Dr|Pr|"
            rf"Mr\.?|Mrs\.?|Ms\.?|Miss|Prof\.?)\s+"
            rf"(?P<value>{_NAME}(?:{_GAP}{_NAME}){{0,3}})"
        ),
        group=1,
        confidence="medium",
        description="name preceded by a title",
    ),
    Rule(
        kind="UPPERCASE_NAME",
        pattern=re.compile(
            rf"(?<![\w]){_NAME}{_GAP}[{_UPPER}]{{2,}}(?:(?:{_GAP}|-)[{_UPPER}]{{2,}})*(?![\w])"
        ),
        confidence="low",
        description="run of uppercase words (likely a surname)",
        enabled_by_default=False,
    ),
    Rule(
        kind="LICENSE_PLATE",
        pattern=re.compile(
            r"(?<![A-Z0-9])[A-Z]{2}[\s\-][0-9]{3}[\s\-][A-Z]{2}(?![A-Z0-9])"
        ),
        confidence="medium",
        description="vehicle registration plate",
    ),
    Rule(
        kind="IP",
        pattern=re.compile(
            r"(?<![\d.])(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}"
            r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(?![\d.])"
        ),
        confidence="medium",
        description="IPv4 address",
    ),
    Rule(
        kind="FR_TAX_ID",
        pattern=re.compile(
            r"(?<![0-9])[0-3][0-9][\s ]?[0-9]{2}[\s ]?[0-9]{3}"
            r"[\s ]?[0-9]{3}[\s ]?[0-9]{3}(?![0-9])"
        ),
        validator=_fr_tax_id_valid,
        description="French tax number (SPI)",
    ),
    Rule(
        kind="FR_SIRET",
        pattern=re.compile(
            r"(?<![0-9])[0-9]{3}[\s ]?[0-9]{3}[\s ]?[0-9]{3}"
            r"[\s ]?[0-9]{5}(?![0-9])"
        ),
        validator=_luhn_exact(14),
        confidence="medium",
        description="French business establishment number (SIRET)",
    ),
    Rule(
        kind="FR_SIREN",
        pattern=re.compile(
            r"(?<![0-9])[0-9]{3}[\s ]?[0-9]{3}[\s ]?[0-9]{3}(?![0-9])"
        ),
        validator=_luhn_exact(9),
        # 1 random 9-digit number in 10 passes Luhn: low confidence.
        confidence="low",
        description="French business number (SIREN)",
    ),
    Rule(
        kind="FR_PASSPORT",
        pattern=re.compile(r"(?<![A-Z0-9])[0-9]{2}[A-Z]{2}[0-9]{5}(?![A-Z0-9])"),
        confidence="medium",
        description="French passport number",
    ),
    Rule(
        kind="BE_NRN",
        pattern=re.compile(
            r"(?<![0-9])[0-9]{2}[.\s]?[0-9]{2}[.\s]?[0-9]{2}[\-\s]?[0-9]{3}"
            r"[.\s]?[0-9]{2}(?![0-9])"
        ),
        validator=_be_nrn_valid,
        description="Belgian national register number",
    ),
    Rule(
        kind="UK_NINO",
        pattern=re.compile(
            r"(?<![A-Z0-9])(?!BG|GB|KN|NK|NT|TN|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]"
            r"\s?[0-9]{2}\s?[0-9]{2}\s?[0-9]{2}\s?[A-D](?![A-Z0-9])"
        ),
        confidence="medium",
        description="UK National Insurance number",
    ),
    Rule(
        kind="US_SSN",
        # Separators required: a bare 9-digit run is far too common.
        pattern=re.compile(r"(?<![0-9\-])[0-9]{3}[\-\s][0-9]{2}[\-\s][0-9]{4}(?![0-9\-])"),
        validator=_us_ssn_valid,
        confidence="medium",
        description="US Social Security number",
    ),
]

# Not a regex rule: surnames, initials and reversed forms of a name found by
# another rule (see `find_name_variants`). Listed as a kind so it can be
# selected, auto-accepted or ignored like any other.
VARIANT_KIND = "NAME_VARIANT"
VARIANT_DESCRIPTION = "other forms of a detected name (surname alone, initial)"

RULES_BY_KIND: Dict[str, Rule] = {rule.kind: rule for rule in RULES}
ALL_KINDS = [rule.kind for rule in RULES] + [VARIANT_KIND]
DEFAULT_KINDS = [rule.kind for rule in RULES if rule.enabled_by_default] + [VARIANT_KIND]


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
@dataclass
class Occurrence:
    source: str  # file name
    page_label: str
    start: int
    end: int
    context: str


@dataclass
class Candidate:
    value: str  # normalized text, exactly what will be searched for
    kind: str
    confidence: Confidence
    occurrences: List[Occurrence] = field(default_factory=list)
    parent: Optional[str] = None  # for a name variant: the name it comes from

    @property
    def key(self) -> str:
        return self.value.casefold()

    @property
    def count(self) -> int:
        return len(self.occurrences)


_TRAILING = " \t\u00a0.,;:!?)]}»\"'"
_LEADING = " \t\u00a0([{«\"'"


def _clean(value: str) -> str:
    return normalize(value).strip(_TRAILING).lstrip(_LEADING).strip()


def detect_in_page(
    page: PageText,
    source: str,
    kinds: Optional[Sequence[str]] = None,
) -> List[Candidate]:
    """Candidates found in one page (one entry per distinct value)."""
    active = [
        RULES_BY_KIND[kind]
        for kind in (kinds if kinds is not None else DEFAULT_KINDS)
        if kind in RULES_BY_KIND
    ]
    found: Dict[tuple, Candidate] = {}
    for rule in active:
        for match in rule.pattern.finditer(page.text):
            raw = match.group(rule.group) if rule.group else match.group(0)
            if raw is None:
                continue
            if rule.validator and not rule.validator(raw):
                continue
            value = _clean(raw)
            if len(value) < 3 or value.isspace():
                continue
            if value.isdigit() and len(value) < 5:
                continue
            span_start = match.start(rule.group) if rule.group else match.start()
            span_end = span_start + len(raw)
            key = (rule.kind, value.casefold())
            candidate = found.get(key)
            if candidate is None:
                candidate = Candidate(
                    value=value, kind=rule.kind, confidence=rule.confidence
                )
                found[key] = candidate
            candidate.occurrences.append(
                Occurrence(
                    source=source,
                    page_label=page.label,
                    start=span_start,
                    end=span_end,
                    context=page.context(span_start, span_end),
                )
            )
    return list(found.values())


def merge_candidates(groups: Iterable[Iterable[Candidate]]) -> List[Candidate]:
    """Merge identical candidates found across pages and files."""
    merged: Dict[tuple, Candidate] = {}
    for group in groups:
        for candidate in group:
            key = (candidate.kind, candidate.key)
            existing = merged.get(key)
            if existing is None:
                merged[key] = Candidate(
                    value=candidate.value,
                    kind=candidate.kind,
                    confidence=candidate.confidence,
                    occurrences=list(candidate.occurrences),
                    parent=candidate.parent,
                )
            else:
                existing.occurrences.extend(candidate.occurrences)
    out = list(merged.values())
    out.sort(
        key=lambda c: (
            -CONFIDENCE_ORDER.get(c.confidence, 0),
            c.kind,
            -c.count,
            c.value.casefold(),
        )
    )
    return out


# --------------------------------------------------------------------------
# Name variants
# --------------------------------------------------------------------------
# Rules whose value may be a person's name.
NAME_KINDS = ("NAME_WITH_TITLE", "UPPERCASE_NAME", "LABELED_FIELD")
_NAME_SHAPE = re.compile(rf"{_NAME}(?:{_GAP}{_NAME}){{1,3}}")


def name_variants(value: str) -> List[str]:
    """Other ways the same person is written: "Jane DOE" -> DOE, J. DOE, DOE Jane.

    Redaction looks for the exact string the user accepted, so "Mme DOE" or
    "J. Doe" further down the document would otherwise stay readable.
    Only values shaped like a name (2 to 4 capitalised words) qualify.
    """
    value = normalize(value)
    if not _NAME_SHAPE.fullmatch(value):
        return []
    words = value.split(" ")
    # Surname: the words written in capitals, otherwise the last word.
    surname = [w for w in words if w.isupper() and len(w) >= 2] or [words[-1]]
    given = [w for w in words if w not in surname]
    last = " ".join(surname)
    out = []
    if len(last) >= 3:
        out.append(last)
    if given:
        out.append(f"{given[0][0]}. {last}")
        out.append(f"{last} {' '.join(given)}")
    key = value.casefold()
    return [v for v in dict.fromkeys(out) if v.casefold() != key]


def find_name_variants(
    candidates: Sequence[Candidate],
    pages: Iterable[Tuple[str, PageText]],
) -> List[Candidate]:
    """Variants of the names in `candidates` that actually occur in `pages`.

    An occurrence that is only part of the full name ("DOE" inside "Jane DOE")
    is not counted: redacting the full name already covers it. Variants equal
    to an existing candidate are skipped, that candidate is decided on its own.
    """
    known = {c.value.casefold() for c in candidates}
    pages = list(pages)
    found: Dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.kind not in NAME_KINDS:
            continue
        variants = [
            v for v in name_variants(candidate.value)
            if v.casefold() not in known and v.casefold() not in found
        ]
        if not variants:
            continue
        full = build_pattern(candidate.value)
        for source, page in pages:
            covered = [(s, e) for s, e, _ in iter_matches(page.text, full)]
            for variant in variants:
                pattern = build_pattern(variant, whole_word=True)
                for start, end, _ in iter_matches(page.text, pattern):
                    if any(s <= start and end <= e for s, e in covered):
                        continue
                    entry = found.get(variant.casefold())
                    if entry is None:
                        entry = found[variant.casefold()] = Candidate(
                            value=variant,
                            kind=VARIANT_KIND,
                            confidence="medium",
                            parent=candidate.value,
                        )
                    entry.occurrences.append(
                        Occurrence(
                            source=source,
                            page_label=page.label,
                            start=start,
                            end=end,
                            context=page.context(start, end),
                        )
                    )
    return list(found.values())
