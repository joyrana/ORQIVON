"""PDF ingestion: layout → located blocks, safe handling of hostile or unreadable PDFs, and the
same requirements from the sample BRD whether it arrives as DOCX or as PDF."""

from __future__ import annotations

import textwrap

import pytest
from fastapi.testclient import TestClient

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.documents.normalize import normalize_docx
from agentic_sdlc.documents.pdf import normalize_pdf
from agentic_sdlc.documents.validation import validate_upload
from agentic_sdlc.workflows.worker import drain
from tests.conftest import H
from tests.helpers import SAMPLE_BRD
from tests.pdf_fixture import make_pdf


def _blocks(pdf: bytes) -> list[tuple[str, str, int]]:
    return [(b["kind"], b["text"], b["page"]) for b in normalize_pdf(pdf)["blocks"]]


def test_layout_becomes_headings_paragraphs_and_lists_with_pages() -> None:
    pdf = make_pdf(
        [
            [
                (20, "Payments Service Requirements", "b"),
                (11, "This document describes the refund flow for the payments"),
                (11, "service and the rules that govern partial refunds."),
                None,
                (15, "3 Functional requirements", "b"),
                (13, "3.1 Refunds", "b"),
                (11, "FR-001: The system shall refund a payment within 30 days of"),
                (11, "capture, and shall reject later refund re-"),
                (11, "quests."),
                (11, "- Refunds are recorded in the ledger"),
                (11, "- Partial refunds keep the remaining"),
                (11, "balance"),
            ],
            [(11, "NFR-001: Refund requests must complete within two seconds.")],
        ]
    )
    blocks = normalize_pdf(pdf)["blocks"]
    got = [(b["kind"], b["text"], b["page"]) for b in blocks]
    assert got == [
        ("heading", "Payments Service Requirements", 1),
        (
            "paragraph",
            "This document describes the refund flow for the payments service and the rules that govern "
            "partial refunds.",
            1,
        ),
        ("heading", "3 Functional requirements", 1),
        ("heading", "3.1 Refunds", 1),
        (
            "paragraph",
            "FR-001: The system shall refund a payment within 30 days of capture, and shall reject later "
            "refund requests.",
            1,
        ),
        ("list_item", "Refunds are recorded in the ledger", 1),
        ("list_item", "Partial refunds keep the remaining balance", 1),
        ("paragraph", "NFR-001: Refund requests must complete within two seconds.", 2),
    ]
    by_text = {b["text"]: b for b in blocks}
    assert by_text["3.1 Refunds"]["level"] == 2  # from the section number
    assert by_text["3.1 Refunds"]["section_path"] == ["3 Functional requirements"]
    assert blocks[4]["section_path"] == ["3 Functional requirements", "3.1 Refunds"]


def test_running_headers_footers_and_page_numbers_are_dropped() -> None:
    pages = [
        [
            (9, "ACME Corp - Confidential"),
            (11, f"Body text on page {n} explains requirement {n}."),
            (9, f"Page {n} of 4"),
        ]
        for n in range(1, 5)
    ]
    texts = [t for _, t, _ in _blocks(make_pdf(pages))]
    assert texts == [f"Body text on page {n} explains requirement {n}." for n in range(1, 5)]


def test_scanned_pdf_is_flagged_not_silently_empty() -> None:
    out = normalize_pdf(make_pdf([[], []]))
    assert out["blocks"] == []
    assert any("OCR is not supported" in w for w in out["warnings"])


def test_hostile_and_invalid_pdfs() -> None:
    ok = make_pdf([[(11, "hello")]])
    assert validate_upload("brd.pdf", ok).format == "pdf"
    scripted = make_pdf(
        [[(11, "hello")]], extra_catalog="/OpenAction << /S /JavaScript /JS (app.alert(1)) >> "
    )
    assert any("scripts or actions" in w for w in validate_upload("brd.pdf", scripted).warnings)
    with pytest.raises(ValidationFailed, match="encrypted"):
        validate_upload("brd.pdf", make_pdf([[(11, "x")]], encrypt=True))
    with pytest.raises(ValidationFailed, match="not a valid PDF"):
        validate_upload("brd.pdf", b"%PDF-1.7\nthis is not a pdf")
    with pytest.raises(ValidationFailed, match="not a valid PDF"):
        validate_upload("notes.txt", ok)  # PDF bytes under another name
    with pytest.raises(ValidationFailed, match="not a valid PDF"):
        validate_upload("brd.pdf", b"plain text pretending")
    with pytest.raises(ValidationFailed, match="too many pages"):
        validate_upload("big.pdf", make_pdf([[(11, "p")]] * 501))


def _brd_as_pdf() -> bytes:
    """The sample BRD laid out as a PDF author would: headings larger, body wrapped to the page."""
    lines: list = []
    for b in normalize_docx(SAMPLE_BRD.read_bytes())["blocks"]:
        if b["kind"] == "heading":
            lines += [None, (22 - 3 * min(b["level"] or 0, 3), b["text"], "b")]
            continue
        text = b["text"]
        if b["kind"] == "list_item":
            text = "- " + text
        if b["kind"] == "table_header":
            continue
        if b["kind"] == "table_row":
            # A PDF table reads as its cells' text; keep the requirement sentence intact.
            cells = dict(c.split(": ", 1) for c in text.split(" | ") if ": " in c)
            text = f"{cells['ID']}: {cells['Requirement']}"
        lines += [None, *[(11, part) for part in textwrap.wrap(text, 85)]]
    return make_pdf([lines[i : i + 40] for i in range(0, len(lines), 40)])


def test_sample_brd_as_pdf_yields_the_same_requirements(client: TestClient, project: dict) -> None:
    pid = project["id"]
    r = client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("task-title-validation-brd.pdf", _brd_as_pdf(), "application/pdf")},
        data={"kind": "brd"},
        headers=H("owner"),
    )
    assert r.status_code == 202, r.text
    drain()
    doc = client.get(
        f"/api/v1/projects/{pid}/documents/{r.json()['document']['id']}", headers=H("owner")
    ).json()
    v = doc["versions"][0]
    assert v["status"] == "processed", v
    assert sorted(v["extraction_summary"]["created"]) == ["FR-001", "FR-002", "FR-003", "NFR-001", "NFR-002"]
    reqs = client.get(f"/api/v1/projects/{pid}/requirements", headers=H("reviewer")).json()
    fr1 = next(x for x in reqs if x["key"] == "FR-001")
    assert fr1["locator"]["section_path"][-1] == "3 Functional requirements"
    assert "120 characters" in fr1["source_text"]
    assert fr1["locator"].get("page") == 1
