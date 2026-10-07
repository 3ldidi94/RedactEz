"""Interface shared by every document format.

Adding a format means writing a `Backend` subclass and registering it with
`register()`. Everything else (detection, prompting, policy, reporting, CLI,
watermark, progress) is format agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Type

from ..textmap import PageText
from ..watermark import DEFAULT_OPACITY, DEFAULT_TEXT

# Progress callback: (current step, total, label)
ProgressCallback = Callable[[int, int, str], None]


# --------------------------------------------------------------------------
# Errors — each one carries a message that can be shown to the user as is
# --------------------------------------------------------------------------
class DocumentError(RuntimeError):
    """Document processing error, with an explicit message and a hint."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class UnsupportedFormatError(DocumentError):
    pass


class EncryptedDocumentError(DocumentError):
    pass


class CorruptedDocumentError(DocumentError):
    pass


class OutputError(DocumentError):
    pass


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------
@dataclass
class RedactionOptions:
    # rendering
    mode: str = "stars"  # stars | box | label
    replacement_char: str = "*"
    label: str = "[REDACTED]"
    max_stars: int = 40
    # matching
    fold_accents: bool = True
    ignore_case: bool = True
    whole_word: bool = False
    # cleaning
    strip_metadata: bool = True
    redact_images: bool = False  # erase pixels under every redacted area
    # flattening (rasterization): makes redaction irreversible
    flatten: str = "auto"  # auto | always | never
    flatten_dpi: int = 150
    flatten_quality: int = 70  # 100 = lossless (much larger file)
    # OCR
    ocr: str = "auto"  # auto | force | never
    ocr_language: str = "auto"
    ocr_dpi: int = 200
    # watermark
    watermark: Optional[str] = DEFAULT_TEXT  # None = no watermark
    watermark_opacity: float = DEFAULT_OPACITY
    watermark_footer: bool = True
    # verification
    verify: bool = True
    verify_image: bool = False  # re-read the output with OCR (slow but conclusive)

    def validate(self) -> None:
        if self.mode not in ("stars", "box", "label"):
            raise ValueError(f"unknown rendering mode: {self.mode}")
        if self.ocr not in ("auto", "force", "never"):
            raise ValueError(f"unknown OCR mode: {self.ocr}")
        if self.flatten not in ("auto", "always", "never"):
            raise ValueError(f"unknown flatten mode: {self.flatten}")
        if not (72 <= self.ocr_dpi <= 600):
            raise ValueError("OCR resolution must be between 72 and 600 dpi")
        if not (72 <= self.flatten_dpi <= 600):
            raise ValueError("flatten resolution must be between 72 and 600 dpi")
        if not (30 <= self.flatten_quality <= 100):
            raise ValueError("flatten quality must be between 30 and 100")
        if not (0.0 <= self.watermark_opacity <= 1.0):
            raise ValueError("watermark opacity must be between 0 and 1")
        if len(self.replacement_char) != 1 or not self.replacement_char.isprintable():
            raise ValueError(
                "the replacement character must be a single printable character"
            )


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
@dataclass
class TermReport:
    term: str
    occurrences: int = 0


@dataclass
class FileReport:
    source: Path
    output: Optional[Path] = None
    pages: int = 0
    terms: List[TermReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    hints: List[str] = field(default_factory=list)
    verified: Optional[bool] = None
    residual: List[str] = field(default_factory=list)
    verified_image: Optional[bool] = None
    residual_image: List[str] = field(default_factory=list)
    ocr_pages: List[int] = field(default_factory=list)
    raster_pages: List[int] = field(default_factory=list)  # areas over an image
    flattened: bool = False
    skipped: bool = False
    duration: float = 0.0

    @property
    def total_occurrences(self) -> int:
        return sum(term.occurrences for term in self.terms)

    @property
    def ok(self) -> bool:
        return (
            not self.errors
            and self.verified is not False
            and self.verified_image is not False
        )

    def add_error(self, message: str, hint: str = "") -> None:
        self.errors.append(message)
        if hint:
            self.hints.append(hint)

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "output": str(self.output) if self.output else None,
            "pages": self.pages,
            "ocr_pages": self.ocr_pages,
            "image_redacted_pages": self.raster_pages,
            "flattened": self.flattened,
            "total_occurrences": self.total_occurrences,
            "duration_s": round(self.duration, 3),
            "terms": [
                {"term": term.term, "occurrences": term.occurrences}
                for term in self.terms
                if term.occurrences
            ],
            "warnings": self.warnings,
            "errors": self.errors,
            "verified": self.verified,
            "residual": self.residual,
            "verified_image": self.verified_image,
            "residual_image": self.residual_image,
        }


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------
class Backend(ABC):
    """Minimal contract for a document backend."""

    name: str = "abstract"
    extensions: Sequence[str] = ()

    @classmethod
    def supports(cls, path: Path) -> bool:
        return path.suffix.lower() in cls.extensions

    @abstractmethod
    def read_pages(
        self,
        path: Path,
        options: Optional[RedactionOptions] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[PageText]:
        """Text and geometry of every page (used for detection)."""

    @abstractmethod
    def redact(
        self,
        path: Path,
        output: Path,
        terms: Sequence[str],
        options: RedactionOptions,
        progress: Optional[ProgressCallback] = None,
    ) -> FileReport:
        """Produce a redacted copy. Must never modify `path`."""


_REGISTRY: Dict[str, Type[Backend]] = {}
# Instances are reused across calls: a backend caches the last document it
# parsed, which avoids redoing extraction — and above all OCR, which costs
# seconds per page — between the analysis phase and the redaction phase.
# Not designed for multi-threaded use.
_INSTANCES: Dict[str, Backend] = {}


def register(backend: Type[Backend]) -> Type[Backend]:
    for extension in backend.extensions:
        _REGISTRY[extension.lower()] = backend
    return backend


def get_backend(path: Path, cached: bool = True) -> Backend:
    extension = Path(path).suffix.lower()
    if cached and extension in _INSTANCES:
        return _INSTANCES[extension]
    backend = _REGISTRY.get(extension)
    if backend is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise UnsupportedFormatError(
            f"unsupported format: '{Path(path).suffix or 'no extension'}'",
            hint=f"known formats: {known}",
        )
    instance = backend()
    if cached:
        _INSTANCES[extension] = instance
    return instance


def supported_extensions() -> List[str]:
    return sorted(_REGISTRY)
