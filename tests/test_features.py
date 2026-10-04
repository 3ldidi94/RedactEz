"""Tests for watermark, OCR, output paths, errors, progress and irreversibility."""

import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from pdf_redactor import ocr as ocr_module  # noqa: E402
from pdf_redactor import ui as ui_module  # noqa: E402
from pdf_redactor.backends import (  # noqa: E402
    FileReport,
    RedactionOptions,
    get_backend,
)
from pdf_redactor.cli import (  # noqa: E402
    UsageError,
    ask_candidates,
    check_output_location,
    collect_files,
    default_output_root,
    main,
    resolve_output,
)
from pdf_redactor.detectors import RULES_BY_KIND, Candidate, Occurrence  # noqa: E402
from pdf_redactor.matching import build_pattern, find_residual  # noqa: E402
from pdf_redactor.policy import Policy, PolicyError, read_terms_file  # noqa: E402
from pdf_redactor.progress import NullProgress, ProgressBar  # noqa: E402
from pdf_redactor.textmap import merge_line_rects  # noqa: E402
from pdf_redactor.watermark import DEFAULT_TEXT  # noqa: E402

from test_redactor import make_pdf, text_of  # noqa: E402


def make_scan(path: Path, lines) -> Path:
    """PDF without a text layer: every page is a single image."""
    source = pymupdf.open()
    page = source.new_page()
    y = 80
    for line in lines:
        page.insert_text((72, y), line, fontname="helv", fontsize=13)
        y += 20
    scan = pymupdf.open()
    pixmap = page.get_pixmap(dpi=160)
    target = scan.new_page(width=page.rect.width, height=page.rect.height)
    target.insert_image(target.rect, pixmap=pixmap)
    scan.save(str(path))
    scan.close()
    source.close()
    return path


def make_preocr_scan(path: Path, lines) -> Path:
    """Already OCR'd scan: visible image plus an invisible text layer on top.

    The trickiest case: the text can be removed, yet the data stays in the
    pixels underneath a plain white rectangle.
    """
    source = pymupdf.open()
    page = source.new_page()
    y = 90
    for line in lines:
        page.insert_text((72, y), line, fontname="helv", fontsize=13)
        y += 26
    doc = pymupdf.open()
    target = doc.new_page(width=page.rect.width, height=page.rect.height)
    target.insert_image(target.rect, pixmap=page.get_pixmap(dpi=160))
    for word in page.get_text("words"):
        target.insert_text((word[0], word[3]), word[4], fontsize=13, render_mode=3)
    doc.save(str(path))
    doc.close()
    source.close()
    return path


def embedded_image(path: Path, index: int = 0):
    doc = pymupdf.open(str(path))
    try:
        xref = doc[index].get_images(full=True)[0][0]
        return pymupdf.Pixmap(doc.extract_image(xref)["image"])
    finally:
        doc.close()


class TestWatermark(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_watermark_applied_by_default(self):
        src = make_pdf(self.tmp / "a.pdf", ["Client: Jean Dupont"])
        out = self.tmp / "a.out.pdf"
        get_backend(src).redact(src, out, ["Jean Dupont"], RedactionOptions())
        body = text_of(out)
        self.assertIn(DEFAULT_TEXT, body)
        self.assertIn("working copy", body)
        self.assertNotIn("Dupont", body.replace(DEFAULT_TEXT, ""))

    def test_watermark_can_be_disabled(self):
        src = make_pdf(self.tmp / "b.pdf", ["Client: Jean Dupont"])
        out = self.tmp / "b.out.pdf"
        get_backend(src).redact(
            src, out, ["Jean Dupont"], RedactionOptions(watermark=None)
        )
        self.assertNotIn(DEFAULT_TEXT, text_of(out))

    def test_custom_watermark_text(self):
        src = make_pdf(self.tmp / "c.pdf", ["Jean Dupont"])
        out = self.tmp / "c.out.pdf"
        get_backend(src).redact(
            src,
            out,
            ["Jean Dupont"],
            RedactionOptions(watermark="INTERNAL COPY", watermark_footer=False),
        )
        body = text_of(out)
        self.assertIn("INTERNAL COPY", body)
        self.assertNotIn("working copy", body)

    def test_source_untouched_by_watermark(self):
        src = make_pdf(self.tmp / "d.pdf", ["Jean Dupont"])
        before = src.read_bytes()
        get_backend(src).redact(
            src, self.tmp / "d.out.pdf", ["Dupont"], RedactionOptions()
        )
        self.assertEqual(before, src.read_bytes())


@unittest.skipUnless(ocr_module.is_available(), "Tesseract unavailable")
class TestOcr(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.language = ocr_module.resolve_language("auto")

    def tearDown(self):
        self._tmp.cleanup()

    def options(self, **kwargs):
        return RedactionOptions(ocr_language=self.language, **kwargs)

    def test_scan_without_ocr_finds_nothing(self):
        src = make_scan(self.tmp / "s.pdf", ["Client DUPONT", "Address Acacias"])
        out = self.tmp / "s.out.pdf"
        report = get_backend(src).redact(
            src, out, ["DUPONT"], self.options(ocr="never")
        )
        self.assertEqual(report.total_occurrences, 0)
        self.assertTrue(
            any("scanned" in warning for warning in report.warnings),
            f"a scanned-page warning is expected: {report.warnings}",
        )

    def test_scan_with_ocr_redacts_pixels(self):
        src = make_scan(self.tmp / "s.pdf", ["Client DUPONT", "Address Acacias"])
        out = self.tmp / "s.out.pdf"
        report = get_backend(src).redact(src, out, ["DUPONT"], self.options(ocr="auto"))
        self.assertGreaterEqual(report.total_occurrences, 1)
        self.assertEqual(report.ocr_pages, [1])

        before = pymupdf.open(str(src))[0].get_pixmap(dpi=100)
        after = pymupdf.open(str(out))[0].get_pixmap(dpi=100)
        self.assertNotEqual(before.samples, after.samples)  # pixels erased

    def test_ocr_detection_pipeline(self):
        src = make_scan(self.tmp / "s.pdf", ["Email: jane@example.com"])
        pages = get_backend(src).read_pages(src, self.options())
        self.assertTrue(pages[0].from_ocr)
        self.assertIn("@", pages[0].text)

    def test_unknown_language_message(self):
        with self.assertRaises(ocr_module.OcrUnavailableError) as context:
            ocr_module.resolve_language("klingon")
        self.assertIn("Installed languages", str(context.exception))


class TestOutputPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_folder_default_output_is_a_sibling(self):
        folder = self.tmp / "documents"
        folder.mkdir()
        root = default_output_root([folder], ".redacted")
        self.assertEqual(root, self.tmp / "documents.redacted")

    def test_single_file_default_keeps_suffix(self):
        src = self.tmp / "a.pdf"
        src.touch()
        self.assertEqual(
            resolve_output(src, [src], None, ".redacted"), self.tmp / "a.redacted.pdf"
        )

    def test_output_inside_recursive_source_is_refused(self):
        folder = self.tmp / "src"
        folder.mkdir()
        with self.assertRaises(UsageError):
            check_output_location([folder], folder / "out", recursive=True)

    def test_output_outside_source_is_accepted(self):
        folder = self.tmp / "src"
        folder.mkdir()
        check_output_location([folder], self.tmp / "out", recursive=True)


class TestErrors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_too_short_term_is_refused(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        self.assertEqual(main([str(src), "-s", "a", "-q"]), 2)

    def test_invalid_policy_json(self):
        path = self.tmp / "p.json"
        path.write_text("{this is not JSON", encoding="utf-8")
        with self.assertRaises(PolicyError) as context:
            Policy.load(path)
        self.assertIn("invalid JSON", str(context.exception))

    def test_policy_wrong_shape(self):
        path = self.tmp / "p.json"
        path.write_text('["a", "b"]', encoding="utf-8")
        with self.assertRaises(PolicyError):
            Policy.load(path)

    def test_missing_terms_file(self):
        with self.assertRaises(PolicyError):
            read_terms_file(self.tmp / "missing.txt")

    def test_empty_terms_file(self):
        path = self.tmp / "t.txt"
        path.write_text("# only comments\n\n", encoding="utf-8")
        with self.assertRaises(PolicyError):
            read_terms_file(path)

    def test_corrupted_pdf_message(self):
        bad = self.tmp / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 this is not a valid PDF")
        self.assertEqual(main([str(bad), "-s", "Dupont", "-q"]), 1)

    def test_encrypted_pdf_message(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        encrypted = self.tmp / "enc.pdf"
        doc = pymupdf.open(str(src))
        doc.save(
            str(encrypted),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="x",
            owner_pw="x",
        )
        doc.close()
        self.assertEqual(main([str(encrypted), "-s", "Jean Dupont", "-q"]), 1)

    def test_invalid_options(self):
        with self.assertRaises(ValueError):
            RedactionOptions(mode="unknown").validate()
        with self.assertRaises(ValueError):
            RedactionOptions(ocr_dpi=10).validate()
        with self.assertRaises(ValueError):
            RedactionOptions(replacement_char="ab").validate()
        with self.assertRaises(ValueError):
            RedactionOptions(watermark_opacity=3).validate()

    def test_report_json(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        report = self.tmp / "r.json"
        main([str(src), "-s", "Jean Dupont", "--report", str(report), "-q"])
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["files"][0]["total_occurrences"], 1)
        self.assertTrue(data["files"][0]["verified"])


class TestProgressAndUi(unittest.TestCase):
    def test_progress_disabled_outside_tty(self):
        stream = io.StringIO()
        bar = ProgressBar(10, "Test", stream=stream)
        with bar:
            bar.update(5, "file.pdf")
        self.assertFalse(bar.enabled)
        self.assertEqual(stream.getvalue(), "")

    def test_progress_draws_on_tty(self):
        class FakeTty(io.StringIO):
            def isatty(self):
                return True

        stream = FakeTty()
        bar = ProgressBar(4, "Test", stream=stream, min_interval=0)
        with bar:
            for _ in range(4):
                bar.update(1, "doc.pdf")
        output = stream.getvalue()
        self.assertIn("100%", output)
        self.assertIn("(4/4)", output)

    def test_null_progress_is_silent(self):
        with NullProgress() as bar:
            bar.update(3, "x")
            bar.set_detail("y")

    def test_plain_strips_ansi(self):
        self.assertEqual(ui_module._plain("\033[1mbold\033[0m"), "bold")

    def test_truncate(self):
        self.assertEqual(ui_module.truncate("abcdefgh", 4), "abc…")
        self.assertEqual(ui_module.truncate("abc", 10), "abc")

    def test_plural(self):
        self.assertEqual(ui_module.plural(1, "file"), "1 file")
        self.assertEqual(ui_module.plural(3, "file"), "3 files")


class TestGeometry(unittest.TestCase):
    def test_same_line_rects_merge(self):
        merged = merge_line_rects([((10, 100, 50, 114), 4), ((52, 100, 80, 114), 3)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][1], 7)

    def test_different_lines_never_merge(self):
        merged = merge_line_rects([((10, 100, 50, 114), 4), ((10, 118, 50, 132), 4)])
        self.assertEqual(len(merged), 2)

    def test_distant_rects_on_same_line_stay_separate(self):
        merged = merge_line_rects([((10, 100, 50, 114), 4), ((200, 100, 240, 114), 4)])
        self.assertEqual(len(merged), 2)


class TestIrreversibility(unittest.TestCase):
    """Redaction must not be undoable by removing an object."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_image_under_zone_triggers_pixel_erasure(self):
        src = make_preocr_scan(self.tmp / "p.pdf", ["Name: DUPONT", "City: Lyon"])
        out = self.tmp / "p.out.pdf"
        report = get_backend(src).redact(src, out, ["DUPONT"], RedactionOptions())
        self.assertEqual(report.raster_pages, [1], "the area overlaps an image")
        self.assertTrue(report.flattened, "the document must be flattened")

    def test_pixels_are_really_erased(self):
        src = make_preocr_scan(self.tmp / "p.pdf", ["Name: DUPONT", "City: Lyon"])
        out = self.tmp / "p.out.pdf"
        get_backend(src).redact(src, out, ["DUPONT"], RedactionOptions())
        self.assertNotEqual(
            embedded_image(src).tobytes("png"),
            embedded_image(out).tobytes("png"),
            "the output image must differ: pixels are erased",
        )

    def test_flattened_output_has_no_removable_object(self):
        src = make_preocr_scan(self.tmp / "p.pdf", ["Name: DUPONT"])
        out = self.tmp / "p.out.pdf"
        get_backend(src).redact(src, out, ["DUPONT"], RedactionOptions())
        doc = pymupdf.open(str(out))
        try:
            page = doc[0]
            self.assertEqual(page.get_text("text").strip(), "")  # no text left
            self.assertEqual(len(list(page.annots() or [])), 0)  # no annotation
            self.assertEqual(len(page.get_images(full=True)), 1)  # a single image
        finally:
            doc.close()

    def test_flatten_never_keeps_text_layer(self):
        src = make_preocr_scan(self.tmp / "p.pdf", ["Name: DUPONT", "City: Lyon"])
        out = self.tmp / "p.out.pdf"
        report = get_backend(src).redact(
            src, out, ["DUPONT"], RedactionOptions(flatten="never")
        )
        self.assertFalse(report.flattened)
        self.assertIn("Lyon", text_of(out))  # text stays selectable

    def test_flatten_always_on_plain_text_document(self):
        src = make_pdf(self.tmp / "t.pdf", ["Client: Jean Dupont"])
        out = self.tmp / "t.out.pdf"
        report = get_backend(src).redact(
            src, out, ["Jean Dupont"], RedactionOptions(flatten="always")
        )
        self.assertTrue(report.flattened)
        self.assertEqual(text_of(out).strip(), "")

    def test_plain_text_document_is_not_flattened_by_default(self):
        src = make_pdf(self.tmp / "t.pdf", ["Client: Jean Dupont"])
        out = self.tmp / "t.out.pdf"
        report = get_backend(src).redact(src, out, ["Jean Dupont"], RedactionOptions())
        self.assertFalse(report.flattened)
        self.assertEqual(report.raster_pages, [])

    def test_flatten_options_validation(self):
        with self.assertRaises(ValueError):
            RedactionOptions(flatten="maybe").validate()
        with self.assertRaises(ValueError):
            RedactionOptions(flatten_dpi=20).validate()
        with self.assertRaises(ValueError):
            RedactionOptions(flatten_quality=5).validate()


@unittest.skipUnless(ocr_module.is_available(), "Tesseract unavailable")
class TestVisualVerification(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.language = ocr_module.resolve_language("auto")

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_output_passes_visual_check(self):
        src = make_preocr_scan(self.tmp / "p.pdf", ["Name: DUPONT"])
        out = self.tmp / "p.out.pdf"
        report = get_backend(src).redact(
            src,
            out,
            ["DUPONT"],
            RedactionOptions(verify_image=True, ocr_language=self.language),
        )
        self.assertTrue(report.verified_image, report.residual_image)

    def test_visual_check_detects_a_leak(self):
        """On a document that was never redacted, the visual check must fail."""
        src = make_preocr_scan(self.tmp / "p.pdf", ["Name: DUPONT"])
        fake = FileReport(source=src, output=src)
        get_backend(src)._verify_visual(
            src, ["DUPONT"], fake, RedactionOptions(ocr_language=self.language)
        )
        self.assertFalse(fake.verified_image)
        self.assertEqual(fake.residual_image, ["DUPONT"])


class TestRegressions(unittest.TestCase):
    """One test per bug fixed in 2.1; each failed before the fix."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def candidates(*values, kind="EMAIL"):
        occurrence = Occurrence("f.pdf", "page 1", 0, 1, "context")
        return [Candidate(v, kind, "high", [occurrence]) for v in values]

    def test_type_wide_answer_redacts_every_value_of_the_type(self):
        policy = Policy()
        ui = ui_module.UI(stream=io.StringIO())
        ui.prompt = mock.Mock(return_value="t")
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            ask_candidates(self.candidates("a@x.fr", "b@y.fr"), policy, ui, [], False)
        self.assertEqual(ui.prompt.call_count, 1)
        self.assertEqual(sorted(policy.redact), ["a@x.fr", "b@y.fr"])

    def test_saved_kind_rule_redacts_new_values(self):
        policy = Policy()
        policy.set_kind("EMAIL", "always")
        ui = ui_module.UI(stream=io.StringIO())
        ask_candidates(self.candidates("a@x.fr"), policy, ui, [], False)
        self.assertEqual(policy.redact, ["a@x.fr"])

    def test_auto_kinds_are_not_persisted(self):
        policy = Policy()
        ui = ui_module.UI(stream=io.StringIO())
        ask_candidates(self.candidates("a@x.fr"), policy, ui, ["email"], False)
        self.assertEqual(policy.redact, ["a@x.fr"])
        self.assertEqual(policy.kind_rules, {})

    def test_default_output_root_of_current_directory(self):
        with mock.patch("os.getcwd", return_value=str(self.tmp)):
            root = default_output_root([Path(".")], ".redacted")
        self.assertEqual(root.name, self.tmp.resolve().name + ".redacted")

    def test_whole_word_verification_has_no_false_alarm(self):
        src = make_pdf(self.tmp / "a.pdf", ["Mr Martin met Martinez"])
        report = get_backend(src).redact(
            src, self.tmp / "o.pdf", ["Martin"], RedactionOptions(whole_word=True)
        )
        self.assertTrue(report.verified, report.errors)
        self.assertIn("Martinez", text_of(self.tmp / "o.pdf"))

    def test_verification_ignores_case_and_accent_options(self):
        """A variant left alone on purpose is still reported, never hidden."""
        self.assertEqual(find_residual("DUPONT", ["Dupont"]), ["Dupont"])
        self.assertEqual(find_residual("Dupond", ["Dupont"]), [])

    def test_accent_folding_beyond_french(self):
        pattern = build_pattern("Dvořák")
        self.assertTrue(pattern.search("DVORAK"))
        self.assertTrue(build_pattern("Lodz").search("Łódź"))

    def test_label_must_start_a_word(self):
        rule = RULES_BY_KIND["LABELED_FIELD"]
        self.assertFalse(rule.pattern.search("Hôtel : Ritz\nusername: bob"))
        self.assertTrue(rule.pattern.search("Tél : 06 12 34 56 78"))

    def test_band_merge_respects_horizontal_order(self):
        # Same line, slightly different tops, right-hand rectangle first.
        right = ((300.0, 100.0, 340.0, 112.0), 4)
        left = ((50.0, 100.3, 90.0, 112.0), 4)
        merged = merge_line_rects([right, left])
        self.assertEqual(len(merged), 2)  # the text in between survives

    def test_short_term_in_policy_file_is_refused(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        policy = self.tmp / "p.json"
        policy.write_text('{"version": 1, "redact": ["e"]}', encoding="utf-8")
        self.assertEqual(main([str(src), "--policy", str(policy), "-q"]), 2)

    def test_same_output_from_two_inputs_is_refused(self):
        for folder in ("one", "two"):
            (self.tmp / folder).mkdir()
            make_pdf(self.tmp / folder / "a.pdf", ["Jean Dupont"])
        out = self.tmp / "out"
        code = main(
            [str(self.tmp / "one"), str(self.tmp / "two"), "-o", str(out),
             "-s", "Jean Dupont", "--force", "-q"]
        )
        self.assertEqual(code, 1)  # the second file must not overwrite the first

    def test_earlier_outputs_are_not_redacted_again(self):
        folder = self.tmp / "docs"
        folder.mkdir()
        make_pdf(folder / "a.pdf", ["Jean Dupont"])
        make_pdf(folder / "a.redacted.pdf", ["Jean Dupont"])
        files, _ = collect_files([folder], False, ui_module.UI(), ".redacted")
        self.assertEqual([f.name for f in files], ["a.pdf"])

    def test_no_temporary_file_left_behind(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        get_backend(src).redact(
            src, self.tmp / "o.pdf", ["Jean Dupont"], RedactionOptions()
        )
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.pdf", "o.pdf"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
