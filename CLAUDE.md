# CLAUDE.md

Working notes for this repository. The README is for users; this file is for
whoever **changes** the code.

## The project in one sentence

A Python CLI that redacts personal data from PDFs by **genuinely removing** the
content (text and pixels), with assisted detection, interactive prompting, OCR
for scans, a watermark, and verification of the output.

**Language: English everywhere** — code, comments, docstrings, user-facing
messages, CLI option names, tests, documentation. Detection rules still contain
French keywords (street types, field labels, month names) because the documents
they target are French; that is data, not prose. Keep it that way and add
keywords for other locales rather than replacing them.

## Commands

```bash
python3 -m pip install -r requirements.txt   # PyMuPDF
python3 -m unittest discover -s tests -q     # 83 tests, ~5 s (web tests skip without Flask)
python3 -m redactez --help
python3 -m redactez --check-ocr          # Tesseract diagnostics
python3 web/webapp.py                    # web UI on :8020 (needs web/requirements.txt)
python3 tests/make_sample.py /tmp/sample.pdf # test PDF (needs reportlab)
```

OCR tests skip themselves when Tesseract is missing
(`@unittest.skipUnless(ocr_module.is_available(), …)`). Do not make them
mandatory.

## Architecture

```
redactez/
  textmap.py        PageText: page text + per-character boxes; geometry helpers
  matching.py       tolerant patterns (case, accents, hyphenation, spacing)
  detectors.py      detection rules + checksum validators
  policy.py         decisions persisted as JSON, O(1) folded-form index
  ocr.py            Tesseract discovery and invocation
  watermark.py      watermark stamped on produced documents
  progress.py       progress bar (stdlib, silent outside a TTY)
  ui.py             colours, messages, tables
  backends/base.py  Backend ABC, RedactionOptions, FileReport, errors, registry
  backends/pdf.py   PDF implementation (PyMuPDF) — most of the work lives here
  cli.py            arguments, phases, prompting, summary
web/
  webapp.py         Flask front-end: /analyze (JSON) then /redact (PDF)
  templates/        single page: upload, candidate review, pdf.js preview
```

Flow: `cli` collects files → `backend.read_pages` → `detectors` → prompting →
`policy` → `backend.redact` → summary.

`textmap.PageText` is the meeting point: detection and redaction operate on
**the same string**, which is what guarantees that anything detected can be
redacted. Do not introduce a parallel extraction path.

## Invariants — do not break these

1. **The source file is never modified.** No in-place writing, no `--in-place`
   option. Test: `test_source_is_never_modified`.
2. **Redaction removes, it does not cover.** Never draw a rectangle without
   `apply_redactions`. The same rule applies to any new backend: the data must
   disappear from the file.
3. **Verification happens before flattening.** After rasterization there is no
   text left, so a textual check would be trivially satisfied — that is, a lie.
   See `verified_before_flatten` in `redact()`.
4. **The watermark is applied by default**, after redaction and before saving
   (otherwise `apply_redactions` would erase it).
5. **When in doubt, over-redact.** Any geometric or matching approximation must
   lean towards protection, never towards looks.

## Known traps (all found by testing, not by reading the docs)

### Geometry: shrink for text, grow for pixels

MuPDF deletes every glyph that **touches** the redaction area, and the boxes of
two consecutive lines touch each other. An area matching the exact line height
therefore also cuts the lines above and below, at the same horizontal position.
→ `shrink_vertically()` (−18 %) before the text pass.

Pixel erasure, on the other hand, covers exactly the rectangle, and glyphs in
an image overflow the measured box (accents, descenders, or a text layer poorly
aligned with the image of an already-OCR'd scan).
→ `expand_for_pixels()` (margin proportional to the height) before the pixel
pass.

These two requirements contradict each other, hence **two**
`apply_redactions` passes per page: the first with `text=REMOVE, images=NONE`,
the second with `text=NONE, images=PIXELS`. Do not merge them.

### An already-OCR'd scan is not detected as a scan

Image plus invisible text layer: `page_needs_ocr()` returns False because there
is text. Removing only the text leaves the data **readable in the pixels**
under the white rectangle. This was a real security bug (fixed in 1.2). The
correct trigger is `_images_under()`: does an area overlap an image? If so,
erase pixels **and** flatten in `auto` mode. Regression test:
`TestIrreversibility`.

### The textual check proves nothing on a scan

`verified: true` only means "no term extractable from the text". On a
rasterized document that is meaningless. The only conclusive check is
`--verify-image` (`_verify_visual`), which re-runs OCR on the output. There is
a negative test (`test_visual_check_detects_a_leak`) proving that a leak is
actually caught: do not delete it, it is what validates the check.

### The final check must be stricter than redaction, never looser

`find_residual()` (matching.py) reuses `build_pattern` but always folds case
and accents, so a variant left alone by `--case-sensitive` is still reported.
It honours `--whole-word` only, otherwise "Martin" is always "found" inside
"Martinez". `_extractable_text()` must list every place `_scrub_*` cleans:
add to both when you add one.

### Type-wide decisions are intents, not redactions

`kind_rules` ("t" answer, saved policy) only make `Policy.decision()` return
True. Only `policy.redact` is redacted, so `ask_candidates` adds every
candidate whose decision is True at the end. Skipping that step silently
leaves data in clear (bug fixed in 2.1, `TestRegressions`).

### Form field text is not in `rawdict`

AcroForm widgets are handled separately (`_scrub_widgets`), as are annotations,
links and bookmarks. A new backend must ask the same question: *where else does
this format store text?*

### PyMuPDF API

- `TextWriter.append()` takes `font=pymupdf.Font("helv")`, **not** `fontname=`.
- `Page.insert_image()` does not accept `clip=`.
- `PDF_REDACT_*` constants may be missing on older versions: go through
  `_const(name, default)`.
- Always import `pymupdf` with a fallback to `fitz` (`backends/pdf.py`).

## Performance

- LRU extraction cache in `PdfBackend` (`CACHE_CHAR_BUDGET`), and backend
  instances shared through `get_backend(path, cached=True)`. Without this, OCR
  runs again between analysis and redaction: 10× slower on a batch of scans.
  Breaking the instance sharing breaks that saving silently.
- `merge_line_rects()` merges by sorted bands, O(n log n). Do not go back to
  pairwise comparison.
- `Policy.decision()` must stay O(1) (folded-form index).
- Rough figures: 50 pages of native text ≈ 1 s; OCR ≈ 1 s per page; flattening
  ≈ 0.2 s per page at 200 dpi.

## Web front-end (`web/`)

A thin layer over the library, like the CLI: it must never re-implement
detection or redaction. Rules specific to it:

- **Backends are created with `get_backend(path, cached=False)`.** The shared
  instance keeps an extraction cache: on a server that would hold one visitor's
  text in RAM for the next one.
- **Nothing on disk beyond the request**: one `TemporaryDirectory` per request.
  Test: `test_no_temp_files_left_behind`.
- **A file that fails verification is withheld** (422), never delivered.
- **No redacted value in logs or headers**: log the exception type only; the
  `X-RedactEz-Report` header carries counts. Test:
  `test_report_header_never_carries_a_redacted_value`.
- The browser never stores terms or detected values (`localStorage` holds the
  options only).
- New `RedactionOptions` field: expose it in `_options()` and the form if it
  makes sense on the web, and add its default to `DEFAULTS`.

## Conventions

- **Errors**: raise a `DocumentError` subclass with a `message` and a `hint`
  (a concrete next step: the exact `qpdf` command, the package to install...).
  The CLI prints them as is. No tracebacks in user-facing output.
- **Exit codes**: 0 success, 1 processing failure, 2 usage error, 130 interrupt.
- **Output**: everything goes through `ui.UI` (stderr). Never call `print()`
  directly in library code; standard output stays free.
- **Progress**: `ProgressBar` disables itself outside a TTY. Do not add a
  `tqdm` or `rich` dependency: the project deliberately stands on PyMuPDF alone.
- **CLI options** are grouped with `add_argument_group`. Every new option must
  appear in the README (a simple consistency check: compare `build_parser()`
  with the README tables).
- **New `RedactionOptions` field**: add validation in `validate()` and a case to
  `test_invalid_options`.

## Adding a format

```python
from redactez.backends.base import Backend, FileReport, register

@register
class DocxBackend(Backend):
    name = "docx"
    extensions = (".docx",)

    def read_pages(self, path, options=None, progress=None): ...    # -> List[PageText]
    def redact(self, path, out, terms, options, progress=None): ... # -> FileReport
```

`PageText` may be supplied without geometry (`boxes=[]`) when substitution
happens on the text itself. Detection, prompting, policy, watermark, reporting,
progress and the CLI then work unchanged.

Do not forget, in a new backend: metadata, comments, document properties,
embedded content, and a final check of the output.

## Out of scope (deliberate decisions, do not undo without a reason)

- No NER or language model for name detection: heavy dependency,
  non-reproducible results, and interactive prompting already covers the need.
- No graphical interface.
- No parallel processing: prompting is sequential and the gains would be
  marginal next to OCR.
- No in-place writing, not even as an option.

## Handling the data

The files processed contain real personal data. The policy file (`--policy`)
holds it **in clear text**; it is created with mode `0600`. Never log a redacted
value, never include one in an error message destined for a shared log, and do
not leave temporary files behind. Test fixtures use fictional data: keep it
that way.
