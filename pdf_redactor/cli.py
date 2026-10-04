"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import __version__, ocr as ocr_module
from .backends import (
    PDF_IMPORT_ERROR,
    DocumentError,
    FileReport,
    RedactionOptions,
    get_backend,
    supported_extensions,
)
from .detectors import (
    ALL_KINDS,
    CONFIDENCE_ORDER,
    DEFAULT_KINDS,
    RULES,
    Candidate,
    detect_in_page,
    merge_candidates,
)
from .matching import normalize
from .policy import Policy, PolicyError, read_terms_file
from .progress import NullProgress, ProgressBar
from .ui import UI, Style, plural, truncate
from .watermark import DEFAULT_TEXT as WATERMARK_TEXT

EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_INTERRUPT = 0, 1, 2, 130
MIN_TERM_LENGTH = 2


class UsageError(Exception):
    """Usage error: message plus hint, no traceback."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------
def collect_files(
    inputs: Sequence[Path], recursive: bool, ui: UI, suffix: str = ""
) -> Tuple[List[Path], List[Path]]:
    """Return (supported files, skipped files).

    Inside directories, files carrying `suffix` are copies produced by an
    earlier run: they are skipped rather than redacted a second time.
    """
    extensions = set(supported_extensions())
    files: List[Path] = []
    skipped: List[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            found = 0
            for child in sorted(path.rglob("*") if recursive else path.glob("*")):
                if not child.is_file():
                    continue
                if child.suffix.lower() in extensions and not (
                    suffix and child.stem.endswith(suffix)
                ):
                    files.append(child)
                    found += 1
                else:
                    skipped.append(child)
            if found == 0:
                ui.warn(
                    f"{path}: no supported document found"
                    + ("" if recursive else " (try -r for sub-directories)")
                )
        elif path.is_file():
            if path.suffix.lower() not in extensions:
                raise UsageError(
                    f"unsupported format: {path.name}",
                    hint=f"known formats: {', '.join(sorted(extensions))}",
                )
            files.append(path)
        elif not path.exists():
            raise UsageError(
                f"path not found: {path}",
                hint="check the spelling, or quote the path if it contains spaces",
            )
        else:
            raise UsageError(f"unusable path: {path}")
    return list(dict.fromkeys(files)), skipped


def default_output_root(inputs: Sequence[Path], suffix: str) -> Optional[Path]:
    """Default output directory: the source directory is never polluted."""
    first = Path(inputs[0]).expanduser()
    if len(inputs) == 1 and first.is_dir():
        first = first.resolve()  # "." or ".." have no name to suffix
        return first.with_name(first.name + suffix)
    return None


def resolve_output(
    src: Path,
    inputs: Sequence[Path],
    output: Optional[Path],
    suffix: str,
) -> Path:
    if output is None:
        return src.with_name(f"{src.stem}{suffix}{src.suffix}")
    output = Path(output).expanduser()
    first = Path(inputs[0]).expanduser()
    if (
        len(inputs) == 1
        and first.is_file()
        and output.suffix.lower() == src.suffix.lower()
    ):
        return output
    base = first if first.is_dir() else first.parent
    try:
        relative = src.resolve().relative_to(base.resolve())
    except ValueError:
        relative = Path(src.name)
    return output / relative


def check_output_location(
    inputs: Sequence[Path], output: Optional[Path], recursive: bool
) -> None:
    """Prevent writing inside the tree being scanned (recursion, overwrites)."""
    if output is None:
        return
    out = Path(output).expanduser().resolve()
    for raw in inputs:
        source = Path(raw).expanduser().resolve()
        if source.is_dir() and recursive and (out == source or source in out.parents):
            raise UsageError(
                f"the output directory {out} is inside {source}, which is "
                "scanned recursively",
                hint="choose an output directory outside the source tree",
            )


# --------------------------------------------------------------------------
# Interactive prompting
# --------------------------------------------------------------------------
ANSWER_HELP = [
    ("y / Enter", "redact this value"),
    ("n", "leave it alone"),
    ("t", "redact every value of this type"),
    ("i", "ignore every value of this type"),
    ("a", "redact all remaining candidates"),
    ("q", "stop asking, keep the decisions already made"),
    ("?", "show this help"),
]


def ask_candidates(
    candidates: Sequence[Candidate],
    policy: Policy,
    ui: UI,
    auto_kinds: Sequence[str],
    accept_all: bool,
) -> None:
    """Complete the policy so every candidate is settled.

    Type-wide rules ("t" answer, `kind_rules` of a saved policy) only record
    an intent: the values they cover are added to the redaction list at the
    end, because only that list is actually redacted.
    """
    auto = {kind.upper() for kind in auto_kinds}

    def decision(candidate: Candidate) -> Optional[bool]:
        if candidate.kind in auto:
            return True
        return policy.decision(candidate.value, candidate.kind)

    pending = [c for c in candidates if decision(c) is None]
    if pending and not accept_all:
        if not sys.stdin.isatty():
            raise UsageError(
                f"{plural(len(pending), 'detected value')} still to confirm, but "
                "standard input is not a terminal",
                hint="for non-interactive runs use --accept-all, --auto TYPES or "
                "--policy file.json",
            )
        _prompt(pending, policy, ui)

    for candidate in candidates:
        verdict = decision(candidate)
        if verdict or (verdict is None and accept_all):
            policy.add_redact(candidate.value)


def _prompt(pending: Sequence[Candidate], policy: Policy, ui: UI) -> None:
    """Ask about each candidate; type-wide answers settle the following ones."""
    ui.title(f"{plural(len(pending), 'value')} to confirm")
    total = len(pending)
    for index, candidate in enumerate(pending, 1):
        if policy.decision(candidate.value, candidate.kind) is not None:
            continue  # already settled by a "t" or "i" answer
        ui.blank()
        ui.raw(
            f"[{index}/{total}] {ui.paint(candidate.kind, Style.BOLD)} "
            f"{ui.paint('·', Style.DIM)} {candidate.confidence} confidence "
            f"{ui.paint('·', Style.DIM)} {plural(candidate.count, 'occurrence')}"
        )
        ui.raw(f"    value   : {ui.paint(candidate.value, Style.BOLD, Style.YELLOW)}")
        ui.raw(
            ui.paint(
                f"    context : "
                f"{truncate(candidate.occurrences[0].context, ui.width - 15)}",
                Style.DIM,
            )
        )
        where = sorted(
            {f"{o.source} · {o.page_label}" for o in candidate.occurrences}
        )
        ui.raw(
            ui.paint(
                "    where   : "
                + truncate(", ".join(where[:3]), ui.width - 15)
                + (f" (+{len(where) - 3})" if len(where) > 3 else ""),
                Style.DIM,
            )
        )
        while True:
            try:
                answer = ui.prompt("    Redact? [Y/n/t/i/a/q/?] ").strip().lower()
            except EOFError:
                return
            if answer in ("", "y", "yes", "o", "oui"):
                policy.add_redact(candidate.value)
                break
            if answer in ("n", "no", "non"):
                policy.add_ignore(candidate.value)
                break
            if answer == "t":
                policy.set_kind(candidate.kind, "always")
                policy.add_redact(candidate.value)
                break
            if answer == "i":
                policy.set_kind(candidate.kind, "never")
                policy.add_ignore(candidate.value)
                break
            if answer == "a":
                for rest in pending[index - 1 :]:
                    if policy.decision(rest.value, rest.kind) is None:
                        policy.add_redact(rest.value)
                return
            if answer == "q":
                return
            ui.blank()
            ui.table([[ui.paint(key, Style.BOLD), text] for key, text in ANSWER_HELP])


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------
def detect_candidates(
    files: Sequence[Path],
    kinds: Sequence[str],
    min_confidence: str,
    options: RedactionOptions,
    ui: UI,
) -> List[Candidate]:
    threshold = CONFIDENCE_ORDER.get(min_confidence, 0)
    groups: List[List[Candidate]] = []
    # Messages are printed once the bar is gone, so they never interleave
    # with it. An unreadable file is reported again by the redaction phase.
    failures: List[Tuple[str, str]] = []  # (message, hint)
    analysed: List[str] = []
    with _bar(len(files), "Analysing", ui) as bar:
        for path in files:
            bar.set_detail(path.name)
            try:
                pages = get_backend(path).read_pages(path, options)
            except DocumentError as exc:
                failures.append((f"{path.name}: {exc}", exc.hint))
            else:
                for page in pages:
                    groups.append(detect_in_page(page, source=path.name, kinds=kinds))
                analysed.append(f"{path} — {plural(len(pages), 'page')}")
            bar.update(1)
    for line in analysed:
        ui.detail(line)
    for message, hint in failures:
        ui.error(message)
        if hint:
            ui.hint(hint)
    merged = merge_candidates(groups)
    return [c for c in merged if CONFIDENCE_ORDER.get(c.confidence, 0) >= threshold]


def redact_files(
    files: Sequence[Path],
    inputs: Sequence[Path],
    output_root: Optional[Path],
    suffix: str,
    terms: Sequence[str],
    options: RedactionOptions,
    force: bool,
    ui: UI,
) -> List[FileReport]:
    reports: List[FileReport] = []
    produced: set = set()  # resolved destinations written during this run
    with _bar(len(files), "Redacting", ui) as bar:
        for src in files:
            bar.set_detail(src.name)
            dest = resolve_output(src, inputs, output_root, suffix)
            report = FileReport(source=src, output=dest)
            started = time.monotonic()
            try:
                _check_destination(src, dest, produced, force)
                produced.add(dest.resolve())
                report = get_backend(src).redact(
                    src,
                    dest,
                    terms,
                    options,
                    progress=lambda done, total, label: bar.set_detail(
                        f"{src.name} — {label}" if label else src.name
                    ),
                )
            except DocumentError as exc:
                report.add_error(str(exc), getattr(exc, "hint", ""))
                report.duration = time.monotonic() - started
            except OSError as exc:
                report.add_error(
                    f"system error: {exc}",
                    "check permissions, free disk space and the path",
                )
                report.duration = time.monotonic() - started
            except Exception as exc:  # one bad file must not abort the batch
                report.add_error(
                    f"unexpected error ({type(exc).__name__}: {exc})",
                    "the file may be malformed; please report it if it opens "
                    "correctly elsewhere",
                )
                report.duration = time.monotonic() - started
            reports.append(report)
            bar.update(1)
    return reports


def _check_destination(src: Path, dest: Path, produced: set, force: bool) -> None:
    """Refuse any destination that would destroy a file we must keep."""
    target = dest.resolve()
    if target == src.resolve():
        raise DocumentError(
            "the output file would be the source file",
            hint="the original is never modified: pick -o or another suffix",
        )
    if target in produced:
        raise DocumentError(
            f"{dest} was already produced from another input (same file name)",
            hint="redact these inputs separately, or into separate -o directories",
        )
    if dest.exists() and not force:
        raise DocumentError(f"{dest} already exists", hint="use --force to overwrite it")


def _bar(total: int, label: str, ui: UI):
    if ui.quiet:
        return NullProgress()
    return ProgressBar(total, label=label, stream=ui.stream)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def print_summary(reports: Sequence[FileReport], ui: UI) -> None:
    if not reports:
        return
    ui.title("Result")
    rows: List[List[str]] = []
    for report in reports:
        symbol = ui.paint("✓", Style.GREEN) if report.ok else ui.paint("✖", Style.RED)
        rows.append(
            [
                symbol,
                truncate(report.source.name, 34),
                f"{report.total_occurrences}",
                f"{report.pages} p.",
                f"{report.duration:.1f}s",
                ui.paint("→ " + str(report.output), Style.DIM) if report.ok else "",
            ]
        )
    ui.table(rows, aligns="llrrrl")

    for report in reports:
        details = [term for term in report.terms if term.occurrences]
        if details and ui.verbose:
            ui.blank()
            ui.raw(ui.paint(f"  {report.source.name}", Style.BOLD))
            ui.table(
                [
                    [f"{term.occurrences} ×", truncate(term.term, ui.width - 20)]
                    for term in sorted(details, key=lambda t: -t.occurrences)
                ],
                aligns="rl",
            )

    warnings = [(r.source.name, w) for r in reports for w in r.warnings]
    if warnings:
        ui.blank()
        for name, message in warnings[:12]:
            ui.warn(f"{name}: {message}")
        if len(warnings) > 12:
            ui.warn(f"… and {len(warnings) - 12} more warning(s)")

    for report in reports:
        for message in report.errors:
            ui.error(f"{report.source.name}: {message}")
        for hint in report.hints:
            ui.hint(hint)

    total_occurrences = sum(r.total_occurrences for r in reports)
    failures = sum(1 for r in reports if not r.ok)
    ocr_pages = sum(len(r.ocr_pages) for r in reports)
    flattened = sum(1 for r in reports if r.flattened)
    ui.blank()
    line = (
        f"{plural(len(reports), 'file')} · "
        f"{plural(total_occurrences, 'occurrence')} redacted"
    )
    if ocr_pages:
        line += f" · {plural(ocr_pages, 'page')} OCR'd"
    if flattened:
        line += f" · {flattened} flattened"
    line += f" · {failures} failure(s)"
    (ui.error if failures else ui.success)(line)


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-redactor",
        description=(
            "Redact personal data from a PDF (or a directory of PDFs). Content "
            "is genuinely removed from the produced file; the original is never "
            "modified and the copy carries a watermark."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  pdf-redactor contract.pdf -s 'Jane Doe' -s 'jane@example.com'\n"
            "  pdf-redactor folder/ -r --policy pii.json\n"
            "  pdf-redactor scan.pdf --ocr force --ocr-language eng\n"
            "  pdf-redactor contract.pdf --dry-run\n"
        ),
    )
    io_group = parser.add_argument_group("input / output")
    io_group.add_argument("inputs", nargs="*", type=Path,
                          help="PDF file(s) or directory(ies)")
    io_group.add_argument("-o", "--output", type=Path,
                          help="output file (single input) or output directory")
    io_group.add_argument("-r", "--recursive", action="store_true",
                          help="walk sub-directories")
    io_group.add_argument("--suffix", default=".redacted",
                          help="suffix of produced files/directories "
                               "(default: .redacted)")
    io_group.add_argument("--force", action="store_true",
                          help="overwrite existing output files")
    io_group.add_argument("--report", type=Path, help="write a JSON report")

    terms_group = parser.add_argument_group("what to redact")
    terms_group.add_argument("-s", "--string", action="append", default=[],
                             metavar="TEXT", help="string to redact (repeatable)")
    terms_group.add_argument("--strings-file", type=Path,
                             help="text file with one string per line")
    terms_group.add_argument("--policy", type=Path,
                             help="JSON decision file, read AND updated")
    terms_group.add_argument("--detect", dest="detect", action="store_true",
                             default=None,
                             help="force automatic detection (default when no -s)")
    terms_group.add_argument("--no-detect", dest="detect", action="store_false",
                             help="disable automatic detection")
    terms_group.add_argument("--types", default=",".join(DEFAULT_KINDS),
                             help="detected types, comma separated")
    terms_group.add_argument("--list-types", action="store_true",
                             help="list detectable types and exit")
    terms_group.add_argument("--auto", default="", metavar="TYPES",
                             help="types redacted without prompting")
    terms_group.add_argument("--accept-all", action="store_true",
                             help="redact every detected candidate")
    terms_group.add_argument("--min-confidence", choices=("low", "medium", "high"),
                             default="low", help="candidate confidence threshold")

    render_group = parser.add_argument_group("rendering")
    render_group.add_argument("--mode", choices=("stars", "box", "label"),
                              default="stars",
                              help="stars, box (black rectangle) or label")
    render_group.add_argument("--char", default="*",
                              help="truncation character (default: *)")
    render_group.add_argument("--label", default="[REDACTED]",
                              help="text drawn in label mode")
    render_group.add_argument("--watermark", default=WATERMARK_TEXT,
                              help=f"watermark text (default: '{WATERMARK_TEXT}')")
    render_group.add_argument("--no-watermark", action="store_true",
                              help="do not stamp a watermark")
    render_group.add_argument("--no-footer", action="store_true",
                              help="watermark without the dated footer line")
    render_group.add_argument("--watermark-opacity", type=float, default=0.11,
                              metavar="0..1",
                              help="watermark opacity (default: 0.11)")

    match_group = parser.add_argument_group("matching")
    match_group.add_argument("--whole-word", action="store_true",
                             help="whole-word matches only")
    match_group.add_argument("--case-sensitive", action="store_true",
                             help="respect letter case")
    match_group.add_argument("--no-accent-folding", action="store_true",
                             help="do not treat 'e' and 'é' as equivalent")

    ocr_group = parser.add_argument_group("OCR (scanned documents)")
    ocr_group.add_argument("--ocr", choices=("auto", "force", "never"), default="auto",
                           help="auto = OCR only pages without a text layer (default)")
    ocr_group.add_argument("--ocr-language", default="auto",
                           help="Tesseract language ('eng', 'fra', 'fra+eng'...)")
    ocr_group.add_argument("--ocr-dpi", type=int, default=200,
                           help="rendering resolution for OCR (default: 200)")
    ocr_group.add_argument("--check-ocr", action="store_true",
                           help="report the OCR installation status and exit")

    irreversible_group = parser.add_argument_group("irreversibility")
    irreversible_group.add_argument(
        "--flatten", nargs="?", const="always", default="auto",
        choices=("auto", "always", "never"),
        help="rasterize the produced document so no object can be removed to "
             "reveal the redaction. auto = whenever an area sits on an image "
             "(default)")
    irreversible_group.add_argument("--flatten-dpi", type=int, default=200,
                                    help="rasterization resolution (default: 200)")
    irreversible_group.add_argument("--flatten-quality", type=int, default=85,
                                    metavar="30..100",
                                    help="JPEG quality; 100 = lossless (default: 85)")
    irreversible_group.add_argument("--verify-image", action="store_true",
                                    help="re-read the output with OCR to prove no "
                                         "term stayed readable (slow)")

    misc_group = parser.add_argument_group("misc")
    misc_group.add_argument("--keep-metadata", action="store_true",
                            help="do not strip document metadata")
    misc_group.add_argument("--redact-images", action="store_true",
                            help="erase pixels under every redacted area")
    misc_group.add_argument("--no-verify", action="store_true",
                            help="do not re-read the produced file")
    misc_group.add_argument("--dry-run", action="store_true",
                            help="analyse everything, write nothing")
    misc_group.add_argument("--color", choices=("auto", "always", "never"),
                            default="auto", help="colourise the output")
    misc_group.add_argument("-v", "--verbose", action="store_true",
                            help="per-term and per-file detail")
    misc_group.add_argument("-q", "--quiet", action="store_true",
                            help="only show errors")
    misc_group.add_argument("--version", action="version",
                            version=f"%(prog)s {__version__}")
    return parser


def _split_kinds(value: str) -> List[str]:
    return [kind.strip().upper() for kind in value.split(",") if kind.strip()]


def _build_options(args) -> RedactionOptions:
    options = RedactionOptions(
        mode=args.mode,
        replacement_char=args.char,
        label=args.label,
        fold_accents=not args.no_accent_folding,
        ignore_case=not args.case_sensitive,
        whole_word=args.whole_word,
        strip_metadata=not args.keep_metadata,
        redact_images=args.redact_images,
        ocr=args.ocr,
        ocr_language=args.ocr_language,
        ocr_dpi=args.ocr_dpi,
        flatten=args.flatten,
        flatten_dpi=args.flatten_dpi,
        flatten_quality=args.flatten_quality,
        verify_image=args.verify_image,
        watermark=None if args.no_watermark else (args.watermark or None),
        watermark_opacity=args.watermark_opacity,
        watermark_footer=not args.no_footer,
        verify=not args.no_verify,
    )
    try:
        options.validate()
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    return options


def _gather_terms(args, policy: Policy) -> List[str]:
    explicit = [normalize(value) for value in args.string]
    if args.strings_file:
        explicit.extend(read_terms_file(args.strings_file))
    _check_term_lengths(explicit)
    for term in explicit:
        if term:
            policy.add_redact(term)
    return [term for term in explicit if term]


def _check_term_lengths(terms: Sequence[str]) -> None:
    too_short = sorted({t for t in terms if 0 < len(t) < MIN_TERM_LENGTH})
    if too_short:
        raise UsageError(
            "term(s) too short: " + ", ".join(f"'{t}'" for t in too_short),
            hint="a single-character term would redact nearly the whole document",
        )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run(argv: Optional[Sequence[str]], ui: UI) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui.quiet = args.quiet
    ui.verbose = args.verbose and not args.quiet
    ui.color = ui.resolve_color(args.color)

    if args.list_types:
        ui.table(
            [["TYPE", "CONFIDENCE", "DESCRIPTION"]]
            + [
                [
                    rule.kind,
                    rule.confidence,
                    rule.description
                    + ("" if rule.enabled_by_default else "  (off by default)"),
                ]
                for rule in RULES
            ]
        )
        return EXIT_OK

    if args.check_ocr:
        ui.raw(f"OCR: {ocr_module.describe()}")
        if ocr_module.is_available():
            ui.raw(f"Languages: {', '.join(ocr_module.available_languages())}")
            ui.raw(f"tessdata: {ocr_module.tessdata_dir()}")
            ui.success("OCR is operational")
            return EXIT_OK
        ui.error("OCR unavailable")
        ui.hint(ocr_module.INSTALL_HINT)
        return EXIT_FAILURE

    if not args.inputs:
        raise UsageError(
            "no document given",
            hint="example: pdf-redactor contract.pdf -s 'Jane Doe'",
        )

    if PDF_IMPORT_ERROR:
        raise DocumentError(
            f"PDF support unavailable: {PDF_IMPORT_ERROR}",
            hint="python -m pip install -r requirements.txt",
        )
    options = _build_options(args)
    files, ignored = collect_files(args.inputs, args.recursive, ui, args.suffix)
    if not files:
        raise UsageError(
            "no supported document found",
            hint=f"known extensions: {', '.join(supported_extensions())}",
        )
    if ignored and not args.quiet:
        ui.detail(
            f"{plural(len(ignored), 'file')} skipped "
            "(unsupported format or earlier output)"
        )

    output_root = args.output or default_output_root(args.inputs, args.suffix)
    check_output_location(args.inputs, output_root, args.recursive)

    policy = Policy.load(args.policy)
    explicit = _gather_terms(args, policy)

    detect = args.detect if args.detect is not None else not explicit
    if detect:
        kinds = _split_kinds(args.types)
        unknown = [kind for kind in kinds if kind not in ALL_KINDS]
        if unknown:
            raise UsageError(
                f"unknown type(s): {', '.join(unknown)}",
                hint="full list: --list-types",
            )
        auto_kinds = _split_kinds(args.auto)
        unknown_auto = [kind for kind in auto_kinds if kind not in ALL_KINDS]
        if unknown_auto:
            raise UsageError(f"unknown type(s) in --auto: {', '.join(unknown_auto)}")

        ui.step(
            f"Analysing {plural(len(files), 'document')}"
            + (
                f" · OCR {options.ocr} ({ocr_module.describe()})"
                if options.ocr != "never"
                else ""
            )
        )
        candidates = detect_candidates(files, kinds, args.min_confidence, options, ui)
        ui.step(f"{plural(len(candidates), 'candidate')} detected")
        ask_candidates(candidates, policy, ui, auto_kinds, args.accept_all)

    if args.policy:
        saved = policy.save()
        if saved:
            ui.detail(f"policy saved: {saved}")

    terms = policy.redact
    _check_term_lengths(terms)  # a hand-edited policy file is not trusted either
    if not terms:
        ui.warn("no string to redact: nothing to do")
        ui.hint("use -s TEXT, --strings-file, or accept some candidates")
        return EXIT_FAILURE

    if args.dry_run:
        ui.title("Dry run — no file written")
        ui.raw(
            f"  {plural(len(terms), 'term')} would be redacted across "
            f"{plural(len(files), 'document')}"
        )
        ui.table([["·", truncate(term, ui.width - 8)] for term in terms])
        return EXIT_OK

    ui.step(
        f"Redacting {plural(len(files), 'document')} · "
        f"{plural(len(terms), 'term')} · {options.mode} mode"
        + ("" if options.watermark else " · no watermark")
    )
    reports = redact_files(
        files, args.inputs, output_root, args.suffix, terms, options, args.force, ui
    )
    print_summary(reports, ui)

    if args.report:
        _write_report(args.report, terms, reports, options, ui)

    ui.blank()
    ui.warn(
        "Read the produced document: automatic detection is not exhaustive "
        "(implicit data, text inside images that were not OCR'd...)."
    )
    if any(report.flattened for report in reports) and not options.verify_image:
        ui.hint(
            "flattened documents: the textual check cannot see pixels — re-run "
            "with --verify-image for an OCR-based proof"
        )
    return EXIT_FAILURE if any(not r.ok for r in reports) else EXIT_OK


def _write_report(path: Path, terms, reports, options: RedactionOptions, ui: UI) -> None:
    payload = {
        "version": __version__,
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "options": {
            "mode": options.mode,
            "ocr": options.ocr,
            "flatten": options.flatten,
            "watermark": options.watermark,
            "verify": options.verify,
            "verify_image": options.verify_image,
        },
        "terms": list(terms),
        "files": [report.to_dict() for report in reports],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ui.detail(f"report: {path}")
    except OSError as exc:
        ui.warn(f"report not written ({exc})")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ui = UI()
    try:
        return run(argv, ui)
    except (UsageError, PolicyError) as exc:
        ui.error(str(exc))
        if exc.hint:
            ui.hint(exc.hint)
        return EXIT_USAGE
    except ocr_module.OcrUnavailableError as exc:
        ui.error(str(exc))
        ui.hint("or disable OCR with --ocr never")
        return EXIT_USAGE
    except DocumentError as exc:
        ui.error(str(exc))
        if exc.hint:
            ui.hint(exc.hint)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        ui.blank()
        ui.warn("interrupted by the user — files already written are kept")
        return EXIT_INTERRUPT
    except BrokenPipeError:  # pragma: no cover
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
