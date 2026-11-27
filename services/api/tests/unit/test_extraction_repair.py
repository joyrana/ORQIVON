"""Section-by-section llm extraction: repair, validation, retries and merge (no database, no model)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from agentic_sdlc.requirements import llm_extraction as lx
from agentic_sdlc.skills.registry import get_skill_registry

SCHEMA = get_skill_registry().get("brd-analysis").output_schema

# Shaped like the prime-brokerage BRD that failed in development: a requirements table whose IDs are
# BR-xxx (business *requirements*), then a non-functional list.
BLOCKS = [
    {"block_id": "b0001", "kind": "paragraph", "text": "3. Key Business Requirements"},
    *(
        {
            "block_id": f"b{i + 1:04d}",
            "kind": "table_row",
            "text": f"ID: BR-{i:03d} | Requirement: The platform shall support capability {i}. | Priority: "
            "Must",
        }
        for i in range(1, 9)
    ),
    {"block_id": "b0100", "kind": "paragraph", "text": "6. Non-Functional Requirements"},
    {
        "block_id": "b0101",
        "kind": "list_item",
        "text": "Performance: Critical updates should be processed with low latency.",
    },
]


def _req(key: str, block: str, quote: str, **kw: Any) -> dict[str, Any]:
    return {
        "key": key,
        "kind": "functional",
        "title": "t",
        "statement": "s",
        "priority": "must",
        "origin": "explicit",
        "block_id": block,
        "quote": quote,
        "confidence": 0.9,
        "acceptance_criteria": [{"text": "c", "origin": "inferred", "block_id": None, "quote": None}],
        **kw,
    }


def _table_req(i: int, key: str | None = None, **kw: Any) -> dict[str, Any]:
    fields = {"block_id": f"b{i + 1:04d}", "quote": f"The platform shall support capability {i}.", **kw}
    return _req(key or f"BR-{i:03d}", fields.pop("block_id"), fields.pop("quote"), **fields)


def _rule(i: int, key: str | None = None) -> dict[str, Any]:
    return {
        "key": key or f"BR-{i:03d}",
        "statement": "s",
        "block_id": f"b{i + 1:04d}",
        "quote": f"The platform shall support capability {i}.",
        "origin": "explicit",
    }


def _out(reqs: list[Any], rules: list[Any] | None = None, qs: list[Any] | None = None) -> dict[str, Any]:
    return {"requirements": reqs, "business_rules": rules or [], "questions": qs or []}


# ---------------------------------------------------------------- repair


def test_requirement_also_listed_as_rule_is_kept_once_as_requirement() -> None:
    reqs = [_table_req(i, key=f"REQ-{i:03d}") for i in range(1, 9)]  # invented keys
    rules = [_rule(i) for i in range(1, 9)]  # the same rows again, as "business rules"
    out = lx.repair(_out(reqs, rules), BLOCKS, notes := [])
    assert [r["key"] for r in out["requirements"]] == [f"BR-{i:03d}" for i in range(1, 9)]
    assert out["business_rules"] == []
    assert any("document gives" in n for n in notes) and any("kept once" in n for n in notes)


def test_one_unverifiable_item_is_dropped_not_fatal() -> None:
    reqs = [_table_req(i) for i in range(1, 9)]
    reqs[3]["quote"] = "Positions are consolidated across custodians."  # paraphrase
    out = lx.repair(_out(reqs), BLOCKS, notes := [])
    assert [r["key"] for r in out["requirements"]] == [
        "BR-001",
        "BR-002",
        "BR-003",
        *[f"BR-{i:03d}" for i in range(5, 9)],
    ]
    assert any("BR-004" in n for n in notes)


def test_mostly_fabricated_output_is_left_for_validation_to_reject() -> None:
    out = lx.repair(_out([_table_req(1, quote="The platform shall export to PDF.")]), BLOCKS, [])
    assert lx.quote_problems(out, {b["block_id"]: lx.norm(b["text"]) for b in BLOCKS})


def test_quote_decoration_and_wrong_block_are_fixed_exactly() -> None:
    reqs = [
        _table_req(1, quote='"The platform shall support capability 1."'),
        _table_req(2, block_id="b0050"),  # quote is real, block is wrong
    ]
    out = lx.repair(_out(reqs), BLOCKS, [])
    assert [(r["block_id"], r["quote"]) for r in out["requirements"]] == [
        ("b0002", "The platform shall support capability 1."),
        ("b0003", "The platform shall support capability 2."),
    ]


def test_enum_synonyms_and_paraphrased_criteria() -> None:
    req = _table_req(
        1,
        priority="High",
        kind="Non-Functional",
        acceptance_criteria=[
            {"text": "c", "origin": "Explicit", "block_id": "b0002", "quote": "supports capability one"},
            {"text": "c", "origin": "explicit", "block_id": "b0002", "quote": "support capability 1"},
        ],
    )
    r = lx.repair(_out([req]), BLOCKS, [])["requirements"][0]
    assert (r["priority"], r["kind"]) == ("must", "non_functional")
    assert [c["origin"] for c in r["acceptance_criteria"]] == ["inferred", "explicit"]


def test_question_with_unverifiable_citation_keeps_its_text() -> None:
    q = {"kind": "ambiguity", "question": "What is low?", "block_id": "b0101", "quote": "very low latency"}
    out = lx.repair(_out([_table_req(1)], qs=[q]), BLOCKS, [])
    assert out["questions"][0]["question"] == "What is low?" and out["questions"][0]["block_id"] is None


def test_wrong_shape_is_left_for_the_schema_check() -> None:
    bad = {"requirements": "wrong type", "business_rules": [], "questions": []}
    assert lx.repair(copy.deepcopy(bad), BLOCKS, []) == bad


def test_unquoted_question_cites_its_block() -> None:
    q = {"kind": "ambiguity", "question": "How low?", "block_id": "b0101", "quote": None, "blocking": True}
    out = lx.repair(_out([_table_req(1)], qs=[q]), BLOCKS, [])
    assert out["questions"][0]["quote"] == BLOCKS[-1]["text"]
    assert lx.quote_problems(out, {b["block_id"]: lx.norm(b["text"]) for b in BLOCKS}) == []


def test_unquoted_invented_rule_does_not_pass_as_cited() -> None:
    # Seen with the local model: a rule absent from the document, pointing at a real block, no quote.
    invented = {
        "key": "RUL-001",
        "statement": "Margin calls must be triggered automatically below 100% maintenance margin.",
        "block_id": "b0002",
        "quote": None,
        "origin": "explicit",
    }
    reqs = [_table_req(i) for i in range(1, 9)]
    out = lx.repair(_out(reqs, [invented]), BLOCKS, notes := [])
    assert out["business_rules"] == [] and any("RUL-001" in n for n in notes)


def test_model_questions_never_block_and_uncited_injection_warnings_are_dropped() -> None:
    qs = [
        {
            "kind": "ambiguity",
            "question": "How low?",
            "block_id": "b0101",
            "quote": "low latency",
            "blocking": True,
        },
        {"kind": "contradiction", "question": "Which?", "block_id": "b0101", "quote": None, "blocking": True},
        {
            "kind": "injection_warning",
            "question": "No instructions found.",
            "block_id": None,
            "blocking": False,
        },
    ]
    out = lx.repair(_out([_table_req(1)], qs=qs), BLOCKS, [])
    assert [(q["kind"], q["blocking"]) for q in out["questions"]] == [
        ("ambiguity", False),
        ("contradiction", False),
    ]


def test_merge_adds_deterministic_contradictions_as_blocking() -> None:
    blocks = [
        {"block_id": "b0001", "kind": "list_item", "text": "Sessions time out after at most 15 minutes."},
        {
            "block_id": "b0002",
            "kind": "list_item",
            "text": "A session times out after no more than 3600 seconds.",
        },
    ]
    reqs = [
        _req("NFR-001", "b0001", "Sessions time out after at most 15 minutes.", statement=blocks[0]["text"]),
        _req(
            "NFR-002",
            "b0002",
            "A session times out after no more than 3600 seconds.",
            statement=blocks[1]["text"],
        ),
    ]
    out = lx.merge([_out(reqs)], blocks, [])
    assert [(q["kind"], q["blocking"]) for q in out["questions"]] == [("contradiction", True)]


def test_statement_claiming_more_than_its_quote_is_dropped() -> None:
    # Seen with the local model: a real quote from the scope table, with an invented rule and threshold.
    blocks = [
        *BLOCKS,
        {
            "block_id": "b0300",
            "kind": "table_row",
            "text": "Business Capability: Margin & Financing | Description: Initial/maintenance margin "
            "calculation, leverage financing, interest accrual and margin calls.",
        },
    ]
    real_quote = (
        "Initial/maintenance margin calculation, leverage financing, interest accrual and margin calls."
    )
    invented = {
        "key": "RUL-001",
        "statement": "Margin calls must be triggered automatically when maintenance margin falls below 100%.",
        "block_id": "b0300",
        "quote": real_quote,
        "origin": "explicit",
    }
    faithful = _req(
        "REQ-001",
        "b0300",
        real_quote,
        statement="Calculate initial and maintenance margin, "
        "leverage financing, interest accrual and margin calls.",
    )
    out = lx.repair(_out([_table_req(1), faithful], [invented]), blocks, notes := [])
    assert out["business_rules"] == [] and [r["key"] for r in out["requirements"]] == ["BR-001", "REQ-001"]
    assert any("stating more than their cited text: RUL-001" in n for n in notes)


# ---------------------------------------------------------------- sections and merge


def _doc(n_sections: int, rows_per_section: int = 12) -> list[dict[str, Any]]:
    blocks, i = [], 0
    for s in range(1, n_sections + 1):
        i += 1
        blocks.append({"block_id": f"b{i:04d}", "kind": "paragraph", "text": f"{s}. Section {s}"})
        for _ in range(rows_per_section):
            i += 1
            blocks.append({"block_id": f"b{i:04d}", "kind": "table_row", "text": "x" * 300})
    return blocks


def test_sections_cut_at_headings_and_cover_every_block_once() -> None:
    blocks = _doc(4)
    secs = lx.sections(blocks)
    assert len(secs) == 4
    assert [b["block_id"] for s in secs for b in s.blocks] == [b["block_id"] for b in blocks]
    assert all(s.blocks[0]["text"].endswith(f"Section {s.index}") for s in secs)
    assert lx.sections(BLOCKS)[0].total == 1  # a short document is one request


def test_oversized_section_is_split_between_blocks() -> None:
    secs = lx.sections(_doc(1, rows_per_section=40))
    assert len(secs) > 1 and all(lx._size(s.blocks) <= lx.MAX_SECTION_CHARS for s in secs)


def test_merge_keeps_document_ids_and_renumbers_invented_keys() -> None:
    blocks = [*BLOCKS, {"block_id": "b0200", "kind": "paragraph", "text": "Users can export reports."}]
    part1 = _out([_table_req(1), _req("REQ-001", "b0101", "processed with low latency")])
    part2 = _out(
        [_table_req(1), _req("REQ-001", "b0200", "Users can export reports.")],  # BR-001 cited twice
        [_rule(2)],
        [{"kind": "ambiguity", "question": "What is low latency?", "blocking": False}],
    )
    rule = {"key": "BR-001", "statement": "s", "block_id": "b0200", "quote": "export", "origin": "inferred"}
    part3 = _out(
        [_table_req(2)],
        [rule],
        [{"kind": "ambiguity", "question": "what is LOW latency?", "blocking": False}],
    )
    out = lx.merge([part1, part2, part3], blocks, notes := [])
    assert [r["key"] for r in out["requirements"]] == ["BR-001", "REQ-001", "REQ-002", "BR-002"]
    # BR-002 is a requirement in part 3; the invented "BR-001" rule can't pose as the document's BR-001.
    assert [b["key"] for b in out["business_rules"]] == ["RUL-001"]
    assert len(out["questions"]) == 1
    assert lx.duplicate_keys(out) == [] and notes


def test_invented_keys_never_take_a_document_prefix() -> None:
    reqs = [
        _req("BR-011", "b0101", "processed with low latency", kind="non_functional"),
        _req("BR-011", "b0100", "Non-Functional Requirements"),  # same invented key, different item
    ]
    out = lx.repair(_out(reqs), BLOCKS, [])
    assert [r["key"] for r in out["requirements"]] == ["NFR-001", "REQ-001"]


# ---------------------------------------------------------------- driver


class FakeModel:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.users: list[str] = []

    def __call__(self, system: str, user: str, attempt: int, max_output_tokens: int) -> dict[str, Any]:
        assert max_output_tokens >= lx.MIN_OUTPUT_TOKENS
        self.users.append(user)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return json.loads(json.dumps(a))


def _extract(model: FakeModel, blocks: list[dict[str, Any]] = BLOCKS, **kw: Any) -> dict[str, Any]:
    return lx.extract(
        blocks,
        document_kind="brd",
        title="t",
        schema=SCHEMA,
        generate=model,
        max_attempts=3,
        warnings=[],
        **kw,
    )


def test_failed_section_is_retried_with_its_errors() -> None:
    good = _out([_table_req(1)])
    model = FakeModel(_out([_table_req(1, quote="The platform shall export to PDF.")]), good)
    assert _extract(model)["requirements"][0]["key"] == "BR-001"
    assert "previous answer was rejected" not in model.users[0]
    assert "quote not found in b0002" in model.users[1]


def test_identical_errors_stop_early() -> None:
    bad = _out([_table_req(1, quote="The platform shall export to PDF.")])
    model = FakeModel(bad, bad, bad)
    with pytest.raises(lx.ExtractionFailed, match="after 2 attempt"):
        _extract(model)


def test_transient_error_is_retried_without_blaming_the_model() -> None:
    model = FakeModel(
        lx.SectionOutputInvalid("model error: timed out", feedback=False), _out([_table_req(1)])
    )
    _extract(model)
    assert "previous answer was rejected" not in model.users[1]


def test_each_section_is_a_separate_request() -> None:
    blocks = _doc(3)
    model = FakeModel(_out([]), _out([]), _out([]))
    seen: list[int] = []
    _extract(model, blocks, before_section=lambda s: seen.append(s.index))
    assert seen == [1, 2, 3] and all("part" in u for u in model.users)
    assert "[b0001]" in model.users[0] and "[b0001]" not in model.users[1]
