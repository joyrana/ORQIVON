"""Import a Confluence page as a source document.

The page's storage format (XHTML) is converted to Markdown-like text, so headings, lists and
table rows become located blocks like any uploaded document. Re-importing a changed page adds a
new document version (and requirement re-extraction marks changed requirements), an unchanged
page is reported as such.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from agentic_sdlc.integrations.worktools import WorkToolConnection


class _ToText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}
    SKIP = {"script", "style", "ac:parameter", "ac:plain-text-link-body", "ri:attachment"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.cur: list[str] = []
        self.prefix = ""
        self.cells: list[str] | None = None
        self.header: list[str] = []
        self.row_is_header = False
        self.skip = 0
        self.lists: list[str] = []

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        if text:
            self.lines.append(self.prefix + text)
        self.cur, self.prefix = [], ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in ("ul", "ol"):
            self.lists.append(tag)
        if tag == "table":
            self.header = []
        if tag in ("td", "th") and self.cells is not None:
            self.cur = []
            self.row_is_header = self.row_is_header or tag == "th"
            return
        if tag == "tr":
            self._flush()
            self.cells, self.row_is_header = [], False
            return
        if self.cells is not None:
            if tag in self.BLOCK:
                self.cur.append(" ")  # paragraphs inside a cell stay in the cell
            return
        if tag in self.BLOCK:
            self._flush()
            if tag.startswith("h") and tag[1:].isdigit():
                self.prefix = "#" * int(tag[1]) + " "
            elif tag == "li":
                self.prefix = "1. " if self.lists and self.lists[-1] == "ol" else "- "

    def _flush_cell(self) -> None:
        if self.cells is not None:
            self.cells.append(re.sub(r"\s+", " ", "".join(self.cur)).strip())
            self.cur = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
        if tag in ("td", "th") and self.cells is not None:
            self._flush_cell()
            return
        if tag == "tr" and self.cells is not None:
            cells = [c.replace("|", "/") for c in self.cells]
            if self.row_is_header and not self.header:
                self.header = cells
                self.lines += ["| " + " | ".join(cells) + " |", "|" + "---|" * len(cells)]
            elif any(cells):
                if not self.header:  # a table without a header row: give it one so it parses
                    self.header = [f"Column {i + 1}" for i in range(len(cells))]
                    self.lines += ["| " + " | ".join(self.header) + " |", "|" + "---|" * len(cells)]
                self.lines.append("| " + " | ".join(cells) + " |")
            self.cells, self.cur = None, []
            return
        if self.cells is not None:
            return
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.cur.append(data)


def storage_to_text(title: str, xhtml: str) -> str:
    p = _ToText()
    p.feed(xhtml)
    p._flush()
    return "\n".join([f"# {unescape(title)}", *p.lines]) + "\n"


def fetch_page(conn: WorkToolConnection, page_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{1,20}", page_id):
        raise ValueError("page id must be numeric")
    data = conn.request("GET", f"/rest/api/content/{page_id}?expand=body.storage,version")
    links = data.get("_links") or {}
    return {
        "id": str(data["id"]),
        "title": str(data["title"]),
        "version": int((data.get("version") or {}).get("number", 0)),
        "text": storage_to_text(
            str(data["title"]), str(((data.get("body") or {}).get("storage") or {}).get("value", ""))
        ),
        "url": f"{conn.base_url}{links.get('webui', '')}" if links.get("webui") else conn.base_url,
    }
