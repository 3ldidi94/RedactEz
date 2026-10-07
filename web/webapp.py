#!/usr/bin/env python3
"""Single-page Flask front-end for redactez (thin layer over the library).

It drives the same code as the CLI — `get_backend().read_pages()`,
`detect_in_page()`, `find_name_variants()`, `get_backend().redact()` — so the
two never drift. Flow, mirroring the CLI phases:

1. ``POST /analyze``: one document in, detected candidates out (JSON, with the
   rectangles of every occurrence for the live preview).
2. The browser lets the user tick what to redact (the CLI's prompting step).
3. ``POST /redact``: one document plus the chosen terms in, redacted copy out.

Nothing is kept on disk: every request works in a throwaway temp dir that is
wiped before the response is sent, and backend instances are never shared
between requests (their extraction cache would otherwise hold a previous
visitor's text in memory).

Optional protection, through environment variables:

- ``REDACTEZ_AUTH=user:password`` — HTTP Basic authentication on every page;
- ``REDACTEZ_RATE_LIMIT=120`` — processing requests per minute and per client
  address (0 disables it). In-memory, per worker process.
"""

from __future__ import annotations

import base64
import hmac
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

# The library lives one level up, at the repo root, and stays there (it is the
# product; the web layer is the add-on). Works both in Docker, where redactez/
# sits next to /app/web, and when run straight from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from redactez import __version__, ocr as ocr_module  # noqa: E402
from redactez.backends import (  # noqa: E402
    PDF_IMPORT_ERROR,
    DocumentError,
    RedactionOptions,
    get_backend,
    supported_extensions,
)
from redactez.cli import MIN_TERM_LENGTH  # noqa: E402
from redactez.detectors import (  # noqa: E402
    CONFIDENCE_ORDER,
    RULES,
    RULES_BY_KIND,
    VARIANT_DESCRIPTION,
    VARIANT_KIND,
    Candidate,
    detect_in_page,
    find_name_variants,
    merge_candidates,
)
from redactez.matching import build_pattern, iter_matches, normalize  # noqa: E402
from redactez.watermark import DEFAULT_OPACITY, DEFAULT_TEXT  # noqa: E402

MAX_MB = 100              # per request (one file per request)
MAX_PAGES = 200           # per file: OCR costs ~1 s per page
MAX_TERMS = 2000
MAX_TERM_CHARS = 500
MAX_NAMES = 500           # names from other files, for the variant search
MAX_OCCURRENCES = 300     # per candidate, in the analysis response
MAX_DPI = 400             # raster size guard (OOM / decompression bomb)

MIMETYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
app.config["AUTH"] = os.environ.get("REDACTEZ_AUTH", "")
app.config["RATE_LIMIT"] = int(os.environ.get("REDACTEZ_RATE_LIMIT", "120") or 0)

# Honour a reverse proxy: X-Forwarded-Prefix lets the app live under a
# sub-path (e.g. nginx `location /redactez/`); no effect without the headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


# -------------------------------------------------------------- protection
def _authorized() -> bool:
    expected = app.config.get("AUTH") or ""
    if not expected:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        given = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except Exception:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def _rate_limited() -> int:
    """Seconds to wait if this client went over the limit, else 0."""
    limit = app.config.get("RATE_LIMIT") or 0
    if limit <= 0:
        return 0
    now = time.monotonic()
    key = request.remote_addr or "?"
    with _hits_lock:
        window = _hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            return max(1, int(60 - (now - window[0])) + 1)
        window.append(now)
    return 0


@app.before_request
def _guard():
    if request.path == "/health":
        return None
    if not _authorized():
        return Response("authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="RedactEz", charset="UTF-8"'})
    if request.method == "POST":
        wait = _rate_limited()
        if wait:
            resp = jsonify(error="too many requests, slow down",
                           hint=f"retry in {wait} s")
            resp.status_code = 429
            resp.headers["Retry-After"] = str(wait)
            return resp
    return None


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "worker-src 'self' blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'")
    return resp


# Defaults surfaced to the form, read from the library so they can never fall
# out of step with the CLI.
_D = RedactionOptions()
DEFAULTS = {
    "mode": _D.mode, "char": _D.replacement_char, "label": _D.label,
    "watermark": DEFAULT_TEXT, "watermark_opacity": DEFAULT_OPACITY,
    "flatten": _D.flatten, "flatten_dpi": _D.flatten_dpi,
    "flatten_quality": _D.flatten_quality, "ocr": _D.ocr,
    "ocr_dpi": _D.ocr_dpi, "suffix": "redacted",
}
KINDS = [{"kind": r.kind, "description": r.description,
          "confidence": r.confidence, "default": r.enabled_by_default}
         for r in RULES] + [{"kind": VARIANT_KIND, "description": VARIANT_DESCRIPTION,
                             "confidence": "medium", "default": True}]
KNOWN_KINDS = set(RULES_BY_KIND) | {VARIANT_KIND}
EXTENSIONS = supported_extensions()


class BadRequest(ValueError):
    """Validation error, shown to the user as is (with an optional hint)."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------- form input
def _form(name: str, default: str = "") -> str:
    return (request.form.get(name) or "").strip() or default


def _flag(name: str, default: bool = False) -> bool:
    raw = request.form.get(name)
    if raw is None:
        return default
    return raw in ("on", "1", "true")


def _num(name: str, cast, default):
    raw = _form(name)
    if raw == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        raise BadRequest(f"invalid value for {name}")


def _options() -> RedactionOptions:
    """Build RedactionOptions from the form; same validation as the CLI."""
    watermark = request.form.get("watermark", DEFAULT_TEXT).strip()
    options = RedactionOptions(
        mode=_form("mode", _D.mode),
        replacement_char=request.form.get("char") or _D.replacement_char,
        label=_form("label", _D.label)[:80],
        fold_accents=not _flag("no_accent_folding"),
        ignore_case=not _flag("case_sensitive"),
        whole_word=_flag("whole_word"),
        strip_metadata=not _flag("keep_metadata"),
        redact_images=_flag("redact_images"),
        ocr=_form("ocr", _D.ocr),
        ocr_language=_form("ocr_language", "auto"),
        ocr_dpi=_num("ocr_dpi", int, _D.ocr_dpi),
        flatten=_form("flatten", _D.flatten),
        flatten_dpi=_num("flatten_dpi", int, _D.flatten_dpi),
        flatten_quality=_num("flatten_quality", int, _D.flatten_quality),
        flatten_grayscale=_flag("flatten_gray"),
        verify_image=_flag("verify_image"),
        watermark=(watermark[:120] or None) if _flag("watermark_on", True) else None,
        watermark_opacity=_num("watermark_opacity", float, DEFAULT_OPACITY),
        watermark_footer=_flag("watermark_footer", True),
        verify=True,  # never optional on the web: it is what makes the output trustworthy
    )
    try:
        options.validate()
    except ValueError as exc:
        raise BadRequest(str(exc))
    if options.ocr_dpi > MAX_DPI or options.flatten_dpi > MAX_DPI:
        raise BadRequest(f"resolution must be {MAX_DPI} dpi or less")
    if options.ocr_language != "auto" and not re.fullmatch(
            r"[A-Za-z_]{3,}(\+[A-Za-z_]{3,})*", options.ocr_language):
        raise BadRequest("invalid OCR language")
    return options


def _upload():
    uploads = [u for u in request.files.getlist("pdf") if u and u.filename]
    if len(uploads) != 1:
        raise BadRequest("send exactly one file per request")
    upload = uploads[0]
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise BadRequest(f"unsupported format: {upload.filename}",
                         hint="supported: " + ", ".join(EXTENSIONS))
    return upload, suffix


def _string_list(name: str, limit: int) -> list:
    try:
        raw = json.loads(request.form.get(name) or "[]")
    except ValueError:
        raise BadRequest("invalid term list")
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise BadRequest("invalid term list")
    values = [t for t in dict.fromkeys(normalize(t) for t in raw) if t]
    if len(values) > limit:
        raise BadRequest(f"too many terms (max {limit})")
    if any(len(t) > MAX_TERM_CHARS for t in values):
        raise BadRequest(f"term too long (max {MAX_TERM_CHARS} characters)")
    return values


def _terms(required: bool = True) -> list:
    terms = _string_list("terms", MAX_TERMS)
    if required and not terms:
        raise BadRequest("nothing to redact",
                         hint="tick some detected values or add terms by hand")
    if any(len(t) < MIN_TERM_LENGTH for t in terms):
        raise BadRequest("term(s) too short",
                         hint="a single-character term would redact nearly "
                              "the whole document")
    return terms


def _page_guard(src: Path) -> list:
    """Reject huge page counts before any expensive work (OCR, rasterizing).

    Returns the page sizes in points, for the preview (empty when unknown,
    e.g. a DOCX, which has no fixed layout).
    """
    if src.suffix.lower() == ".docx":
        return []
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - old installs
        import fitz as pymupdf
    try:
        with pymupdf.open(str(src)) as doc:
            if doc.needs_pass:
                return []  # the backend raises the explicit error + hint
            if doc.page_count > MAX_PAGES:
                raise BadRequest(f"too many pages (max {MAX_PAGES})",
                                 hint="split the document, or use the CLI, "
                                      "which has no limit")
            return [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in doc]
    except BadRequest:
        raise
    except Exception:
        return []  # let the backend report the damaged file properly


def _scrub(text: str, tmp: str) -> str:
    """Never echo the server temp path back to the client."""
    return text.replace(tmp, "").replace(str(Path(tmp).resolve()), "")


def _doc_error(exc: DocumentError, tmp: str, status: int = 422):
    return jsonify(error=_scrub(str(exc), tmp),
                   hint=_scrub(getattr(exc, "hint", "") or "", tmp)), status


def _occurrences(occurrences, by_label):
    out = []
    for o in occurrences[:MAX_OCCURRENCES]:
        page = by_label.get(o.page_label)
        rects = page.rects_for(o.start, o.end) if page is not None else []
        out.append({"page": page.number if page is not None else 0,
                    "context": o.context,
                    "rects": [[round(v, 2) for v in r] for r in rects]})
    return out


# -------------------------------------------------------------------- routes
@app.get("/")
def index():
    ocr_langs = ocr_module.available_languages() if ocr_module.is_available() else []
    return render_template(
        "index.html", d=DEFAULTS, kinds=KINDS, max_mb=MAX_MB,
        max_pages=MAX_PAGES, ocr_langs=ocr_langs, version=__version__,
        min_term=MIN_TERM_LENGTH, extensions=EXTENSIONS)


@app.get("/health")
def health():
    return {"status": "ok" if not PDF_IMPORT_ERROR else "degraded",
            "ocr": ocr_module.is_available()}


@app.post("/analyze")
def analyze():
    """Detect candidates in one document. Returns JSON; keeps nothing.

    Optional fields: ``terms`` (typed by hand, located for the preview) and
    ``names`` (names found in the other files of the batch, so their variants
    are looked for here too).
    """
    try:
        upload, suffix = _upload()
        options = _options()
        kinds = [k for k in request.form.getlist("kinds") if k in KNOWN_KINDS]
        if not kinds:
            raise BadRequest("choose at least one data type to detect")
        threshold = CONFIDENCE_ORDER.get(_form("min_confidence", "low"), 0)
        manual = _terms(required=False)
        names = _string_list("names", MAX_NAMES)

        with tempfile.TemporaryDirectory(prefix="redactez-") as tmp:
            src = Path(tmp) / f"in{suffix}"
            upload.save(src)
            sizes = _page_guard(src)
            try:
                # cached=False: a shared instance would keep this text in RAM.
                pages = get_backend(src, cached=False).read_pages(src, options)
            except DocumentError as exc:
                return _doc_error(exc, tmp)

        groups = [detect_in_page(p, source="doc", kinds=kinds) for p in pages]
        merged = [c for c in merge_candidates(groups)
                  if CONFIDENCE_ORDER.get(c.confidence, 0) >= threshold]
        if VARIANT_KIND in kinds:
            # Names from the other files act as parents too ("Jane DOE" in a
            # letter, "Mme DOE" alone in the attachment).
            outside = [Candidate(value=n, kind="NAME_WITH_TITLE", confidence="medium")
                       for n in names]
            merged += find_name_variants(merged + outside, [("doc", p) for p in pages])
        by_label = {p.label: p for p in pages}
        candidates = [{"value": c.value, "kind": c.kind, "confidence": c.confidence,
                       "count": c.count, "parent": c.parent,
                       "occurrences": _occurrences(c.occurrences, by_label)}
                      for c in merged]

        located = []
        for term in manual:
            pattern = build_pattern(term, fold_accents=options.fold_accents,
                                    ignore_case=options.ignore_case,
                                    whole_word=options.whole_word)
            occ = []
            for page in pages:
                for start, end, _ in iter_matches(page.text, pattern):
                    occ.append({"page": page.number, "context": page.context(start, end),
                                "rects": [[round(v, 2) for v in r]
                                          for r in page.rects_for(start, end)]})
                    if len(occ) >= MAX_OCCURRENCES:
                        break
            located.append({"value": term, "occurrences": occ})

        return jsonify(pages=len(pages), sizes=sizes,
                       ocr_pages=[p.number + 1 for p in pages if p.from_ocr],
                       candidates=candidates, manual=located)

    except BadRequest as exc:
        return jsonify(error=str(exc), hint=exc.hint), 400
    except Exception as exc:  # never leak internals (nor document data) to the client
        # Type only: a message or traceback could quote document content.
        app.logger.error("analysis failed: %s", type(exc).__name__)
        return jsonify(error="could not analyse the document"), 500


@app.post("/redact")
def redact():
    """Redact one document with the given terms. Returns the copy; keeps nothing.

    A file whose final check finds a term still readable is NOT returned:
    handing out a leaking document would defeat the point of the tool.
    """
    try:
        upload, suffix = _upload()
        options = _options()
        terms = _terms()
        tag = re.sub(r"[^\w.\-]", "_", _form("suffix", "redacted"))[:60].strip("._")
        tag = tag or "redacted"

        with tempfile.TemporaryDirectory(prefix="redactez-") as tmp:
            src = Path(tmp) / f"in{suffix}"
            upload.save(src)
            _page_guard(src)
            backend = get_backend(src, cached=False)
            out = Path(tmp) / f"out{backend.output_suffix(src)}"
            try:
                report = backend.redact(src, out, terms, options)
            except DocumentError as exc:
                return _doc_error(exc, tmp)
            failed_check = report.verified is False or report.verified_image is False
            if report.errors and not failed_check:
                return jsonify(error=_scrub(report.errors[0], tmp),
                               hint=_scrub(report.hints[0] if report.hints else "", tmp)), 422
            if failed_check:
                leaks = len(set(report.residual) | set(report.residual_image))
                return jsonify(
                    error=f"verification failed: {leaks} term(s) still readable "
                          "in the output, file withheld",
                    hint="enable 'Erase pixels under every area' and flattening "
                         "'always', then retry; or use the CLI to inspect"), 422
            data = out.read_bytes()
            out_suffix = out.suffix

        summary = {
            "pages": report.pages,
            "occurrences": report.total_occurrences,
            "terms_found": sum(1 for t in report.terms if t.occurrences),
            "terms_total": len(report.terms),
            "ocr_pages": report.ocr_pages,
            "flattened": report.flattened,
            "verified": report.verified,
            "verified_image": report.verified_image,
            "warnings": list(report.warnings)[:20],
        }
        stem = Path(secure_filename(upload.filename)).stem or "document"
        resp = send_file(io.BytesIO(data),
                         mimetype=MIMETYPES.get(out_suffix, "application/octet-stream"),
                         as_attachment=True, download_name=f"{stem}.{tag}{out_suffix}")
        # Counts and generic warnings only: never a redacted value in a header.
        resp.headers["X-RedactEz-Report"] = json.dumps(summary, ensure_ascii=True)
        resp.headers["Access-Control-Expose-Headers"] = "X-RedactEz-Report"
        return resp

    except BadRequest as exc:
        return jsonify(error=str(exc), hint=exc.hint), 400
    except Exception as exc:
        app.logger.error("redaction failed: %s", type(exc).__name__)
        return jsonify(error="could not process the document"), 500


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_e):
    return jsonify(error=f"upload too large (limit {MAX_MB} MB per file)"), 413


if __name__ == "__main__":
    # Dev server only; production runs under gunicorn (see Dockerfile).
    app.run(host=os.environ.get("BIND", "127.0.0.1"),
            port=int(os.environ.get("PORT", 8020)), debug=False)
