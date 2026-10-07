"""Image backend (PNG, JPEG, TIFF, BMP): scans and photos of documents.

An image has no text layer: it is converted to a one-image PDF, read through
OCR and redacted by the PDF backend, which already knows how to erase pixels
under a redaction area. The result is rasterized again, so the output is an
image again with the redacted pixels genuinely gone.

- PNG and JPEG come back in their own format.
- TIFF and BMP come back as PDF: PyMuPDF cannot write them, and a TIFF may
  hold several pages.

Re-rendering also drops the source metadata (EXIF: camera, date, GPS).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import weakref
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .base import (
    Backend,
    CorruptedDocumentError,
    FileReport,
    OutputError,
    ProgressCallback,
    RedactionOptions,
    register,
)
from .pdf import PdfBackend, pymupdf
from ..textmap import PageText

_SAME_FORMAT = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


@register
class ImageBackend(Backend):
    name = "image"
    extensions = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")

    def __init__(self) -> None:
        self._pdf = PdfBackend()
        # Converted copies live in a private directory, removed when the
        # backend goes away or the interpreter exits. Reusing the same copy
        # between analysis and redaction lets the PDF backend's cache spare a
        # second OCR pass.
        self._tmp = tempfile.mkdtemp(prefix="redactez-img-")
        self._cleanup = weakref.finalize(self, shutil.rmtree, self._tmp, True)
        self._converted: Dict[tuple, Path] = {}

    def output_suffix(self, path: Path) -> str:
        suffix = Path(path).suffix
        return suffix if suffix.lower() in _SAME_FORMAT else ".pdf"

    # ------------------------------------------------------------ conversion
    def _as_pdf(self, path: Path) -> Path:
        path = Path(path)
        if not path.is_file():
            raise CorruptedDocumentError(
                f"file not found: {path}",
                hint="check the path, or quote it if it contains spaces",
            )
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        converted = self._converted.get(key)
        if converted is not None and converted.exists():
            return converted
        try:
            with pymupdf.open(str(path)) as image:
                data = image.convert_to_pdf()
        except Exception as exc:
            raise CorruptedDocumentError(
                f"unreadable image ({exc})",
                hint="re-export it as PNG or PDF and try again",
            ) from exc
        converted = Path(self._tmp) / f"{len(self._converted)}.pdf"
        converted.write_bytes(data)
        self._converted[key] = converted
        return converted

    # ---------------------------------------------------------------- backend
    def read_pages(
        self,
        path: Path,
        options: Optional[RedactionOptions] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[PageText]:
        return self._pdf.read_pages(self._as_pdf(path), options, progress)

    def redact(
        self,
        path: Path,
        output: Path,
        terms: Sequence[str],
        options: RedactionOptions,
        progress: Optional[ProgressCallback] = None,
    ) -> FileReport:
        output = Path(output)
        source_pdf = self._as_pdf(path)
        # The source is an image: the output must be one too, with no text
        # layer left behind. Flattening is therefore not optional here.
        # Rasterized at the image's own resolution, so redacting a 300 dpi
        # scan does not hand back a blurrier 150 dpi one.
        flat = dataclasses.replace(
            options, flatten="always", flatten_dpi=self._native_dpi(source_pdf, options)
        )
        produced = Path(self._tmp) / f"out-{os.getpid()}-{id(output)}.pdf"
        try:
            report = self._pdf.redact(source_pdf, produced, terms, flat, progress)
            report.source, report.output = Path(path), output
            if not report.errors:
                self._write(produced, output, flat)
        finally:
            produced.unlink(missing_ok=True)
        # The PDF backend reports the flattening it was told to do; on an
        # image that is the normal case, not something to warn about.
        report.warnings = [w for w in report.warnings if not w.startswith("document flattened")]
        return report

    @staticmethod
    def _native_dpi(source_pdf: Path, options: RedactionOptions) -> int:
        try:
            with pymupdf.open(str(source_pdf)) as doc:
                page = doc[0]
                images = page.get_images(full=True)
                if not images or page.rect.width <= 0:
                    return options.flatten_dpi
                pixels = images[0][2]  # width in pixels
                return max(72, min(600, round(pixels * 72 / page.rect.width)))
        except Exception:
            return options.flatten_dpi

    def _write(self, produced: Path, output: Path, options: RedactionOptions) -> None:
        fmt = _SAME_FORMAT.get(output.suffix.lower())
        self._pdf._check_output(output)
        tmp = output.with_name(f".{output.name}.tmp")
        try:
            if fmt is None:
                shutil.copyfile(produced, tmp)
            else:
                with pymupdf.open(str(produced)) as doc:
                    colorspace = pymupdf.csGRAY if options.flatten_grayscale else pymupdf.csRGB
                    pixmap = doc[0].get_pixmap(
                        dpi=options.flatten_dpi, alpha=False, colorspace=colorspace
                    )
                    if fmt == "png":
                        data = pixmap.tobytes("png")
                    else:
                        data = pixmap.tobytes("jpeg", jpg_quality=min(95, options.flatten_quality))
                tmp.write_bytes(data)
            os.replace(tmp, output)
        except OSError as exc:
            raise OutputError(
                f"cannot write to {output} ({exc})",
                hint="disk full, file open elsewhere, or insufficient permissions?",
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)
