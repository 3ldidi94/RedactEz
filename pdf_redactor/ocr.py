"""OCR of scanned pages, through Tesseract.

Two facts drive this module:

1. A scanned PDF has no text layer: without OCR there is simply *nothing* to
   redact, and the tool would silently produce an unchanged copy. Staying quiet
   about that would be the worst possible outcome.
2. On a scanned page, removing text is not enough: the data lives in the
   pixels. Redacting such pages therefore forces pixel erasure
   (`PDF_REDACT_IMAGE_PIXELS`).

The OCR engine used is the one built into PyMuPDF (`get_textpage_ocr`), which
calls the Tesseract binary and returns per-character positions — exactly what
the rest of the tool needs. No extra Python dependency.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from typing import List, Optional

_TESSDATA_CANDIDATES = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tesseract-ocr/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
    "/opt/homebrew/share/tessdata",
)

_PREFERRED = ("eng", "fra")

INSTALL_HINT = (
    "install Tesseract: "
    "Debian/Ubuntu 'sudo apt install tesseract-ocr tesseract-ocr-eng', "
    "macOS 'brew install tesseract tesseract-lang', "
    "Windows 'winget install UB-Mannheim.TesseractOCR'"
)


class OcrUnavailableError(RuntimeError):
    """Raised when OCR is requested but unavailable or misconfigured."""


@lru_cache(maxsize=1)
def binary_path() -> Optional[str]:
    found = shutil.which("tesseract")
    if found:
        return found
    return next((path for path in _windows_binaries() if os.path.isfile(path)), None)


def _windows_binaries() -> List[str]:
    """Default install locations: the Windows installer does not touch PATH."""
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs"))
    return [
        os.path.join(root, "Tesseract-OCR", "tesseract.exe") for root in roots if root
    ]


@lru_cache(maxsize=1)
def tessdata_dir() -> Optional[str]:
    candidates: List[str] = []
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        candidates += [env, os.path.join(env, "tessdata")]
    binary = binary_path()
    if binary:
        # Data usually sits next to the binary (Windows) or in ../share.
        home = os.path.dirname(os.path.realpath(binary))
        candidates += [
            os.path.join(home, "tessdata"),
            os.path.join(home, os.pardir, "share", "tessdata"),
        ]
    candidates += _TESSDATA_CANDIDATES
    return next(
        (
            os.path.normpath(path)
            for path in candidates
            if os.path.isdir(path) and _has_traineddata(path)
        ),
        None,
    )


def _has_traineddata(path: str) -> bool:
    try:
        return any(name.endswith(".traineddata") for name in os.listdir(path))
    except OSError:
        return False


@lru_cache(maxsize=1)
def available_languages() -> List[str]:
    """Installed languages, read from tessdata first, then from Tesseract."""
    directory = tessdata_dir()
    if directory:
        try:
            return sorted(
                name[: -len(".traineddata")]
                for name in os.listdir(directory)
                if name.endswith(".traineddata")
            )
        except OSError:
            pass
    binary = binary_path()
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(
        line.strip()
        for line in result.stdout.splitlines()[1:]
        if re.fullmatch(r"[A-Za-z_]{3,}", line.strip())
    )


def is_available() -> bool:
    return bool(binary_path()) and bool(available_languages())


def describe() -> str:
    if not binary_path():
        return "Tesseract not found"
    languages = available_languages()
    if not languages:
        return "Tesseract present but no language data found"
    more = "…" if len(languages) > 6 else ""
    return f"Tesseract ({', '.join(languages[:6])}{more})"


def resolve_language(requested: str = "auto") -> str:
    """Pick an available language, with a clear message when it fails."""
    if not binary_path():
        raise OcrUnavailableError(
            "OCR requested but the 'tesseract' binary is not in PATH. "
            f"{INSTALL_HINT}."
        )
    languages = available_languages()
    if not languages:
        raise OcrUnavailableError(
            "Tesseract is installed but no language data (.traineddata) was "
            "found. Point TESSDATA_PREFIX at the 'tessdata' directory, or "
            f"{INSTALL_HINT}."
        )
    if requested in ("auto", "", None):
        for language in _PREFERRED:
            if language in languages:
                return language
        return languages[0]
    # Tesseract accepts "fra+eng": validate every component.
    parts = [part for part in requested.split("+") if part]
    missing = [part for part in parts if part not in languages]
    if missing:
        raise OcrUnavailableError(
            f"OCR language not available: {', '.join(missing)}. "
            f"Installed languages: {', '.join(languages)}. "
            "To add French: 'sudo apt install tesseract-ocr-fra'."
        )
    return requested


def prepare_environment() -> None:
    """Set TESSDATA_PREFIX when needed (PyMuPDF reads it at call time)."""
    if os.environ.get("TESSDATA_PREFIX"):
        return
    directory = tessdata_dir()
    if directory:
        os.environ["TESSDATA_PREFIX"] = directory


def page_needs_ocr(text: str, has_images: bool, threshold: int = 24) -> bool:
    """A page carrying images but no usable text layer is a scan."""
    return has_images and len(text.strip()) < threshold


def ocr_textpage(page, language: str, dpi: int = 200):
    """TextPage produced by OCR of the whole page.

    Raises `OcrUnavailableError` so the caller can degrade gracefully and warn,
    rather than aborting a whole batch because of one page.
    """
    prepare_environment()
    try:
        return page.get_textpage_ocr(language=language, dpi=dpi, full=True)
    except Exception as exc:  # MuPDF RuntimeError, missing binary, memory...
        raise OcrUnavailableError(
            f"OCR failed on page {page.number + 1}: {exc}. "
            f"Check the installation ({INSTALL_HINT})."
        ) from exc
