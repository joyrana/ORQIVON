"""The evaluation harness itself: datasets load, suites meet baselines, regressions are caught."""

from __future__ import annotations

import json
from typing import Any

from agentic_sdlc.evals.run import EVALS_DIR, check_baseline, main
from agentic_sdlc.evals.suites import SUITES, requirements_suite
from agentic_sdlc.requirements.rules import extract


def _data(name: str) -> dict[str, Any]:
    return json.loads((EVALS_DIR / name / "cases.json").read_text())


def test_every_suite_meets_its_baseline() -> None:
    baselines = json.loads((EVALS_DIR / "baselines.json").read_text())
    for name, suite in SUITES.items():
        result = suite(_data(name))
        assert result.results, name
        assert check_baseline(result, baselines[name]) == [], name


def test_a_regressed_extractor_is_caught() -> None:
    def no_injection_checks(blocks: list[dict[str, Any]], *, document_kind: str) -> dict[str, Any]:
        out = extract(blocks, document_kind=document_kind)
        out["questions"] = [q for q in out["questions"] if q["kind"] != "injection_warning"]
        return out

    baselines = json.loads((EVALS_DIR / "baselines.json").read_text())
    result = requirements_suite(_data("requirements"), extractor=no_injection_checks)
    failures = check_baseline(result, baselines["requirements"])
    assert any("injection_recall" in f for f in failures)


def test_cli_reports_and_exit_status(tmp_path: Any) -> None:
    report = tmp_path / "report.json"
    assert main(["--all", "--report", str(report)]) == 0
    body = json.loads(report.read_text())
    assert {s["suite"] for s in body["suites"]} == set(SUITES)
    assert body["config"]["strategy"] == "heuristic"
    # Criteria with no offline measurement are reported as such, never as met.
    unmeasured = [c for c in body["skill_criteria"] if c["measured"] is None]
    assert unmeasured and all(c["status"] == "not measured offline" for c in unmeasured)


def test_cli_fails_when_a_baseline_is_raised(tmp_path: Any) -> None:
    for name in SUITES:
        (tmp_path / name).mkdir()
        (tmp_path / name / "cases.json").write_text((EVALS_DIR / name / "cases.json").read_text())
    (tmp_path / "baselines.json").write_text(
        json.dumps({"requirements": {"contradiction_recall": {"min": 1.0}}})
    )
    assert main(["--suite", "requirements", "--evals-dir", str(tmp_path)]) == 1


def test_recorded_runs_are_listed(client: Any) -> None:
    from tests.conftest import H

    assert main(["--suite", "redaction", "--record"]) == 0
    rows = client.get("/api/v1/evaluations", headers=H("auditor")).json()
    assert rows[0]["suite"] == "redaction" and rows[0]["passed"] is True
    assert rows[0]["config"]["strategy"] == "heuristic" and rows[0]["failing_cases"] == []
    assert client.get("/api/v1/evaluations").status_code == 401


def test_model_backed_extraction_is_scored_over_http_and_fabrication_scores_zero() -> None:
    """`--strategy llm`: a model that returns the rule-based answer scores like it; a model that
    invents a quote gets nothing for that case."""
    from agentic_sdlc.documents.normalize import normalize_text
    from agentic_sdlc.evals.model_extractor import ModelExtractor
    from agentic_sdlc.models.endpoints import parse_endpoints
    from agentic_sdlc.models.registry import ModelRegistry, default_providers
    from agentic_sdlc.models.types import ModelEntry
    from tests.model_server import FakeOpenAI, serve, user_prompt

    mode = {"fabricate": False}

    # The scripted "model" answers each document with what the rule-based extractor finds in it.
    answers: dict[str, dict[str, Any]] = {}
    for case in _data("requirements")["cases"]:
        blocks = normalize_text(case["text"].encode(), kind=case["kind"])["blocks"]
        payload = "\n".join(
            f"[{b['block_id']}] ({' > '.join(b.get('section_path', [])) or 'root'}) {b['text']}"
            for b in blocks
        )
        answers[payload] = extract(blocks, document_kind=case["kind"])

    def handler(body: dict[str, Any]) -> dict[str, Any]:
        doc = user_prompt(body).split("<document>\n", 1)[1].rsplit("\n</document>", 1)[0]
        out = json.loads(json.dumps(answers[doc]))
        if mode["fabricate"] and out["requirements"]:
            out["requirements"][0]["quote"] = "words that are not in the document"
        return out

    with serve(FakeOpenAI(models=["lab/extractor"], handler=handler)) as fake:
        eps = parse_endpoints([{"name": "lab", "base_url": fake.url}], None)
        entry = ModelEntry(
            model_id="lab-extractor",
            provider="openai_compatible",
            endpoint="lab",
            served_model_name="lab/extractor",
            revision="1",
            family="lab",
            license="Apache-2.0",
            tokenizer="t",
            context_window=32768,
            max_output_tokens=8192,
            tasks=["extraction"],
            capabilities=["structured_output"],
            privacy="self_hosted",
        )
        reg = ModelRegistry([entry], default_providers(eps), eps)
        data = _data("requirements")
        rules_result = requirements_suite(data)
        model = ModelExtractor(reg, "lab-extractor")
        model_result = requirements_suite(data, extractor=model)
        assert model_result.metrics == rules_result.metrics
        summary = model.summary()
        assert summary["calls"] == len(data["cases"]) and summary["invalid_outputs"] == 0
        assert summary["usage_estimated"] is False and summary["input_tokens"] > 0

        mode["fabricate"] = True
        cheat = ModelExtractor(reg, "lab-extractor")
        cheat_result = requirements_suite(data, extractor=cheat)
        assert cheat.invalid > 0
        assert cheat_result.metrics["statement_recall"] < rules_result.metrics["statement_recall"]
