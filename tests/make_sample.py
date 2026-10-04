"""Generate a test PDF containing FICTIONAL personal data.

Usage: python tests/make_sample.py /tmp/sample.pdf
Requires reportlab (test-only dependency).
"""

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


def ssn_with_checksum(body: str) -> str:
    key = 97 - (int(body) % 97)
    return f"{body}{key:02d}"


def french_iban(bban: str) -> str:
    rearranged = bban + "FR00"
    converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    check = 98 - (int(converted) % 97)
    return f"FR{check:02d}{bban}"


SSN = ssn_with_checksum("1800375116001")
IBAN = french_iban("30006000011234567890189")

PAGE_ONE = f"""
<b>TENANCY AGREEMENT — LANDLORD'S COPY</b><br/><br/>
Between the undersigned, Monsieur Jean-François Dupont, born on 12/03/1980 in
Lyon, residing at 42 bis rue des Acacias, 69003 Lyon, hereafter "the tenant",<br/><br/>
Nom : DUPONT<br/>
Prénom : Jean-François<br/>
Date of birth: 12/03/1980<br/>
Address: 42 bis rue des Acacias, 69003 Lyon<br/>
Phone: 06 12 34 56 78<br/>
Email: jean-francois.dupont@example.com<br/>
Social security number: {SSN}<br/>
IBAN: {IBAN}<br/>
Plate: AB-123-CD<br/><br/>
This agreement binds Mr Dupont for three years. All correspondence will be sent
to jean-francois.dupont@example.com or to 06 12 34 56 78. Payments will be
collected from account {IBAN}.
"""

PAGE_TWO = """
<b>APPENDIX — CONDITION REPORT</b><br/><br/>
Drawn up in the presence of Madame Claire Martin (agent) and Jean-François
Dupont. Property address: 42 bis rue des Acacias, 69003 Lyon.
Managing agent: c.martin@agency-example.com — 04 78 00 11 22.<br/><br/>
Note: the tenant Jean-François Dupont reported a leak on 03/09/2026. A spare set
of keys was handed over to Claire Martin.
"""


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")
    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        title="Dupont agreement",
        author="Jean-François Dupont",
        subject="Lease 42 rue des Acacias",
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph(PAGE_ONE, styles["Normal"]),
        Spacer(1, 18),
        PageBreak(),
        Paragraph(PAGE_TWO, styles["Normal"]),
    ]
    doc.build(story)
    print(f"written: {out}")
    print(f"SSN  : {SSN}")
    print(f"IBAN : {IBAN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
