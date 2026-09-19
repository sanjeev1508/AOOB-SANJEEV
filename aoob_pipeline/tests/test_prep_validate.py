"""Unit tests for deterministic prep / merge / validate (no LLM)."""

from __future__ import annotations

from pathlib import Path

from aoob_pipeline.merge import merge_packs
from aoob_pipeline.prep import build_prep
from aoob_pipeline.schemas import ExplorePack, Fact, ProveReport
from aoob_pipeline.source_index import SourceIndex
from aoob_pipeline.validate import validate_reports


def test_prep_and_validate_on_fixture(tmp_path: Path) -> None:
    src = tmp_path / "input.c"
    src.write_text(
        """
int buf[4];
int idx;

void helper(int x) {
  idx = x & 3;
}

void alarm_fn(void) {
  if (idx < 4) {
    (void)buf[idx];
  }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    source = SourceIndex(src)
    alarm = {
        "group_id": 1,
        "location": "input.c:10.1-10",
        "enclosing_function": "alarm_fn",
        "variable": "idx",
        "paths": [
            {
                "path_id": 1,
                "call_stack": [
                    "call#helper at input.c:5.1-10",
                    "call#alarm_fn at input.c:9.1-10",
                ],
                "function_sequence": ["helper", "alarm_fn"],
                "source_lines": ["(void)buf[idx];"],
            },
            {
                "path_id": 2,
                "call_stack": [
                    "call#helper at input.c:5.1-10",
                    "call#alarm_fn at input.c:9.1-10",
                ],
                "function_sequence": ["helper", "alarm_fn"],
                "source_lines": ["(void)buf[idx];"],
            },
        ],
        "variable_info": {
            "symbol_name": "buf",
            "role": "flagged_array",
            "kind": "array",
            "index_expression": "idx",
            "declaration_line": 1,
            "declaration_text": "int buf[4];",
            "resolved_sizes": [4],
            "size_known": True,
            "occurrences": [
                {
                    "line": 1,
                    "access": "declaration",
                    "line_text": "int buf[4];",
                    "function": "global",
                },
                {
                    "line": 5,
                    "access": "write",
                    "line_text": "idx = x & 3;",
                    "function": "helper",
                },
                {
                    "line": 9,
                    "access": "read",
                    "line_text": "if (idx < 4) {",
                    "function": "alarm_fn",
                },
            ],
            "used_in_functions": ["helper", "alarm_fn"],
        },
    }
    prep = build_prep(pver_id="t", order="1", alarm=alarm, source=source, path_class_cap=15)
    assert len(prep.path_classes) == 1
    assert prep.coverage_cap == "full"
    assert prep.array_size == 4

    call = ExplorePack(
        agent="CALL_PATH_EXPLORE",
        facts=[
            Fact(kind="call_edge", function="helper", line=5, quote="idx = x & 3;", path_class_id="C1")
        ],
    )
    var = ExplorePack(
        agent="VAR_VALUE_EXPLORE",
        facts=[
            Fact(kind="mask", function="helper", line=5, quote="idx = x & 3;", symbol="idx"),
            Fact(kind="guard", function="alarm_fn", line=9, quote="if (idx < 4) {", symbol="idx"),
        ],
    )
    merged = merge_packs(prep, call, var, source)
    assert merged.call_path.facts
    assert merged.var_value.facts

    tp = ProveReport(
        agent="TP_PROVE",
        claim="no_credible_case",
        witness="none",
        coverage="full",
        addressed_path_classes=["C1"],
        findings=[],
    )
    fp = ProveReport(
        agent="FP_PROVE",
        claim="fp",
        witness="none",
        coverage="full",
        addressed_path_classes=["C1"],
        findings=[
            {"function": "helper", "line": 5, "quote": "idx = x & 3;", "path_class_id": "C1"},
        ],
    )
    validated = validate_reports(merged, tp, fp, source)
    assert validated.fp.claim == "fp"
    assert not validated.fp.unaddressed_path_classes
