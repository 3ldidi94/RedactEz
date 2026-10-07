"""Tests for 2.2: new identifiers, name variants, grayscale flattening,
image and DOCX backends. Data is fictional."""

import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from redactez import ocr as ocr_module  # noqa: E402
from redactez.backends import RedactionOptions, get_backend  # noqa: E402
from redactez.cli import ask_candidates, detect_candidates, main, resolve_output  # noqa: E402
from redactez.detectors import (  # noqa: E402
    RULES_BY_KIND,
    VARIANT_KIND,
    Candidate,
    Occurrence,
    detect_in_page,
    find_name_variants,
    name_variants,
)
from redactez.policy import Policy  # noqa: E402
from redactez.textmap import PageText  # noqa: E402
from redactez.ui import UI  # noqa: E402

from make_docx import make_docx  # noqa: E402
from test_core import make_pdf, text_of  # noqa: E402


def quiet_ui() -> UI:
    ui = UI(stream=io.StringIO())
    ui.color = False
    return ui


def found(text, kind):
    page = PageText(number=0, text=text)
    return [c.value for c in detect_in_page(page, "t", kinds=[kind])]


class TestNewIdentifiers(unittest.TestCase):
    def test_valid_numbers_are_found(self):
        cases = {
            "FR_TAX_ID": "Numéro fiscal 30 23 217 600 053",
            "FR_SIRET": "SIRET 732 829 320 00074",
            "FR_SIREN": "SIREN 732 829 320",
            "FR_PASSPORT": "Passeport 12AB34567 délivré",
            "BE_NRN": "NN 85.07.30-033.28",
            "UK_NINO": "NINO AB 12 34 56 C",
            "US_SSN": "SSN 123-45-6789",
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.assertTrue(found(text, kind), text)

    def test_checksums_reject_near_misses(self):
        cases = {
            "FR_TAX_ID": "30 23 217 600 054",
            "FR_SIRET": "732 829 320 00075",
            "FR_SIREN": "732 829 321",
            "BE_NRN": "85.07.30-033.29",
            "US_SSN": "666-45-6789",
            "UK_NINO": "GB 12 34 56 C",
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.assertFalse(found(text, kind), text)

    def test_identity_document_labels(self):
        values = found("N° de passeport : 18XY12345\nID card number: X4RTBPFW4", "LABELED_FIELD")
        self.assertIn("18XY12345", values)
        self.assertIn("X4RTBPFW4", values)


class TestNameVariants(unittest.TestCase):
    def test_variants_of_a_name(self):
        self.assertEqual(name_variants("Jane DOE"), ["DOE", "J. DOE", "DOE Jane"])
        self.assertEqual(name_variants("Jean Martin"), ["Martin", "J. Martin", "Martin Jean"])
        # Not shaped like a name: no variant.
        self.assertEqual(name_variants("12 rue des Lilas"), [])
        self.assertEqual(name_variants("DUPONT"), [])

    def _pages(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_pdf(Path(tmp) / "a.pdf", lines)
            return get_backend(src, cached=False).read_pages(src)

    def test_only_variants_present_outside_the_full_name(self):
        pages = self._pages(["Client: Mme Jane DOE", "Signed: J. DOE", "Jane DOE again"])
        parent = Candidate("Jane DOE", "NAME_WITH_TITLE", "medium")
        variants = find_name_variants([parent], [("a.pdf", p) for p in pages])
        values = {v.value: v for v in variants}
        # "DOE" only occurs inside "Jane DOE" or "J. DOE": covered or proposed
        # through the more specific variant, never on its own.
        self.assertIn("J. DOE", values)
        self.assertEqual(values["J. DOE"].kind, VARIANT_KIND)
        self.assertEqual(values["J. DOE"].parent, "Jane DOE")

    def test_cli_detection_proposes_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_pdf(Path(tmp) / "a.pdf", ["Client: Mme Jane DOE", "Signed: J. DOE"])
            kinds = ["NAME_WITH_TITLE", VARIANT_KIND]
            cands = detect_candidates([src], kinds, "low", RedactionOptions(), quiet_ui())
        self.assertIn(("J. DOE", "Jane DOE"), {(c.value, c.parent) for c in cands})

    def _variant(self):
        occ = Occurrence("a.pdf", "page 1", 0, 6, "[J. DOE]")
        parent = Candidate("Jane DOE", "NAME_WITH_TITLE", "medium", [occ])
        variant = Candidate("J. DOE", VARIANT_KIND, "medium", [occ], parent="Jane DOE")
        return parent, variant

    def test_variant_of_accepted_name_is_redacted_in_batch_mode(self):
        parent, variant = self._variant()
        policy = Policy()
        policy.add_redact("Jane DOE")  # decided earlier, e.g. by a saved policy
        with mock.patch("sys.stdin.isatty", return_value=False):
            ask_candidates([parent, variant], policy, quiet_ui(), [], accept_all=False)
        self.assertIn("J. DOE", policy.redact)

    def test_variant_of_rejected_name_is_left_alone(self):
        parent, variant = self._variant()
        policy = Policy()
        policy.add_ignore("Jane DOE")
        with mock.patch("sys.stdin.isatty", return_value=False):
            ask_candidates([parent, variant], policy, quiet_ui(), [], accept_all=False)
        self.assertNotIn("J. DOE", policy.redact)

    def test_variant_follows_auto_parent(self):
        parent, variant = self._variant()
        policy = Policy()
        with mock.patch("sys.stdin.isatty", return_value=False):
            ask_candidates([parent, variant], policy, quiet_ui(), ["NAME_WITH_TITLE"], False)
        self.assertEqual(set(policy.redact), {"Jane DOE", "J. DOE"})

    def test_variant_can_be_ignored_by_type(self):
        parent, variant = self._variant()
        policy = Policy()
        policy.add_redact("Jane DOE")
        policy.set_kind(VARIANT_KIND, "never")
        with mock.patch("sys.stdin.isatty", return_value=False):
            ask_candidates([parent, variant], policy, quiet_ui(), [], accept_all=False)
        self.assertNotIn("J. DOE", policy.redact)


class TestGrayscale(unittest.TestCase):
    def test_flatten_in_grayscale(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_pdf(Path(tmp) / "a.pdf", ["Client: Jean Dupont"])
            out = Path(tmp) / "a.out.pdf"
            options = RedactionOptions(flatten="always", flatten_grayscale=True)
            report = get_backend(src).redact(src, out, ["Jean Dupont"], options)
            self.assertTrue(report.flattened)
            with pymupdf.open(str(out)) as doc:
                xref = doc[0].get_images(full=True)[0][0]
                image = pymupdf.Pixmap(doc, xref)
                self.assertEqual(image.n, 1)  # one channel: gray


def make_png(path: Path, lines) -> Path:
    page = pymupdf.open().new_page(width=420, height=40 + 40 * len(lines))
    for i, line in enumerate(lines):
        page.insert_text((30, 50 + 40 * i), line, fontsize=16)
    page.get_pixmap(dpi=200).save(str(path))
    return path


class TestImages(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_output_names(self):
        for name, expected in (("a.png", "a.redacted.png"), ("a.jpg", "a.redacted.jpg"),
                               ("a.tif", "a.redacted.pdf"), ("a.TIFF", "a.redacted.pdf")):
            with self.subTest(name=name):
                src = self.tmp / name
                self.assertEqual(resolve_output(src, [src], None, ".redacted").name, expected)

    def test_unreadable_image(self):
        bad = self.tmp / "bad.png"
        bad.write_bytes(b"not an image")
        with self.assertRaises(Exception) as ctx:
            get_backend(bad, cached=False).read_pages(bad)
        self.assertIn("image", str(ctx.exception))

    @unittest.skipUnless(ocr_module.is_available(), "Tesseract unavailable")
    def test_image_is_redacted_in_its_pixels(self):
        src = make_png(self.tmp / "scan.png", ["Client: Jane Doe", "This line stays."])
        before = src.read_bytes()
        out = self.tmp / "scan.redacted.png"
        backend = get_backend(src, cached=False)
        report = backend.redact(src, out, ["Jane Doe"], RedactionOptions(verify_image=True))
        self.assertEqual(before, src.read_bytes())  # source untouched
        self.assertTrue(report.ok, report.errors)
        self.assertGreaterEqual(report.total_occurrences, 1)
        self.assertTrue(report.verified_image)
        # Same format, same resolution.
        self.assertEqual(pymupdf.Pixmap(str(out)).width, pymupdf.Pixmap(str(src)).width)
        with pymupdf.open(str(out)) as image:
            pdf = pymupdf.open("pdf", image.convert_to_pdf())
            text = ocr_module.ocr_textpage(pdf[0], ocr_module.resolve_language(), 200).extractText()
        self.assertNotIn("Jane", text)
        self.assertIn("stays", text)


class TestDocx(unittest.TestCase):
    TERMS = ["Jane Doe", "jane.doe@example.com"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = make_docx(self.tmp / "c.docx")

    def tearDown(self):
        self._tmp.cleanup()

    def redact(self, **options):
        out = self.tmp / "c.redacted.docx"
        report = get_backend(self.src).redact(self.src, out, self.TERMS, RedactionOptions(**options))
        return report, out

    def all_text(self, path):
        with zipfile.ZipFile(path) as archive:
            return {n: archive.read(n).decode("utf-8", "replace") for n in archive.namelist()}

    def test_detection_sees_every_part(self):
        pages = get_backend(self.src).read_pages(self.src)
        labels = {p.label for p in pages}
        self.assertTrue({"body", "header 1", "comments", "links"} <= labels, labels)
        body = next(p for p in pages if p.label == "body")
        self.assertIn("Client: Jane Doe", body.text)  # runs joined
        emails = [c.value for p in pages for c in detect_in_page(p, "c", kinds=["EMAIL"])]
        self.assertIn("jane.doe@example.com", emails)  # only stored in the link

    def test_redaction_removes_the_data_everywhere(self):
        before = self.src.read_bytes()
        report, out = self.redact()
        self.assertEqual(before, self.src.read_bytes())
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(report.verified)
        parts = self.all_text(out)
        blob = "\n".join(parts.values())
        self.assertNotIn("Jane", blob)
        self.assertNotIn("jane.doe", blob)
        self.assertIn("Unrelated line that must survive.", parts["word/document.xml"])
        self.assertIn("REDACTED COPY", parts["word/document.xml"])
        self.assertNotIn("docProps/thumbnail.jpeg", parts)
        self.assertNotIn("thumbnail", parts["_rels/.rels"])

    def test_output_is_well_formed_xml(self):
        _, out = self.redact()
        for name, text in self.all_text(out).items():
            if name.endswith((".xml", ".rels")):
                with self.subTest(part=name):
                    ET.fromstring(text.encode("utf-8"))

    def test_keep_metadata_still_masks_terms(self):
        report, out = self.redact(strip_metadata=False)
        core = self.all_text(out)["docProps/core.xml"]
        self.assertIn("<dc:title>Contract</dc:title>", core)
        self.assertNotIn("Jane", core)
        self.assertTrue(report.verified)

    def test_modes(self):
        _, out = self.redact(mode="label", label="[X]", watermark=None)
        document = self.all_text(out)["word/document.xml"]
        self.assertIn("Client: [X]", document)
        self.assertNotIn("REDACTED COPY", document)

    def test_cli_end_to_end(self):
        code = main([str(self.src), "-s", "Jane Doe", "-q"])
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "c.redacted.docx").exists())

    def test_damaged_and_protected_files(self):
        broken = self.tmp / "broken.docx"
        broken.write_bytes(b"PK\x03\x04 truncated")
        ole = self.tmp / "locked.docx"
        ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
        for path, word in ((broken, "damaged"), (ole, "password")):
            with self.subTest(path=path.name):
                with self.assertRaises(Exception) as ctx:
                    get_backend(path).read_pages(path)
                self.assertIn(word, str(ctx.exception))
                self.assertTrue(getattr(ctx.exception, "hint", ""))


if __name__ == "__main__":
    unittest.main()
