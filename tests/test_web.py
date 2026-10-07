"""Tests for the web front-end (web/webapp.py). Skipped when Flask is missing."""

import base64
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import pymupdf  # noqa: E402

from test_core import make_pdf  # noqa: E402

try:
    import webapp  # noqa: E402
except ImportError:  # Flask not installed: the web layer is optional
    webapp = None

LINES = [
    "Client: Jane Doe",
    "Email: jane.doe@example.com",
    "Unrelated line that must survive.",
]


def pdf_bytes(lines=LINES) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = make_pdf(Path(tmp) / "in.pdf", lines)
        return path.read_bytes()


def text_from(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


@unittest.skipUnless(webapp, "Flask is not installed")
class TestWeb(unittest.TestCase):
    def setUp(self):
        self.client = webapp.app.test_client()
        webapp.app.config["RATE_LIMIT"] = 0  # tests post many times

    def _post(self, url, data=None, **form):
        payload = {"pdf": (io.BytesIO(data or pdf_bytes()), "contract.pdf")}
        payload.update(form)
        return self.client.post(url, data=payload, content_type="multipart/form-data")

    def test_index_and_health(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_analyze_returns_candidates_with_geometry(self):
        resp = self._post("/analyze", kinds=["EMAIL"])
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        body = resp.get_json()
        self.assertEqual(body["pages"], 1)
        values = {c["value"]: c for c in body["candidates"]}
        self.assertIn("jane.doe@example.com", values)
        occurrence = values["jane.doe@example.com"]["occurrences"][0]
        self.assertEqual(occurrence["page"], 0)
        self.assertTrue(occurrence["rects"])

    def test_redact_removes_terms_and_keeps_the_rest(self):
        resp = self._post(
            "/redact",
            terms=json.dumps(["Jane Doe", "jane.doe@example.com"]),
            suffix="redacted",
        )
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        self.assertEqual(resp.mimetype, "application/pdf")
        self.assertIn("contract.redacted.pdf", resp.headers["Content-Disposition"])
        text = text_from(resp.data)
        self.assertNotIn("Jane Doe", text)
        self.assertNotIn("jane.doe@example.com", text)
        self.assertIn("Unrelated line", text)
        report = json.loads(resp.headers["X-RedactEz-Report"])
        self.assertTrue(report["verified"])
        self.assertGreaterEqual(report["occurrences"], 2)

    def test_report_header_never_carries_a_redacted_value(self):
        resp = self._post("/redact", terms=json.dumps(["Jane Doe"]))
        self.assertEqual(resp.status_code, 200)
        for header, value in resp.headers.items():
            if header.lower() != "content-disposition":
                self.assertNotIn("Jane", value, header)

    def test_rejects_bad_input(self):
        cases = [
            ("/redact", {"terms": "[]"}),                    # nothing to redact
            ("/redact", {"terms": json.dumps(["x"])}),       # too short
            ("/redact", {"terms": "not json"}),
            ("/redact", {"terms": json.dumps(["Jane"]), "flatten_dpi": "5000"}),
            ("/redact", {"terms": json.dumps(["Jane"]), "mode": "nope"}),
            ("/analyze", {"kinds": ["NOT_A_KIND"]}),
        ]
        for url, form in cases:
            with self.subTest(url=url, form=form):
                resp = self._post(url, **form)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("error", resp.get_json())

    def test_not_a_pdf(self):
        resp = self.client.post(
            "/redact",
            data={"pdf": (io.BytesIO(b"hello"), "notes.txt"), "terms": '["Jane"]'},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_corrupt_pdf_gives_a_clean_error_without_temp_path(self):
        resp = self._post("/redact", data=b"%PDF-1.7 garbage", terms='["Jane"]')
        self.assertIn(resp.status_code, (400, 422))
        body = resp.get_json()
        self.assertIn("error", body)
        self.assertNotIn(tempfile.gettempdir(), json.dumps(body))

    def test_variants_and_typed_terms_are_located(self):
        data = pdf_bytes(["Client: Mme Jane DOE", "Signed: J. DOE", "Room 42 Lyon"])
        resp = self._post("/analyze", data=data, kinds=["NAME_WITH_TITLE", "NAME_VARIANT"],
                          terms=json.dumps(["Lyon"]))
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        body = resp.get_json()
        variants = {c["value"]: c for c in body["candidates"] if c["kind"] == "NAME_VARIANT"}
        self.assertEqual(variants["J. DOE"]["parent"], "Jane DOE")
        self.assertTrue(variants["J. DOE"]["occurrences"][0]["rects"])
        manual = {m["value"]: m for m in body["manual"]}
        self.assertTrue(manual["Lyon"]["occurrences"][0]["rects"])
        self.assertEqual(len(body["sizes"]), 1)

    def test_names_from_other_files_bring_their_variants(self):
        data = pdf_bytes(["Signed: J. DOE"])  # the full name is in another file
        resp = self._post("/analyze", data=data, kinds=["NAME_VARIANT"],
                          names=json.dumps(["Jane DOE"]))
        values = {c["value"] for c in resp.get_json()["candidates"]}
        self.assertIn("J. DOE", values)

    def test_docx_through_the_web(self):
        from make_docx import make_docx
        with tempfile.TemporaryDirectory() as tmp:
            data = make_docx(Path(tmp) / "c.docx").read_bytes()
        resp = self.client.post(
            "/redact",
            data={"pdf": (io.BytesIO(data), "contract.docx"),
                  "terms": json.dumps(["Jane Doe", "jane.doe@example.com"])},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        self.assertIn("contract.redacted.docx", resp.headers["Content-Disposition"])
        self.assertIn("wordprocessingml", resp.mimetype)
        with zipfile.ZipFile(io.BytesIO(resp.data)) as archive:
            blob = b"".join(archive.read(n) for n in archive.namelist())
        self.assertNotIn(b"Jane", blob)

    def test_basic_auth(self):
        webapp.app.config["AUTH"] = "alice:s3cret"
        try:
            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(self.client.get("/health").status_code, 200)
            bad = base64.b64encode(b"alice:wrong").decode()
            good = base64.b64encode(b"alice:s3cret").decode()
            self.assertEqual(self.client.get("/", headers={"Authorization": "Basic " + bad}).status_code, 401)
            self.assertEqual(self.client.get("/", headers={"Authorization": "Basic " + good}).status_code, 200)
        finally:
            webapp.app.config["AUTH"] = ""

    def test_rate_limit(self):
        webapp.app.config["RATE_LIMIT"] = 2
        webapp._hits.clear()
        try:
            codes = [self._post("/analyze", kinds=["EMAIL"]).status_code for _ in range(3)]
            self.assertEqual(codes[:2], [200, 200])
            self.assertEqual(codes[2], 429)
        finally:
            webapp.app.config["RATE_LIMIT"] = 0
            webapp._hits.clear()

    def test_no_temp_files_left_behind(self):
        before = set(Path(tempfile.gettempdir()).glob("redactez-*"))
        self._post("/analyze", kinds=["EMAIL"])
        self._post("/redact", terms='["Jane Doe"]')
        after = set(Path(tempfile.gettempdir()).glob("redactez-*"))
        self.assertEqual(after - before, set())


if __name__ == "__main__":
    unittest.main()
