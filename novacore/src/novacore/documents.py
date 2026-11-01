"""Work order N5: write a new document as plain text, DOCX or PDF (stdlib only).

The writers are small on purpose, so a reviewer can read them whole:

- DOCX: the three parts a WordprocessingML package needs (ECMA-376 Part 1,
  section 11.3.10 main document; Part 2, Open Packaging Conventions: content
  types and the package relationship), zipped with fixed timestamps.
- PDF: PDF 1.4 with the standard Helvetica font (ISO 32000-1, section 9.6.2.2,
  one of the 14 standard fonts, so nothing is embedded), WinAnsi encoding,
  one text line per BT/ET block, a cross-reference table and a trailer. A
  character outside WinAnsi (for example Devanagari) is written as "?":
  Helvetica cannot show it. No creation date, so the same input gives the
  same bytes.

Nothing here decides where a file goes: `create()` writes into the folder it
is given, never outside it, and never over an existing file.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import re
import textwrap
import zipfile
from xml.sax.saxutils import escape

FORMATS = ("pdf", "docx", "txt")
_UNSAFE = re.compile(r"[^A-Za-z0-9 _-]+")
# Names Windows will not open as files, whatever the extension.
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def file_name(title: str) -> str:
    """A safe file name (no extension) from a title: letters, digits, spaces, '-' and '_'."""
    name = " ".join(_UNSAFE.sub(" ", title).split())[:60].strip()
    if not name:
        return "Untitled"
    if name.upper() in _RESERVED:
        return f"NOVA {name}"
    return name


def create(folder: Path, title: str, fmt: str, text: str) -> Path:
    """Write a new document into `folder` and return its path. Never overwrites:
    "name.pdf", then "name (2).pdf", and so on, each created with O_EXCL."""
    data = render(title, fmt, text)
    base = file_name(title)
    for number in range(1, 1000):
        name = f"{base}.{fmt}" if number == 1 else f"{base} ({number}).{fmt}"
        path = folder / name
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return path
    raise FileExistsError(f"999 documents named {base!r} already exist")


def render(title: str, fmt: str, text: str) -> bytes:
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if fmt == "txt":
        return ("\n\n".join([title] + paragraphs) + "\n").encode("utf-8")
    if fmt == "docx":
        return _docx(title, paragraphs)
    if fmt == "pdf":
        return _pdf(title, paragraphs)
    raise ValueError(f"unknown document format {fmt!r}")


# --- DOCX ---------------------------------------------------------------------------

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELATIONSHIPS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _paragraph(text: str, heading: bool = False) -> str:
    style = '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr>' if heading else ""
    return f'<w:p><w:r>{style}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _docx(title: str, paragraphs: list[str]) -> bytes:
    body = _paragraph(title, heading=True) + "".join(_paragraph(p) for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _RELATIONSHIPS),
            ("word/document.xml", document),
        ):
            package.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), content)
    return buffer.getvalue()


# --- PDF ----------------------------------------------------------------------------

_PAGE_WIDTH, _PAGE_HEIGHT, _MARGIN = 612, 792, 72  # US Letter, 1-inch margins
_TITLE_SIZE, _BODY_SIZE, _LEADING = 16, 11, 15
_WRAP = 85  # characters per body line at 11 pt Helvetica


def _pdf_string(text: str) -> bytes:
    encoded = text.encode("cp1252", errors="replace")
    return b"(" + encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _pdf(title: str, paragraphs: list[str]) -> bytes:
    lines: list[tuple[str, int]] = [(title, _TITLE_SIZE), ("", _BODY_SIZE)]
    for paragraph in paragraphs:
        lines += [(line, _BODY_SIZE) for line in textwrap.wrap(paragraph, _WRAP)] + [("", _BODY_SIZE)]
    per_page = (_PAGE_HEIGHT - 2 * _MARGIN) // _LEADING
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]

    streams = []
    for page in pages:
        parts = []
        y = _PAGE_HEIGHT - _MARGIN
        for text, size in page:
            if text:
                parts.append(b"BT /F1 %d Tf %d %d Td " % (size, _MARGIN, y) + _pdf_string(text) + b" Tj ET")
            y -= _LEADING + (size - _BODY_SIZE)
        streams.append(b"\n".join(parts))

    # Objects: 1 catalog, 2 page tree, 3 font, then a page and its content per page.
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, stream in enumerate(streams):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
