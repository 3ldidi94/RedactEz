"""Build small .docx files for the tests, with the standard library only.

The documents exercise every place the DOCX backend must clean: a name split
across runs, a header, a comment and its author, a tracked deletion, a
mailto: hyperlink, core metadata and a thumbnail. Data is fictional.
"""

import zipfile
from pathlib import Path

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="jpeg" ContentType="image/jpeg"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail" Target="docProps/thumbnail.jpeg"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="mailto:jane.doe@example.com" TargetMode="External"/>
</Relationships>"""


def _p(*runs: str) -> str:
    return "<w:p>" + "".join(f"<w:r><w:t xml:space=\"preserve\">{r}</w:t></w:r>" for r in runs) + "</w:p>"


def make_docx(path: Path, paragraphs=None, *, author="Jane Doe") -> Path:
    paragraphs = paragraphs or [
        _p("Client: Ja", "ne D", "oe"),  # one name, three runs
        _p("Unrelated line that must survive."),
        '<w:p><w:del w:id="1" w:author="' + author + '"><w:r><w:delText>old Jane Doe</w:delText></w:r></w:del></w:p>',
        '<w:p><w:hyperlink r:id="rId3"><w:r><w:t>write to me</w:t></w:r></w:hyperlink></w:p>',
    ]
    body = "".join(p if p.startswith("<w:p") else _p(p) for p in paragraphs)
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document {W} {R}><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    header = f'<?xml version="1.0" encoding="UTF-8"?><w:hdr {W}>{_p("Case of Jane Doe")}</w:hdr>'
    comments = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:comments {W}>'
        f'<w:comment w:id="0" w:author="{author}" w:initials="JD">{_p("check with Jane Doe")}</w:comment>'
        "</w:comments>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:creator>{author}</dc:creator><dc:title>Contract</dc:title>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
        "</cp:coreProperties>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/header1.xml", header)
        archive.writestr("word/comments.xml", comments)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/thumbnail.jpeg", b"\xff\xd8 fake page-1 picture")
    return path
