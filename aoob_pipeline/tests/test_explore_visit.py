"""Tests for visit cursor, submit gate, and seed facts (no LLM)."""

from __future__ import annotations

import json
from pathlib import Path

from aoob_pipeline.explore_support import call_path_sequence, seed_facts, var_value_sequence
from aoob_pipeline.prep import build_prep
from aoob_pipeline.session import ExploreCursor
from aoob_pipeline.source_index import SourceIndex
from aoob_pipeline.tools import make_explore_tools


def test_move_get_submit_requires_full_visit(tmp_path: Path) -> None:
    src = tmp_path / "input.c"
    src.write_text(
        "void a(void){ int x=1; }\n"
        "void b(void){ int y=2; }\n"
        "void c(void){ int z=a(); (void)z; }\n",
        encoding="utf-8",
    )
    source = SourceIndex(src)
    cursor = ExploreCursor(sequence=["a", "b", "c"])
    tools, sink = make_explore_tools(source, cursor)
    by_name = {t.name: t for t in tools}

    early = json.loads(by_name["submit_explore_pack"].invoke({"facts_json": "[]"}))
    assert early["accepted"] is False
    assert "remaining" in early

    assert "snippet" in json.loads(by_name["get_current"].invoke({}))
    by_name["move_func"].invoke({"direction": "next"})
    by_name["get_current"].invoke({})
    by_name["move_func"].invoke({"direction": "next"})
    by_name["get_current"].invoke({})

    status = json.loads(by_name["visit_status"].invoke({}))
    assert status["complete"] is True

    empty = json.loads(by_name["submit_explore_pack"].invoke({"facts_json": "[]"}))
    assert empty["accepted"] is False

    ok = json.loads(
        by_name["submit_explore_pack"].invoke(
            {
                "facts_json": json.dumps(
                    [{"kind": "path_step", "function": "a", "line": 1, "quote": "int x=1;"}]
                )
            }
        )
    )
    assert ok["accepted"] is True
    assert sink["pack"]["facts"]


def test_seed_and_sequences(tmp_path: Path) -> None:
    src = tmp_path / "input.c"
    src.write_text(
        "int buf[4];\n"
        "void alarm_fn(void) {\n"
        "  if (idx < 4) { (void)buf[idx]; }\n"
        "}\n",
        encoding="utf-8",
    )
    source = SourceIndex(src)
    alarm = {
        "group_id": 1,
        "location": "input.c:3.1-3",
        "enclosing_function": "alarm_fn",
        "variable": "idx",
        "paths": [
            {
                "path_id": 1,
                "call_stack": ["call#alarm_fn at input.c:2.1-10"],
                "function_sequence": ["alarm_fn"],
                "source_lines": ["(void)buf[idx];"],
            }
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
            "occurrences": [],
            "used_in_functions": ["alarm_fn"],
        },
    }
    prep = build_prep(pver_id="t", order="1", alarm=alarm, source=source)
    assert call_path_sequence(prep) == ["alarm_fn"]
    assert "alarm_fn" in var_value_sequence(prep, source)
    seeds = seed_facts(prep, source)
    assert any(f.kind == "declaration" for f in seeds)
    assert any(f.kind == "alarm_site" for f in seeds)
