"""Regenerate the screenshots used in the README.

Everything is produced from FICTIONAL data, on the fly:

    python3 -m pip install reportlab pillow
    python3 docs/make_screenshots.py

Outputs into docs/: before-after.png, modes.png, scanned.png
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DPI = 150
MARGIN = 24
HEADER = 44
BG = (255, 255, 255)
FRAME = (214, 211, 202)
TEXT = (61, 61, 58)
ACCENT = (162, 45, 45)

TERMS = [
    "Jean-François Dupont",
    "DUPONT",
    "Jean-François",
    "jean-francois.dupont@example.com",
    "06 12 34 56 78",
    "12/03/1980",
    "42 bis rue des Acacias, 69003 Lyon",
    "180037511600178",
    "FR7630006000011234567890189",
    "AB-123-CD",
    "Claire Martin",
]


def font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def run(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pdf_redactor", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(f"pdf-redactor failed: {result.stderr}")


def render(pdf: Path, page: int = 0, top: float = 0.09, bottom: float = 0.42) -> Image.Image:
    doc = pymupdf.open(str(pdf))
    try:
        rect = doc[page].rect
        clip = pymupdf.Rect(
            rect.x0 + rect.width * 0.10,
            rect.y0 + rect.height * top,
            rect.x1 - rect.width * 0.06,
            rect.y0 + rect.height * bottom,
        )
        pixmap = doc[page].get_pixmap(dpi=DPI, clip=clip)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    finally:
        doc.close()


def embedded_image(pdf: Path, crop: tuple[float, float, float, float]) -> Image.Image:
    doc = pymupdf.open(str(pdf))
    try:
        xref = doc[0].get_images(full=True)[0][0]
        pixmap = pymupdf.Pixmap(doc.extract_image(xref)["image"])
        if pixmap.n > 3:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    finally:
        doc.close()
    width, height = image.size
    return image.crop(
        (
            int(width * crop[0]),
            int(height * crop[1]),
            int(width * crop[2]),
            int(height * crop[3]),
        )
    )


def panel(image: Image.Image, title: str, subtitle: str = "", width: int = 620):
    scaled = image.resize(
        (width, int(image.height * width / image.width)), Image.LANCZOS
    )
    canvas = Image.new("RGB", (width, scaled.height + HEADER), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((0, 6), title, font=font(19, bold=True), fill=TEXT)
    if subtitle:
        draw.text((0, 26), subtitle, font=font(14), fill=ACCENT)
    canvas.paste(scaled, (0, HEADER))
    draw.rectangle(
        [0, HEADER, width - 1, HEADER + scaled.height - 1], outline=FRAME, width=1
    )
    return canvas


def compose(panels: list[Image.Image], name: str) -> Path:
    width = sum(p.width for p in panels) + MARGIN * (len(panels) + 1)
    height = max(p.height for p in panels) + MARGIN * 2
    canvas = Image.new("RGB", (width, height), BG)
    x = MARGIN
    for item in panels:
        canvas.paste(item, (x, MARGIN))
        x += item.width + MARGIN
    DOCS.mkdir(exist_ok=True)
    path = DOCS / name
    canvas.save(path, optimize=True)
    print(f"written: {path} ({path.stat().st_size // 1024} kB)")
    return path


def make_preocr_scan(path: Path) -> Path:
    """Already OCR'd scan: visible image plus an invisible text layer."""
    source = pymupdf.open()
    page = source.new_page()
    lines = [
        "TENANCY AGREEMENT — LANDLORD'S COPY",
        "",
        "Name: DUPONT",
        "First name: Jean-François",
        "Date of birth: 12/03/1980",
        "Phone: 06 12 34 56 78",
    ]
    y = 70
    for index, line in enumerate(lines):
        page.insert_text(
            (60, y), line, fontname="hebo" if index == 0 else "helv", fontsize=13
        )
        y += 26
    doc = pymupdf.open()
    target = doc.new_page(width=page.rect.width, height=page.rect.height)
    target.insert_image(target.rect, pixmap=page.get_pixmap(dpi=170))
    for word in page.get_text("words"):
        target.insert_text((word[0], word[3]), word[4], fontsize=13, render_mode=3)
    doc.save(str(path))
    doc.close()
    source.close()
    return path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sample = tmp / "contract.pdf"
        subprocess.run(
            [sys.executable, str(ROOT / "tests" / "make_sample.py"), str(sample)],
            check=True,
            capture_output=True,
        )
        terms: list[str] = []
        for term in TERMS:
            terms += ["-s", term]

        outputs = {}
        for mode in ("stars", "box", "label"):
            out = tmp / f"{mode}.pdf"
            run([str(sample), "-o", str(out), *terms, "--mode", mode, "-q"])
            outputs[mode] = out

        compose(
            [
                panel(render(sample), "Before", "original, untouched"),
                panel(render(outputs["stars"]), "After", "text removed, not covered"),
            ],
            "before-after.png",
        )

        compose(
            [
                panel(render(outputs["stars"], bottom=0.34), "--mode stars", "default", 420),
                panel(render(outputs["box"], bottom=0.34), "--mode box", "black boxes", 420),
                panel(
                    render(outputs["label"], bottom=0.34),
                    "--mode label",
                    "fixed width: hides the length",
                    420,
                ),
            ],
            "modes.png",
        )

        scan = make_preocr_scan(tmp / "scan.pdf")
        scan_out = tmp / "scan.redacted.pdf"
        run([str(scan), "-o", str(scan_out), "-s", "DUPONT", "-s", "12/03/1980", "-q"])
        crop = (0.05, 0.05, 0.75, 0.32)
        compose(
            [
                panel(
                    embedded_image(scan, crop),
                    "Scanned page — embedded image",
                    "data lives in the pixels",
                ),
                panel(
                    embedded_image(scan_out, crop),
                    "After redaction — same image",
                    "pixels destroyed, nothing to un-hide",
                ),
            ],
            "scanned.png",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
