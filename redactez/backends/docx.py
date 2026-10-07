"""DOCX backend (Word 2007+), standard library only.

A .docx is a ZIP of XML parts. Text substitution happens *in the XML text*,
so there is no geometry (`PageText.boxes` is empty) and nothing to rasterize:
the characters are replaced, which removes them from the file.

The XML is edited surgically (text nodes and attribute values located by
regex, spliced in place) rather than parsed and re-serialized: ElementTree
renames namespace prefixes and drops unused declarations, which breaks the
`mc:Ignorable` mechanism and makes Word refuse the file.

Where a .docx stores text, and how each place is handled:

- body, headers, footers, footnotes, endnotes, comments: `w:t` runs, matched
  across run boundaries (Word splits a word into several runs freely);
- tracked deletions (`w:delText`) and field codes (`w:instrText`): same;
- attribute values anywhere (authors of comments and revisions, field
  instructions, alt text of pictures, `people.xml` identities, hyperlink
  targets in `.rels`): masked where a term occurs;
- `customXml/` (data bound to content controls): text nodes;
- `docProps/` metadata: cleared by default, masked with --keep-metadata;
- `docProps/thumbnail.*`: a picture of the first page — always removed;
- pictures (`word/media/`) and embedded objects: not analysed, reported.
"""

from __future__ import annotations

import html
import os
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..matching import build_pattern, find_residual, iter_matches, normalize
from ..textmap import PageText
from .base import (
    Backend,
    CorruptedDocumentError,
    DocumentError,
    EncryptedDocumentError,
    FileReport,
    OutputError,
    ProgressCallback,
    RedactionOptions,
    TermReport,
    register,
)

# Zip-bomb guard: total uncompressed size accepted.
MAX_UNCOMPRESSED = 512 * 1024 * 1024

_WORD_PART = re.compile(
    r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments|glossary/document)\.xml$"
)
_CUSTOM_XML = re.compile(r"customXml/item\d+\.xml$")
_ANY_XML = re.compile(r".+\.(xml|rels)$")
_THUMBNAIL = re.compile(r"docProps/thumbnail\.\w+$")

# Paragraph boundaries, text nodes and in-run separators, in document order.
_TOKEN = re.compile(
    r"<w:p[\s>/]|</w:p>"
    r"|<w:(t|delText|instrText)(\s[^>]*)?>(.*?)</w:\1>"
    r"|<w:(?:tab|br|cr)\b[^>]*/>",
    re.S,
)
_ATTRIBUTE = re.compile(r'(\s[\w:.-]+=)"([^"]*)"')
_TEXT_NODE = re.compile(r">([^<]+)<")

_CORE_KEEP = ("dcterms:created", "dcterms:modified")
_APP_CLEAR = ("Company", "Manager", "HyperlinkBase")

_WATERMARK_COLOR = "B81C1C"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(text: str) -> str:
    return _escape(text).replace('"', "&quot;")


def _label(name: str) -> str:
    """Readable location for prompts and reports: "header 1", "comments"..."""
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if name == "word/document.xml":
        return "body"
    match = re.fullmatch(r"(header|footer)(\d*)", stem)
    if match:
        return f"{match.group(1)} {match.group(2) or 1}"
    if name.startswith("customXml/"):
        return f"bound data {stem}"
    return stem


@dataclass
class _Node:
    """One text node: where its (escaped) content sits in the part."""

    start: int  # content start in the XML string
    end: int  # content end
    open_start: int  # start of the opening tag (to add xml:space)
    open_end: int
    text: str  # unescaped content
    offset: int  # position of `text` in the segment string


@dataclass
class _Segment:
    """Text of one paragraph, split over its text nodes."""

    text: str
    nodes: List[_Node]


def _segments(xml: str) -> List[_Segment]:
    segments: List[_Segment] = []
    parts: List[str] = []
    nodes: List[_Node] = []
    length = 0

    def flush() -> None:
        nonlocal parts, nodes, length
        if nodes:
            segments.append(_Segment("".join(parts), nodes))
        parts, nodes, length = [], [], 0

    for match in _TOKEN.finditer(xml):
        token = match.group(0)
        if token.startswith("<w:p") or token == "</w:p>":
            flush()  # also splits around a text box nested in a paragraph
        elif match.group(1):
            text = html.unescape(match.group(3))
            open_end = match.start(3)
            nodes.append(
                _Node(match.start(3), match.end(3), match.start(), open_end, text, length)
            )
            parts.append(text)
            length += len(text)
        else:  # tab, line break: keeps "Jane<tab>Doe" from reading "JaneDoe"
            parts.append(" ")
            length += 1
    flush()
    return segments


def _plain_text(xml: str) -> str:
    """Text nodes of a non-Word part (customXml, metadata), one per line."""
    return "\n".join(html.unescape(m.group(1)) for m in _TEXT_NODE.finditer(xml) if m.group(1).strip())


@register
class DocxBackend(Backend):
    name = "docx"
    extensions = (".docx",)

    # -------------------------------------------------------------- opening
    @staticmethod
    def _open(path: Path) -> Dict[str, bytes]:
        path = Path(path)
        if not path.is_file():
            raise DocumentError(
                f"file not found: {path}",
                hint="check the path, or quote it if it contains spaces",
            )
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
                    raise CorruptedDocumentError(
                        "document too large once decompressed",
                        hint="this may be a zip bomb; open it in Word to check",
                    )
                parts = {i.filename: archive.read(i) for i in infos}
        except zipfile.BadZipFile as exc:
            # An encrypted .docx is an OLE container, not a ZIP.
            with open(path, "rb") as handle:
                magic = handle.read(8)
            if magic.startswith(b"\xd0\xcf\x11\xe0"):
                raise EncryptedDocumentError(
                    "password protected (or legacy) Word document",
                    hint="open it in Word and save an unprotected .docx copy",
                ) from exc
            raise CorruptedDocumentError(
                f"unreadable or damaged DOCX ({exc})",
                hint="open it in Word and save it again as .docx",
            ) from exc
        if "word/document.xml" not in parts:
            raise CorruptedDocumentError(
                "not a Word document (word/document.xml is missing)",
                hint="only .docx files produced by Word or compatible editors are supported",
            )
        return parts

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="surrogateescape")

    @staticmethod
    def _encode(text: str) -> bytes:
        return text.encode("utf-8", errors="surrogateescape")

    # -------------------------------------------------------------- reading
    def read_pages(
        self,
        path: Path,
        options: Optional[RedactionOptions] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> List[PageText]:
        parts = self._open(path)
        names = [n for n in parts if _WORD_PART.match(n) or _CUSTOM_XML.match(n)]
        names.sort(key=lambda n: (n != "word/document.xml", n))
        pages: List[PageText] = []
        for index, name in enumerate(names):
            if progress:
                progress(index, len(names), _label(name))
            xml = self._decode(parts[name])
            if _WORD_PART.match(name):
                text = "\n".join(s.text for s in _segments(xml))
            else:
                text = _plain_text(xml)
            if text.strip():
                pages.append(PageText(number=len(pages), text=text, label=_label(name)))
        # Hyperlink targets ("mailto:jane@...") are only stored in the .rels.
        links = [
            html.unescape(m.group(1))
            for name in parts
            if name.startswith("word/_rels/")
            for m in re.finditer(
                r'<Relationship\b[^>]*Target="([^"]*)"[^>]*TargetMode="External"',
                self._decode(parts[name]),
            )
        ]
        if links:
            pages.append(PageText(number=len(pages), text="\n".join(links), label="links"))
        if progress:
            progress(len(names), len(names), "")
        if not pages:
            pages.append(PageText(number=0, text="", label="body"))
        return pages

    # ------------------------------------------------------------ redaction
    def redact(
        self,
        path: Path,
        output: Path,
        terms: Sequence[str],
        options: RedactionOptions,
        progress: Optional[ProgressCallback] = None,
    ) -> FileReport:
        started = time.monotonic()
        options.validate()
        report = FileReport(source=Path(path), output=Path(output))
        cleaned = [t for t in dict.fromkeys(normalize(t) for t in terms) if t]
        if not cleaned:
            report.add_error("no term to redact for this document")
            return report
        patterns = {
            term: build_pattern(
                term,
                fold_accents=options.fold_accents,
                ignore_case=options.ignore_case,
                whole_word=options.whole_word,
            )
            for term in cleaned
        }
        counters: Dict[str, int] = dict.fromkeys(cleaned, 0)

        parts = self._open(path)
        names = list(parts)
        report.pages = sum(1 for n in names if _WORD_PART.match(n))
        out: Dict[str, Optional[bytes]] = dict(parts)

        for index, name in enumerate(names):
            if progress:
                progress(index, len(names), _label(name))
            if _THUMBNAIL.match(name):
                out[name] = None  # a rendering of page 1: it shows the data
                continue
            if not _ANY_XML.match(name):
                continue
            xml = self._decode(parts[name])
            if _WORD_PART.match(name):
                xml = self._redact_runs(xml, patterns, counters, options)
            if name.startswith("docProps/") and options.strip_metadata:
                xml = self._strip_metadata(name, xml)
            else:
                xml = self._mask_text_nodes(xml, patterns, counters, options)
            xml = self._mask_attributes(xml, patterns, counters, options)
            if options.strip_metadata:
                xml = self._anonymise_authors(xml)
            out[name] = self._encode(xml)

        self._drop_thumbnail_references(out)
        if options.watermark:
            out["word/document.xml"] = self._encode(
                self._stamp(self._decode(out["word/document.xml"]), options)
            )
        self._warn_unanalysed(names, report)
        if options.flatten == "always":
            report.warnings.append("flattening does not apply to DOCX: text was replaced")
        if options.verify_image:
            report.warnings.append("visual check (OCR) does not apply to DOCX")

        self._write(out, names, Path(output))
        if progress:
            progress(len(names), len(names), "")

        report.terms = [TermReport(term=t, occurrences=counters[t]) for t in cleaned]
        if report.total_occurrences == 0:
            report.warnings.append(
                "no occurrence found in this document (term spelling? text inside "
                "pictures is not analysed)"
            )
        if options.verify:
            self._verify(Path(output), cleaned, report, options)
        report.duration = time.monotonic() - started
        return report

    # ---------------------------------------------------- text substitution
    @staticmethod
    def _replacement(matched: str, options: RedactionOptions) -> str:
        if options.mode == "label":
            return options.label
        char = "█" if options.mode == "box" else options.replacement_char
        return char * max(3, min(len(matched), options.max_stars))

    @staticmethod
    def _spans(text: str, patterns, counters) -> List[Tuple[int, int]]:
        """Union of every term's matches; overlapping ones merged."""
        spans: List[Tuple[int, int]] = []
        for term, pattern in patterns.items():
            for start, end, _ in iter_matches(text, pattern):
                spans.append((start, end))
                counters[term] += 1
        spans.sort()
        merged: List[Tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def _redact_runs(self, xml: str, patterns, counters, options) -> str:
        """Replace matches inside paragraphs, across run boundaries."""
        edits: List[Tuple[int, int, str]] = []  # (xml start, xml end, new escaped text)
        for segment in _segments(xml):
            spans = self._spans(segment.text, patterns, counters)
            if not spans:
                continue
            new_text = {id(n): list(n.text) for n in segment.nodes}
            for start, end in spans:
                replacement = self._replacement(segment.text[start:end], options)
                first = True
                for node in segment.nodes:
                    lo = max(start, node.offset)
                    hi = min(end, node.offset + len(node.text))
                    if lo >= hi:
                        continue
                    chars = new_text[id(node)]
                    for i in range(lo - node.offset, hi - node.offset):
                        chars[i] = ""
                    if first:  # the pattern goes where the match begins
                        chars[lo - node.offset] = replacement
                        first = False
            for node in segment.nodes:
                text = "".join(new_text[id(node)])
                if text == node.text:
                    continue
                edits.append((node.start, node.end, _escape(text)))
                if (text != text.strip()) and "xml:space" not in xml[node.open_start:node.open_end]:
                    tag_end = xml.index(">", node.open_start)
                    edits.append((tag_end, tag_end, ' xml:space="preserve"'))
        for start, end, value in sorted(edits, reverse=True):
            xml = xml[:start] + value + xml[end:]
        return xml

    def _mask_value(self, value: str, patterns, counters, options) -> str:
        spans = self._spans(value, patterns, counters)
        for start, end in reversed(spans):
            value = value[:start] + self._replacement(value[start:end], options) + value[end:]
        return value

    def _mask_text_nodes(self, xml: str, patterns, counters, options) -> str:
        def repl(match: "re.Match") -> str:
            raw = match.group(1)
            text = html.unescape(raw)
            masked = self._mask_value(text, patterns, counters, options)
            return match.group(0) if masked == text else f">{_escape(masked)}<"

        return _TEXT_NODE.sub(repl, xml)

    def _mask_attributes(self, xml: str, patterns, counters, options) -> str:
        def repl(match: "re.Match") -> str:
            text = html.unescape(match.group(2))
            masked = self._mask_value(text, patterns, counters, options)
            if masked == text:
                return match.group(0)
            return f'{match.group(1)}"{_escape_attr(masked)}"'

        return _ATTRIBUTE.sub(repl, xml)

    # ------------------------------------------------------------- metadata
    @staticmethod
    def _strip_metadata(name: str, xml: str) -> str:
        if name.endswith("core.xml"):
            def clear(match: "re.Match") -> str:
                tag = match.group(1)
                if tag in _CORE_KEEP:
                    return match.group(0)
                return f"<{tag}{match.group(2) or ''}></{tag}>"

            return re.sub(r"<([\w:]+)(\s[^>]*)?>[^<]*</\1>", clear, xml)
        if name.endswith("app.xml"):
            for tag in _APP_CLEAR:
                xml = re.sub(rf"<{tag}>[^<]*</{tag}>", f"<{tag}></{tag}>", xml)
            return re.sub(r"(<vt:lpstr>)[^<]*(</vt:lpstr>)", r"\1\2", xml)
        if name.endswith("custom.xml"):
            return re.sub(r"(<Properties\b[^>]*?)(?:/>|>.*</Properties>)", r"\1/>", xml, flags=re.S)
        return xml

    @staticmethod
    def _anonymise_authors(xml: str) -> str:
        """Revision, comment and people.xml identities are personal data."""
        xml = re.sub(r'(\s(?:w|w15):author=)"[^"]*"', r'\1"Author"', xml)
        xml = re.sub(r'\s(?:w:initials|w15:userId|w15:providerId)="[^"]*"', "", xml)
        return xml

    @staticmethod
    def _drop_thumbnail_references(out: Dict[str, Optional[bytes]]) -> None:
        rels = out.get("_rels/.rels")
        if rels is None:
            return
        text = rels.decode("utf-8", errors="surrogateescape")
        text = re.sub(r"<Relationship\b[^>]*Target=\"[^\"]*thumbnail\.\w+\"[^>]*/>", "", text)
        out["_rels/.rels"] = text.encode("utf-8", errors="surrogateescape")

    @staticmethod
    def _stamp(xml: str, options: RedactionOptions) -> str:
        """Visible notice at the top of the body (Word has no stamp layer)."""
        text = options.watermark or ""
        if options.watermark_footer:
            text = f"{text} — {date.today():%Y-%m-%d} — working copy, not the original"
        paragraph = (
            '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/>'
            f'<w:color w:val="{_WATERMARK_COLOR}"/></w:rPr>'
            f'<w:t xml:space="preserve">{_escape(text)}</w:t></w:r></w:p>'
        )
        match = re.search(r"<w:body[^>]*>", xml)
        if not match:
            return xml
        return xml[: match.end()] + paragraph + xml[match.end():]

    @staticmethod
    def _warn_unanalysed(names: Sequence[str], report: FileReport) -> None:
        media = [n for n in names if n.startswith("word/media/")]
        embedded = [n for n in names if n.startswith("word/embeddings/")]
        if media:
            report.warnings.append(
                f"{len(media)} picture(s) not analysed — text inside pictures is "
                "left as is, check them manually"
            )
        if embedded:
            report.warnings.append(
                f"{len(embedded)} embedded object(s) not analysed — check manually"
            )

    # ---------------------------------------------------------------- output
    @staticmethod
    def _write(out: Dict[str, Optional[bytes]], order: Sequence[str], output: Path) -> None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputError(
                f"cannot create output directory {output.parent} ({exc})",
                hint="check write permissions",
            ) from exc
        tmp = output.with_name(f".{output.name}.tmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in order:  # [Content_Types].xml stays first
                    data = out.get(name)
                    if data is not None:
                        archive.writestr(name, data)
            os.replace(tmp, output)
        except OSError as exc:
            raise OutputError(
                f"cannot write to {output} ({exc})",
                hint="disk full, file open elsewhere, or insufficient permissions?",
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)

    # ---------------------------------------------------------- verification
    def _verify(self, output: Path, terms: Sequence[str], report: FileReport, options) -> None:
        try:
            blob = self._extractable_text(output)
        except Exception as exc:
            report.warnings.append(f"final check impossible ({exc}) — read the produced document")
            return
        residual = find_residual(blob, terms, options.whole_word)
        report.residual = residual
        report.verified = not residual
        if residual:
            report.add_error(
                f"{len(residual)} term(s) still extractable after redaction",
                hint="flag this document: it must not be distributed as is",
            )

    def _extractable_text(self, path: Path) -> str:
        """Every place redaction scrubs: text nodes AND attribute values, all parts."""
        chunks: List[str] = []
        for name, data in self._open(path).items():
            if not _ANY_XML.match(name):
                continue
            xml = self._decode(data)
            if _WORD_PART.match(name):
                chunks.extend(s.text for s in _segments(xml))
            chunks.append(_plain_text(xml))
            chunks.extend(html.unescape(m.group(2)) for m in _ATTRIBUTE.finditer(xml))
        return "\n".join(chunks)
