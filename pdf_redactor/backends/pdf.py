"""PDF backend built on PyMuPDF (MuPDF).

Why PyMuPDF: it is the only widely used Python library implementing *true*
redaction (`apply_redactions`), meaning removal of the glyphs from the content
stream. Drawing a black rectangle over text (pypdf, reportlab, pdf-lib, ...)
removes nothing: the text is still extractable by copy and paste.

Beyond the body text, this backend handles:
- scanned pages, through OCR (see `pdf_redactor.ocr`);
- form field (widget) values;
- annotation contents;
- hyperlinks (often `mailto:` with the address in clear text);
- bookmarks, and both classic and XMP metadata;
- rasterization of the output, so redaction cannot be undone.
"""

from __future__ import annotations

import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # PyMuPDF >= 1.24 exposes the module as "pymupdf"
    import pymupdf  # type: ignore
except ImportError:  # pragma: no cover - older versions
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "PyMuPDF is required for PDF support. Install it with "
            "'pip install pymupdf'."
        ) from exc

from .. import ocr as ocr_module
from .. import watermark as watermark_module
from ..matching import build_pattern, find_residual, iter_matches, normalize
from ..textmap import (
    PageText,
    PageTextBuilder,
    expand_for_pixels,
    merge_line_rects,
    shrink_vertically,
)
from .base import (
    Backend,
    CorruptedDocumentError,
    DocumentError,
    EncryptedDocumentError,
    FileReport,
    OutputError,
    ProgressCallback,
    RedactionOptions,
    TermReport,
    register,
)

_MIN_FONTSIZE = 3.5
# Annotation fields holding free text (popup content, author, subject).
_ANNOT_KEYS = ("content", "title", "subject")
_MAX_FONTSIZE = 18.0


def _const(name: str, default: int) -> int:
    return int(getattr(pymupdf, name, default))


@register
class PdfBackend(Backend):
    name = "pdf"
    extensions = (".pdf",)

    # Memory budget of the extraction cache, in retained characters (~50 MB).
    CACHE_CHAR_BUDGET = 4_000_000

    def __init__(self) -> None:
        # Analysis and redaction read the same document. Without a cache both
        # phases do the work twice — harmless on native text (0.1 s), very
        # costly with OCR (seconds per page). LRU cache bounded by character
        # count so behaviour stays predictable on large batches.
        self._cache: "OrderedDict[tuple, List[PageText]]" = OrderedDict()
        self._cache_chars = 0

    def _cache_get(self, key: tuple) -> Optional[List[PageText]]:
        pages = self._cache.get(key)
        if pages is not None:
            self._cache.move_to_end(key)
        return pages

    def _cache_put(self, key: tuple, pages: List[PageText]) -> None:
        size = sum(len(page.text) for page in pages)
        if size > self.CACHE_CHAR_BUDGET:
            return  # document too large: do not cache it
        self._cache[key] = pages
        self._cache_chars += size
        while self._cache_chars > self.CACHE_CHAR_BUDGET and len(self._cache) > 1:
            _, evicted = self._cache.popitem(last=False)
            self._cache_chars -= sum(len(page.text) for page in evicted)

    # ------------------------------------------------------------- opening
    def _open(self, path: Path):
        path = Path(path)
        if not path.exists():
            raise DocumentError(
                f"file not found: {path}",
                hint="check the path, or quote it if it contains spaces",
            )
        if path.is_dir():
            raise DocumentError(f"{path} is a directory, not a PDF file")
        try:
            doc = pymupdf.open(str(path))
        except PermissionError:
            raise DocumentError(
                f"read denied: {path}", hint="check the file permissions"
            ) from None
        except Exception as exc:
            raise CorruptedDocumentError(
                f"unreadable or damaged PDF ({exc})",
                hint="try repairing it: 'qpdf --replace-input --qdf file.pdf'",
            ) from exc
        if doc.needs_pass:
            doc.close()
            raise EncryptedDocumentError(
                "password protected PDF",
                hint="decrypt it first: 'qpdf --password=PASSWORD --decrypt "
                "source.pdf clear.pdf'",
            )
        if doc.page_count == 0:
            doc.close()
            raise CorruptedDocumentError("document has no pages")
        return doc

    @staticmethod
    def _signature(path: Path, options: Optional[RedactionOptions]) -> tuple:
        stat = Path(path).stat()
        ocr_settings = (
            (options.ocr, options.ocr_language, options.ocr_dpi) if options else ()
        )
        return (str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size, ocr_settings)

    # ------------------------------------------------------------- reading
    def read_pages(
        self,
        path: Path,
        options: Optional[RedactionOptions] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[PageText]:
        key = self._signature(path, options)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        options = options or RedactionOptions()
        language = self._resolve_ocr_language(options)
        doc = self._open(path)
        try:
            total = doc.page_count
            pages = []
            for index in range(total):
                if progress:
                    progress(index, total, f"page {index + 1}/{total}")
                page_text, _ = self._page_text(doc, index, options, language)
                pages.append(page_text)
            if progress:
                progress(total, total, "")
        finally:
            doc.close()

        self._cache_put(key, pages)
        return pages

    def _resolve_ocr_language(self, options: RedactionOptions) -> Optional[str]:
        """OCR language, or None when OCR is disabled or unavailable.

        In "auto" mode a missing Tesseract is not an error: processing carries
        on without OCR and warns page by page. In "force" mode it is an
        explicit error, since the user asked for it.
        """
        if options.ocr == "never":
            return None
        try:
            return ocr_module.resolve_language(options.ocr_language)
        except ocr_module.OcrUnavailableError:
            if options.ocr == "force":
                raise
            return None

    def _page_text(
        self,
        doc,
        index: int,
        options: RedactionOptions,
        ocr_language: Optional[str],
    ) -> Tuple[PageText, bool]:
        """PageText for one page, plus an "came from OCR" flag."""
        page = doc[index]
        native = self._build_page_text(page, index)

        needs_ocr = options.ocr == "force" or (
            options.ocr == "auto"
            and ocr_module.page_needs_ocr(native.text, native.has_images)
        )
        if not needs_ocr or not ocr_language:
            return native, False

        textpage = ocr_module.ocr_textpage(page, ocr_language, options.ocr_dpi)
        ocred = self._build_page_text(page, index, textpage=textpage, from_ocr=True)
        if len(ocred.text.strip()) <= len(native.text.strip()):
            return native, False  # OCR added nothing
        return ocred, True

    @staticmethod
    def _build_page_text(
        page, index: int, textpage=None, from_ocr: bool = False
    ) -> PageText:
        builder = PageTextBuilder(index, label=f"page {index + 1}")
        try:
            raw = (
                textpage.extractRAWDICT()
                if textpage is not None
                else page.get_text("rawdict")
            )
        except Exception as exc:
            raise CorruptedDocumentError(
                f"page {index + 1}: text extraction failed ({exc})"
            ) from exc

        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # 1 = image
                builder.has_images = True
                continue
            for line in block.get("lines", []):
                builder.new_line()
                for span in line.get("spans", []):
                    for char_info in span.get("chars", []):
                        char = char_info.get("c", "")
                        if char:
                            builder.add_char(char, tuple(char_info["bbox"]))
                builder.add_separator("\n")

        if not from_ocr:
            # Form field values do not appear in rawdict: append them to the
            # analysed text (without geometry).
            for widget in PdfBackend._widgets(page):
                value = getattr(widget, "field_value", None)
                if isinstance(value, str) and value.strip():
                    builder.new_line()
                    builder.add_separator(
                        f"[field {widget.field_name or '?'}] {value}\n"
                    )
        return builder.build(from_ocr=from_ocr)

    @staticmethod
    def _widgets(page) -> List:
        try:
            return list(page.widgets() or [])
        except Exception:
            return []

    # ------------------------------------------------------------ redaction
    def redact(
        self,
        path: Path,
        output: Path,
        terms: Sequence[str],
        options: RedactionOptions,
        progress: Optional[ProgressCallback] = None,
    ) -> FileReport:
        started = time.monotonic()
        options.validate()
        report = FileReport(source=Path(path), output=Path(output))

        verified_before_flatten = False
        cleaned = [t for t in dict.fromkeys(normalize(t) for t in terms) if t]
        if not cleaned:
            report.add_error("no term to redact for this document")
            return report

        patterns = {
            term: build_pattern(
                term,
                fold_accents=options.fold_accents,
                ignore_case=options.ignore_case,
                whole_word=options.whole_word,
            )
            for term in cleaned
        }
        counters: Dict[str, int] = dict.fromkeys(cleaned, 0)

        self._check_output(Path(output))
        language = self._resolve_ocr_language(options)
        cached = self._cached_pages(path, options)

        doc = self._open(path)
        try:
            total = doc.page_count
            report.pages = total
            for index in range(total):
                if progress:
                    progress(index, total, f"page {index + 1}/{total}")
                self._redact_page(
                    doc, index, patterns, counters, options, report, language, cached
                )
            self._scrub_document(doc, patterns, counters, options, report)
            self._stamp(doc, options, report)

            if self._should_flatten(options, report):
                # Verification must run on the version *before* rasterization:
                # once flattened there is no text left to extract, so any check
                # would be trivially satisfied — that is, misleading.
                if options.verify:
                    self._verify(
                        doc.tobytes(garbage=3, deflate=True), cleaned, report, options
                    )
                    verified_before_flatten = True
                if progress:
                    progress(total, total, "flattening")
                flat = self._flatten(doc, options, report)
                try:
                    self._save(flat, Path(output))
                finally:
                    flat.close()
                report.flattened = True
            else:
                self._save(doc, Path(output))
            if progress:
                progress(total, total, "")
        finally:
            doc.close()

        report.terms = [TermReport(term=t, occurrences=counters[t]) for t in cleaned]
        if report.total_occurrences == 0:
            report.warnings.append(
                "no occurrence found in this document "
                "(term spelling? scanned document without OCR?)"
            )
        if options.verify and not verified_before_flatten:
            self._verify(Path(output), cleaned, report, options)
        if options.verify_image:
            self._verify_visual(Path(output), cleaned, report, options)
        report.duration = time.monotonic() - started
        return report

    def _cached_pages(
        self, path: Path, options: RedactionOptions
    ) -> Optional[List[PageText]]:
        try:
            return self._cache_get(self._signature(path, options))
        except OSError:
            return None

    @staticmethod
    def _check_output(output: Path) -> None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputError(
                f"cannot create output directory {output.parent} ({exc})",
                hint="check write permissions",
            ) from exc

    @staticmethod
    def _save(doc, output: Path) -> None:
        """Write next to `output`, then rename into place.

        A crash or a full disk never leaves a truncated PDF under the final
        name, and the temporary file is always removed.
        """
        tmp = output.with_name(f".{output.name}.tmp")
        try:
            doc.save(str(tmp), garbage=4, deflate=True, clean=True)
            os.replace(tmp, output)
        except OSError as exc:
            raise OutputError(
                f"cannot write to {output} ({exc})",
                hint="disk full, file open elsewhere, or insufficient permissions?",
            ) from exc
        except Exception as exc:
            raise OutputError(f"failed to save {output} ({exc})") from exc
        finally:
            tmp.unlink(missing_ok=True)

    # ---------------------------------------------------------------- pages
    def _redact_page(
        self,
        doc,
        index: int,
        patterns: Dict[str, "re.Pattern"],
        counters: Dict[str, int],
        options: RedactionOptions,
        report: FileReport,
        ocr_language: Optional[str],
        cached: Optional[List[PageText]],
    ) -> None:
        page = doc[index]
        if cached is not None and index < len(cached):
            page_text = cached[index]
            used_ocr = page_text.from_ocr
        else:
            try:
                page_text, used_ocr = self._page_text(doc, index, options, ocr_language)
            except ocr_module.OcrUnavailableError as exc:
                report.warnings.append(str(exc))
                page_text, used_ocr = self._build_page_text(page, index), False

        if used_ocr:
            report.ocr_pages.append(index + 1)
        elif ocr_module.page_needs_ocr(page_text.text, page_text.has_images):
            report.warnings.append(
                f"{page_text.label}: no text layer (scanned page). "
                + (
                    "Re-run with --ocr force if automatic detection was not enough."
                    if ocr_language
                    else f"OCR unavailable — {ocr_module.INSTALL_HINT}."
                )
            )

        items: List[Tuple[Tuple[float, float, float, float], int]] = []
        for term, pattern in patterns.items():
            for start, end, matched in iter_matches(page_text.text, pattern):
                rects = page_text.rects_for(start, end)
                if not rects:
                    continue
                counters[term] += 1
                length = max(1, len(matched.strip()) // len(rects))
                items.extend((rect, length) for rect in rects)

        if items:
            merged = merge_line_rects(items)
            rects = [rect for rect, _ in merged]

            # The data may live in the pixels and not only in the text: scanned
            # page, or a scan already OCR'd by another tool (image plus an
            # invisible text layer). Removing only the text would leave the
            # data readable under a plain white rectangle, which anyone could
            # delete in a PDF editor.
            over_image = self._images_under(page, rects)
            if over_image:
                report.raster_pages.append(index + 1)
            erase_pixels = options.redact_images or used_ocr or over_image

            # Pass 1 — text. Shrunk area: the engine removes every glyph that
            # *touches* the area, and consecutive line boxes touch each other;
            # without this inset the neighbouring lines would be cut off too.
            for rect in rects:
                self._add_annot(page, shrink_vertically(rect), options)
            self._apply(
                page,
                report,
                page_text.label,
                images=_const("PDF_REDACT_IMAGE_NONE", 0),
                text=_const("PDF_REDACT_TEXT_REMOVE", 0),
            )

            # Pass 2 — pixels. Erasure covers exactly the rectangle, so a
            # margin is needed: OCR boxes hug the glyphs, and on an already
            # OCR'd scan the text layer is not perfectly aligned with the
            # image. Text is no longer a concern, pass 1 dealt with it.
            if erase_pixels:
                for rect in rects:
                    self._add_annot(page, expand_for_pixels(rect), options)
                self._apply(
                    page,
                    report,
                    page_text.label,
                    images=_const("PDF_REDACT_IMAGE_PIXELS", 2),
                    text=_const("PDF_REDACT_TEXT_NONE", 1),
                )

            # The truncation pattern is drawn AFTER removal, otherwise it would
            # be erased along with the rest.
            for rect, length in merged:
                self._draw_replacement(page, rect, length, options, report)

        self._scrub_widgets(page, patterns, counters, options, report)
        self._scrub_annotations(page, patterns, counters, options, report)
        self._scrub_links(page, patterns, report)

    def _add_annot(self, page, geometry, options: RedactionOptions) -> None:
        """Add a redaction area (geometry already adjusted by the caller)."""
        rect = pymupdf.Rect(geometry) & page.rect
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            return
        fill = (0, 0, 0) if options.mode == "box" else (1, 1, 1)
        try:
            page.add_redact_annot(rect, fill=fill, cross_out=False)
        except TypeError:
            page.add_redact_annot(rect, fill=fill)

    def _draw_replacement(self, page, rect, length, options, report) -> None:
        if options.mode == "box":
            return  # the black rectangle of the annotation is enough
        area = pymupdf.Rect(rect) & page.rect
        if area.is_empty or area.width <= 2:
            return
        text = (
            options.label
            if options.mode == "label"
            else options.replacement_char * max(3, min(int(length), options.max_stars))
        )
        size = min(_MAX_FONTSIZE, max(_MIN_FONTSIZE, area.height * 0.68))
        width = self._text_length(text, size)
        if width > area.width and options.mode == "stars":
            unit = width / max(1, len(text))
            keep = max(3, int(area.width / unit))
            text = options.replacement_char * keep
            width = unit * keep
        if width > area.width:  # label too long: shrink the font
            size = max(_MIN_FONTSIZE, size * area.width / width)
            width = area.width
        try:
            page.insert_text(
                (area.x0 + max(0.0, (area.width - width) / 2),
                 area.y1 - area.height * 0.22),
                text,
                fontname="helv",
                fontsize=size,
                color=(0, 0, 0),
            )
        except Exception as exc:
            report.warnings.append(f"truncation pattern not drawn ({exc})")

    @staticmethod
    def _apply(page, report: FileReport, label: str, images: int, text: int) -> None:
        kwargs = {
            "images": images,
            "text": text,
            "graphics": _const("PDF_REDACT_LINE_ART_NONE", 0),
        }
        try:
            page.apply_redactions(**kwargs)
        except TypeError:  # older PyMuPDF: fewer options
            try:
                page.apply_redactions(images=images)
            except Exception as exc:
                raise DocumentError(f"{label}: redaction failed ({exc})") from exc
        except Exception as exc:
            raise DocumentError(
                f"{label}: redaction failed ({exc})",
                hint="the file may be damaged; try 'qpdf --qdf'",
            ) from exc

    @staticmethod
    def _images_under(page, rects) -> bool:
        """Does any redaction area overlap a raster image?"""
        if not rects:
            return False
        targets = [pymupdf.Rect(rect) for rect in rects]
        try:
            images = page.get_images(full=True)
        except Exception:
            return False
        for info in images:
            try:
                placements = page.get_image_rects(info[0])
            except Exception:
                continue
            for placement in placements:
                if any(not (placement & target).is_empty for target in targets):
                    return True
        return False

    @staticmethod
    def _text_length(text: str, size: float) -> float:
        try:
            return pymupdf.get_text_length(text, fontname="helv", fontsize=size)
        except Exception:
            return 0.5 * size * len(text)

    # ------------------------------------------------- outside the body text
    def _scrub_widgets(self, page, patterns, counters, options, report) -> None:
        for widget in self._widgets(page):
            value = getattr(widget, "field_value", None)
            if not isinstance(value, str) or not value.strip():
                continue
            new_value, hits = self._mask_string(value, patterns, counters, options)
            if not hits:
                continue
            try:
                widget.field_value = new_value
                widget.update()
            except Exception as exc:
                report.warnings.append(
                    f"form field '{widget.field_name}' could not be modified "
                    f"({exc}) — data possibly retained"
                )

    def _scrub_annotations(self, page, patterns, counters, options, report) -> None:
        try:
            annotations = list(page.annots() or [])
        except Exception:
            return
        for annotation in annotations:
            info = dict(annotation.info or {})
            changed = False
            for key in _ANNOT_KEYS:
                value = info.get(key)
                if not value:
                    continue
                new_value, hits = self._mask_string(value, patterns, counters, options)
                if hits:
                    info[key] = new_value
                    changed = True
            if changed:
                try:
                    annotation.set_info(info)
                    annotation.update()
                except Exception as exc:
                    report.warnings.append(f"annotation not modifiable ({exc})")

    def _scrub_links(self, page, patterns, report) -> None:
        try:
            links = page.get_links()
        except Exception:
            return
        for link in links:
            uri = link.get("uri") or link.get("file") or ""
            if uri and any(pattern.search(uri) for pattern in patterns.values()):
                try:
                    page.delete_link(link)
                    report.warnings.append(
                        f"page {page.number + 1}: link containing personal data "
                        "was removed"
                    )
                except Exception as exc:
                    report.warnings.append(f"link not removable ({exc})")

    def _mask_string(self, value: str, patterns, counters, options) -> Tuple[str, int]:
        hits = 0
        out = value
        for term, pattern in patterns.items():
            def _substitute(match, _term=term):
                nonlocal hits
                hits += 1
                counters[_term] = counters.get(_term, 0) + 1
                if options.mode == "label":
                    return options.label
                return options.replacement_char * max(3, len(match.group(0).strip()))

            out = pattern.sub(_substitute, out)
        return out, hits

    def _scrub_document(self, doc, patterns, counters, options, report) -> None:
        if options.strip_metadata:
            try:
                doc.set_metadata({})
            except Exception as exc:
                report.warnings.append(f"metadata not cleared ({exc})")
            try:
                doc.del_xml_metadata()
            except Exception:
                pass
        else:
            metadata = dict(doc.metadata or {})
            changed = False
            for key, value in list(metadata.items()):
                if isinstance(value, str) and value:
                    new_value, hits = self._mask_string(
                        value, patterns, counters, options
                    )
                    if hits:
                        metadata[key] = new_value
                        changed = True
            if changed:
                try:
                    doc.set_metadata(metadata)
                except Exception as exc:
                    report.warnings.append(f"metadata not updated ({exc})")

        try:
            embedded = doc.embfile_count()
        except Exception:
            embedded = 0
        if embedded:
            report.warnings.append(
                f"{embedded} embedded file(s) were not analysed — check manually"
            )

        try:
            toc = doc.get_toc(simple=True)
        except Exception:
            toc = []
        if toc:
            new_toc, changed = [], False
            for entry in toc:
                masked, hits = self._mask_string(entry[1], patterns, counters, options)
                changed = changed or bool(hits)
                new_toc.append([entry[0], masked, entry[2]])
            if changed:
                try:
                    doc.set_toc(new_toc)
                except Exception as exc:
                    report.warnings.append(f"bookmarks not updated ({exc})")

    def _stamp(self, doc, options: RedactionOptions, report: FileReport) -> None:
        if not options.watermark:
            return
        footer = (
            watermark_module.footer_text(options.watermark)
            if options.watermark_footer
            else None
        )
        for page in doc:
            message = watermark_module.apply(
                pymupdf,
                page,
                options.watermark,
                opacity=options.watermark_opacity,
                footer=footer,
            )
            if message and message not in report.warnings:
                report.warnings.append(message)

    # ------------------------------------------------- flattening (raster)
    @staticmethod
    def _should_flatten(options: RedactionOptions, report: FileReport) -> bool:
        if options.flatten == "never":
            return False
        if options.flatten == "always":
            return True
        # "auto": only when a redacted area sits on top of an image.
        return bool(report.ocr_pages or report.raster_pages)

    def _flatten(self, doc, options: RedactionOptions, report: FileReport):
        """Rasterize every page so redaction becomes irreversible.

        Without this, a redaction applied over an image is in theory
        recoverable if an object can be removed from the content stream. After
        rasterization there is no layer left to remove: the masked pixels
        simply no longer exist.
        """
        flat = pymupdf.open()
        try:
            for page in doc:
                rect = page.rect
                pixmap = page.get_pixmap(dpi=options.flatten_dpi, alpha=False)
                target = flat.new_page(width=rect.width, height=rect.height)
                if options.flatten_quality >= 100:
                    target.insert_image(target.rect, pixmap=pixmap)
                else:
                    target.insert_image(
                        target.rect,
                        stream=pixmap.tobytes(
                            "jpeg", jpg_quality=options.flatten_quality
                        ),
                    )
                pixmap = None  # release memory page by page
        except Exception as exc:
            flat.close()
            raise OutputError(
                f"flattening failed ({exc})",
                hint="lower --flatten-dpi, or use --flatten never after checking "
                "that the document contains no image",
            ) from exc
        try:
            flat.set_metadata({})
        except Exception:
            pass
        report.warnings.append(
            f"document flattened at {options.flatten_dpi} dpi: redaction is "
            "irreversible, but text is no longer selectable and the file is "
            "larger (--flatten never keeps the text layer)"
        )
        return flat

    def _verify_visual(
        self, output: Path, terms: Sequence[str], report: FileReport, options
    ) -> None:
        """Re-read the output *with OCR*: the only conclusive check on a scan.

        The textual check cannot see what remains in the pixels (erasure
        residue, text layer misaligned with the image). Costly — roughly one
        second per page — hence opt-in.
        """
        try:
            language = ocr_module.resolve_language(options.ocr_language)
        except ocr_module.OcrUnavailableError as exc:
            report.warnings.append(f"visual check impossible: {exc}")
            return
        try:
            doc = pymupdf.open(str(output))
        except Exception as exc:
            report.warnings.append(f"visual check impossible ({exc})")
            return
        try:
            chunks = []
            for page in doc:
                try:
                    textpage = ocr_module.ocr_textpage(page, language, options.ocr_dpi)
                    chunks.append(page.get_text("text", textpage=textpage))
                except ocr_module.OcrUnavailableError as exc:
                    report.warnings.append(str(exc))
                    return
            blob = "\n".join(chunks)
        finally:
            doc.close()
        residual = find_residual(blob, terms, options.whole_word)
        report.residual_image = residual
        report.verified_image = not residual
        if residual:
            report.add_error(
                "terms still READABLE in the image after redaction: "
                + ", ".join(residual[:5])
                + ("…" if len(residual) > 5 else ""),
                hint="increase the margin (--ocr-dpi 300) or inspect the document "
                "visually: it must not be distributed as is",
            )

    # --------------------------------------------------------- verification
    def _verify(
        self, source, terms: Sequence[str], report: FileReport, options
    ) -> None:
        """Re-read the produced document. `source` is a path, or PDF bytes."""
        try:
            doc = (
                pymupdf.open(stream=source, filetype="pdf")
                if isinstance(source, (bytes, bytearray))
                else pymupdf.open(str(source))
            )
        except Exception as exc:
            report.warnings.append(
                f"final check impossible ({exc}) — read the produced document"
            )
            return
        try:
            blob = self._extractable_text(doc)
        finally:
            doc.close()
        residual = find_residual(blob, terms, options.whole_word)
        report.residual = residual
        report.verified = not residual
        if residual:
            report.add_error(
                "terms still extractable after redaction: "
                + ", ".join(residual[:5])
                + ("…" if len(residual) > 5 else ""),
                hint="flag this document: it must not be distributed as is",
            )

    def _extractable_text(self, doc) -> str:
        """Everything a reader can pull text from — every place redaction scrubs."""
        chunks: List[str] = []
        for page in doc:
            chunks.append(page.get_text("text"))
            for widget in self._widgets(page):
                value = getattr(widget, "field_value", None)
                if isinstance(value, str):
                    chunks.append(value)
            try:
                for annotation in page.annots() or []:
                    info = annotation.info or {}
                    chunks.extend(str(info.get(key) or "") for key in _ANNOT_KEYS)
                chunks.extend(
                    link.get("uri") or link.get("file") or ""
                    for link in page.get_links()
                )
            except Exception:
                pass
        try:
            chunks.extend(entry[1] for entry in doc.get_toc(simple=True))
        except Exception:
            pass
        chunks.extend(str(value) for value in (doc.metadata or {}).values())
        return "\n".join(chunks)
