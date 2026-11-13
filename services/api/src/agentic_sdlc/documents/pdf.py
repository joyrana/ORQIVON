"""PDF text extraction into located blocks.

PDFs have no paragraphs or headings, only positioned text. Structure is recovered from layout:

* lines are fragments on the same baseline;
* headings are short lines set larger than the body text (or bold), and their level is the rank
  of their size, or the depth of a section number ("3.2 Task titles" is level 2);
* paragraphs are consecutive body lines without a vertical gap, with hyphenation undone;
* list items start with a bullet or "1." / "a)";
* running headers, footers and page numbers (the same text on most pages) are dropped.

Every block records its page, so a requirement's locator reads "page 4, 3 Functional requirements".
Scanned PDFs (images, no text layer) produce no blocks and a warning: there is no OCR.
"""

from __future__ import annotations

import io
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pypdf

from agentic_sdlc.documents.normalize import _block

BULLET = re.compile(r"^(?:[-*•▪◦\u2013]|\(?\d{1,3}[.)]|\(?[a-z][.)])\s+")
SECTION_NO = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+\S")
PAGE_NO = re.compile(r"^(?:page\s+)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?$", re.I)


@dataclass
class Line:
    page: int
    y: float
    size: float
    bold: bool
    text: str


def _lines(reader: pypdf.PdfReader) -> list[Line]:
    out: list[Line] = []
    for pno, page in enumerate(reader.pages, start=1):
        frags: list[tuple[float, float, bool, str]] = []

        def visit(text: str, cm: Any, tm: Any, font: Any, size: float, _f: list = frags) -> None:
            if not text or not text.strip():
                return
            scale = math.hypot(tm[2], tm[3]) * math.hypot(cm[2], cm[3]) or 1.0
            y = tm[5] * cm[3] + cm[5]
            name = str((font or {}).get("/BaseFont", ""))
            for part in text.split("\n"):
                if part.strip():
                    _f.append((y, size * scale, "Bold" in name or "Black" in name, part))

        page.extract_text(visitor_text=visit)
        current: Line | None = None
        for y, size, bold, text in frags:
            if current is not None and abs(current.y - y) < max(2.0, size * 0.3):
                current.text += ("" if current.text.endswith(" ") or text.startswith(" ") else " ") + text
                current.size = max(current.size, size)
                current.bold = current.bold and bold
                continue
            if current is not None:
                out.append(current)
            current = Line(pno, y, round(size, 1), bold, text)
        if current is not None:
            out.append(current)
    for ln in out:
        ln.text = re.sub(r"\s+", " ", ln.text).strip()
    return [ln for ln in out if ln.text]


def _key(text: str) -> str:
    """Running text matches exactly; short labels ("Page 3 of 9", "v2.1 - 12") also with digits varying."""
    t = text.lower()
    return re.sub(r"\d+", "#", t) if len(t) <= 30 else t


def _edges(ids: list[int]) -> list[int]:
    n = 2 if len(ids) >= 6 else 1
    return ids if len(ids) <= 2 * n else ids[:n] + ids[-n:]


def _running(lines: list[Line], pages: int) -> set[str]:
    """Text at the top or bottom of most pages (headers/footers), with digits normalized."""
    if pages < 3:
        return set()
    by_page: dict[int, list[int]] = {}
    for i, ln in enumerate(lines):
        by_page.setdefault(ln.page, []).append(i)
    seen: Counter[str] = Counter()
    for ids in by_page.values():
        for key in {_key(lines[i].text) for i in _edges(ids)}:
            seen[key] += 1
    return {k for k, n in seen.items() if n >= max(3, pages * 0.6)}


def normalize_pdf(data: bytes) -> dict[str, Any]:
    reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
    pages = len(reader.pages)
    lines = _lines(reader)
    warnings: list[str] = []
    running = _running(lines, pages)
    edge_ids = set()
    by_page: dict[int, list[int]] = {}
    for i, ln in enumerate(lines):
        by_page.setdefault(ln.page, []).append(i)
    for ids in by_page.values():
        edge_ids.update(_edges(ids))
    lines = [
        ln
        for i, ln in enumerate(lines)
        if not (i in edge_ids and (PAGE_NO.match(ln.text) or re.sub(r"\d+", "#", ln.text.lower()) in running))
    ]
    chars: Counter[float] = Counter()
    for ln in lines:
        chars[ln.size] += len(ln.text)
    body = chars.most_common(1)[0][0] if chars else 11.0

    def heading(ln: Line) -> bool:
        short = len(ln.text) <= 120 and not ln.text.endswith((".", ",", ";", ":"))
        return short and (ln.size >= body * 1.15 or (ln.bold and len(ln.text) <= 80))

    heading_sizes = sorted({ln.size for ln in lines if heading(ln)}, reverse=True)
    blocks: list[dict[str, Any]] = []
    path: list[str] = []
    para: list[str] = []
    para_page = 0
    last: Line | None = None

    def flush() -> None:
        nonlocal para
        if para:
            text = ""
            for part in para:
                if text.endswith("-") and part[:1].islower():
                    text = text[:-1] + part
                else:
                    text = f"{text} {part}" if text else part
            _block(blocks, "paragraph", text, path, page=para_page)
        para = []

    for ln in lines:
        if heading(ln):
            flush()
            m = SECTION_NO.match(ln.text)
            level = m.group(1).count(".") + 1 if m else heading_sizes.index(ln.size) + 1
            path = [*path[: level - 1], ln.text]
            _block(blocks, "heading", ln.text, path[:-1], level=level, page=ln.page)
        elif BULLET.match(ln.text):
            flush()
            _block(blocks, "list_item", BULLET.sub("", ln.text), path, page=ln.page)
        else:
            gap = last is not None and (
                last.page != ln.page or (last.y - ln.y) > max(ln.size, last.size) * 1.75
            )
            continuing_item = (
                blocks
                and blocks[-1]["kind"] == "list_item"
                and not para
                and not gap
                and last is not None
                and last.page == ln.page
                and BULLET.match(last.text) is not None
            )
            if continuing_item:
                blocks[-1]["text"] += " " + ln.text
            else:
                if gap:
                    flush()
                if not para:
                    para_page = ln.page
                para.append(ln.text)
        last = ln
    flush()
    if pages and not lines:
        warnings.append(
            "No text layer was found: the PDF looks scanned. OCR is not supported; upload a text PDF or DOCX."
        )
    elif pages and len(lines) < pages:
        warnings.append("Some pages have no text layer (scanned images); their content was not read.")
    warnings.append("Tables in PDFs are read as lines of text; check extracted rows against the source.")
    return {
        "format": "pdf",
        "metadata": {"pages": pages, "headings": sum(1 for b in blocks if b["kind"] == "heading")},
        "blocks": blocks,
        "warnings": warnings,
    }
