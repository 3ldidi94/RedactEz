"""Core regression tests. Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from pdf_redactor import Policy, RedactionOptions, get_backend  # noqa: E402
from pdf_redactor.cli import main  # noqa: E402
from pdf_redactor.detectors import (  # noqa: E402
    RULES_BY_KIND,
    detect_in_page,
    merge_candidates,
)
from pdf_redactor.matching import build_pattern, contains, iter_matches  # noqa: E402


def make_pdf(path: Path, lines, *, widget=None, annot=None, meta=True) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 80
    for line in lines:
        page.insert_text((72, y), line, fontname="helv", fontsize=11)
        y += 14
    if widget:
        field = pymupdf.Widget()
        field.rect = pymupdf.Rect(300, 300, 500, 320)
        field.field_name = "name"
        field.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        field.field_value = widget
        page.add_widget(field)
    if annot:
        page.add_text_annot((400, 100), annot)
    if meta:
        doc.set_metadata({"author": "Jean Dupont", "title": "Dupont file"})
    doc.save(str(path))
    doc.close()
    return path


def text_of(path: Path) -> str:
    doc = pymupdf.open(str(path))
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


class TestMatching(unittest.TestCase):
    def test_case_and_accents(self):
        pattern = build_pattern("Jean-François")
        for variant in ("jean-francois", "JEAN-FRANÇOIS", "Jean\u2011Francois"):
            self.assertTrue(list(iter_matches(variant, pattern)), variant)

    def test_line_break_inside_word(self):
        pattern = build_pattern("Dupont")
        self.assertTrue(list(iter_matches("Du-\npont", pattern)))

    def test_flexible_whitespace(self):
        pattern = build_pattern("Jean Dupont")
        self.assertTrue(list(iter_matches("Jean\n Dupont", pattern)))

    def test_whole_word(self):
        pattern = build_pattern("Martin", whole_word=True)
        self.assertFalse(list(iter_matches("Martinez", pattern)))
        self.assertTrue(list(iter_matches("Mr Martin,", pattern)))

    def test_contains_is_tolerant(self):
        self.assertTrue(contains("JEAN  FRANCOIS", "Jean François"))


class TestDetectors(unittest.TestCase):
    def rule(self, kind):
        return RULES_BY_KIND[kind]

    def test_fr_ssn_checksum_is_verified(self):
        valid = "1800375116001" + f"{97 - int('1800375116001') % 97:02d}"
        self.assertTrue(self.rule("FR_SSN").validator(valid))
        self.assertFalse(self.rule("FR_SSN").validator(valid[:-2] + "00"))

    def test_iban_mod97(self):
        self.assertTrue(self.rule("IBAN").validator("FR7630006000011234567890189"))
        self.assertFalse(self.rule("IBAN").validator("FR7630006000011234567890188"))

    def test_card_luhn(self):
        self.assertTrue(self.rule("CREDIT_CARD").validator("4539 1488 0343 6467"))
        self.assertFalse(self.rule("CREDIT_CARD").validator("4539 1488 0343 6468"))

    def test_phone_no_false_positive(self):
        pattern = self.rule("PHONE").pattern
        self.assertFalse(pattern.findall("amount 1 234,56 euros"))
        self.assertFalse(pattern.findall("SSN 180037511600178"))
        self.assertEqual(pattern.findall("Tel 06 12 34 56 78")[0], "06 12 34 56 78")

    def test_detection_on_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_pdf(
                Path(tmp) / "a.pdf",
                ["Name: DUPONT", "Email: jane@example.com", "Tel: 06 12 34 56 78"],
            )
            pages = get_backend(src).read_pages(src)
            candidates = merge_candidates([detect_in_page(pages[0], "a.pdf")])
            values = {candidate.value for candidate in candidates}
            self.assertIn("jane@example.com", values)
            self.assertIn("DUPONT", values)
            self.assertIn("06 12 34 56 78", values)


class TestRedaction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_text_is_really_removed(self):
        src = make_pdf(self.tmp / "a.pdf", ["First name: Jean-François", "Name: DUPONT"])
        out = self.tmp / "a.redacted.pdf"
        report = get_backend(src).redact(
            src, out, ["Jean-François"], RedactionOptions()
        )
        body = text_of(out)
        self.assertNotIn("Jean-François", body)
        self.assertIn("First name", body)  # the neighbouring label survives
        self.assertIn("DUPONT", body)  # the neighbouring line is untouched
        self.assertTrue(report.verified)
        self.assertEqual(report.terms[0].occurrences, 1)

    def test_neighbour_lines_untouched(self):
        lines = [f"Line {i}: secret value {i}" for i in range(6)]
        src = make_pdf(self.tmp / "b.pdf", lines)
        out = self.tmp / "b.out.pdf"
        get_backend(src).redact(src, out, ["secret value 3"], RedactionOptions())
        body = text_of(out)
        self.assertNotIn("secret value 3", body)
        for index in (0, 1, 2, 4, 5):
            self.assertIn(f"secret value {index}", body)

    def test_metadata_cleared(self):
        src = make_pdf(self.tmp / "c.pdf", ["Hello"])
        out = self.tmp / "c.out.pdf"
        get_backend(src).redact(src, out, ["Hello"], RedactionOptions())
        doc = pymupdf.open(str(out))
        self.assertEqual(doc.metadata.get("author") or "", "")
        doc.close()

    def test_form_field_and_annotation(self):
        src = make_pdf(
            self.tmp / "d.pdf",
            ["Document"],
            widget="Jean Dupont",
            annot="call back Jean Dupont",
        )
        out = self.tmp / "d.out.pdf"
        report = get_backend(src).redact(src, out, ["Jean Dupont"], RedactionOptions())
        doc = pymupdf.open(str(out))
        values = [w.field_value for p in doc for w in (p.widgets() or [])]
        notes = [
            (a.info or {}).get("content", "") for p in doc for a in (p.annots() or [])
        ]
        doc.close()
        self.assertFalse(any("Dupont" in (value or "") for value in values))
        self.assertFalse(any("Dupont" in note for note in notes))
        self.assertTrue(report.verified)

    def test_case_and_accent_variants_are_caught(self):
        src = make_pdf(
            self.tmp / "e.pdf", ["Jean-François", "JEAN-FRANCOIS", "jean-francois"]
        )
        out = self.tmp / "e.out.pdf"
        report = get_backend(src).redact(
            src, out, ["Jean-François"], RedactionOptions()
        )
        self.assertEqual(report.terms[0].occurrences, 3)
        self.assertTrue(report.verified)

    def test_source_is_never_modified(self):
        src = make_pdf(self.tmp / "f.pdf", ["Secret Dupont"])
        before = src.read_bytes()
        get_backend(src).redact(
            src, self.tmp / "f.out.pdf", ["Dupont"], RedactionOptions()
        )
        self.assertEqual(before, src.read_bytes())


class TestPolicy(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            policy = Policy(path=path)
            policy.add_redact("Jean Dupont")
            policy.add_ignore("Paris")
            policy.set_kind("EMAIL", "always")
            policy.save()
            reloaded = Policy.load(path)
            self.assertTrue(reloaded.decision("jean  dupont"))
            self.assertFalse(reloaded.decision("Paris"))
            self.assertTrue(reloaded.decision("unknown@x.com", "EMAIL"))
            self.assertIsNone(reloaded.decision("unknown", "DATE"))


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_folder_recursive(self):
        (self.tmp / "src" / "sub").mkdir(parents=True)
        make_pdf(self.tmp / "src" / "a.pdf", ["Client: Jean Dupont"])
        make_pdf(self.tmp / "src" / "sub" / "b.pdf", ["Contact: Jean Dupont"])
        out = self.tmp / "out"
        code = main(
            [str(self.tmp / "src"), "-r", "-o", str(out), "-s", "Jean Dupont", "-q"]
        )
        self.assertEqual(code, 0)
        self.assertTrue((out / "a.pdf").exists())
        self.assertTrue((out / "sub" / "b.pdf").exists())
        self.assertNotIn("Dupont", text_of(out / "a.pdf"))
        self.assertNotIn("Dupont", text_of(out / "sub" / "b.pdf"))

    def test_existing_output_is_protected(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        out = self.tmp / "out.pdf"
        out.write_bytes(b"already here")
        code = main([str(src), "-o", str(out), "-s", "Jean Dupont", "-q"])
        self.assertEqual(code, 1)
        self.assertEqual(out.read_bytes(), b"already here")

    def test_unknown_kind_rejected(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        self.assertEqual(main([str(src), "--types", "NOPE", "-q"]), 2)

    def test_dry_run_writes_nothing(self):
        src = make_pdf(self.tmp / "a.pdf", ["Jean Dupont"])
        code = main([str(src), "-s", "Jean Dupont", "--dry-run", "-q"])
        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "a.redacted.pdf").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
