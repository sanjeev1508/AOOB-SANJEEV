"""Regression tests for the sample-run bugs (no LLM).

Covers: ``|separate`` frames, index-expression cleanup, in-scope index
declaration, initializer-size recovery on long tables, index origin →
coverage, LLM-fact normalisation, merge dedup, compact prover brief, and
prover-failure handling in validate/final.
"""

from __future__ import annotations

from pathlib import Path

from aoob_pipeline.explore_support import (
    compact_explore_pack,
    dataflow_symbol_paths,
    mine_facts_from_body,
    normalize_llm_fact,
    relevance_tokens,
    seed_facts,
    var_value_sequence,
)
from aoob_pipeline.final_graph import run_final_classification
from aoob_pipeline.llm import ctx_for_prompt
from aoob_pipeline.merge import merge_packs
from aoob_pipeline.prep import (
    build_prep,
    caller_value_origin_callees,
    clean_index_expression,
    index_origin,
    locate_declaration,
)
from aoob_pipeline.prove_graph import build_brief, compact_facts, report_is_empty
from aoob_pipeline.schemas import ExplorePack, Fact, ProveReport, ValidatedReports
from aoob_pipeline.source_index import SourceIndex
from aoob_pipeline.validate import validate_reports

_SRC = """\
typedef unsigned char uint8;
typedef unsigned short uint16;
typedef unsigned int uint32;
uint16 numClass;
const uint8 tbl[] =
{
  {1, 2},
  {3, 4},
  {5, 6}
};
uint8 GetCls(uint16 id)
{
  return (uint8)(id & 0x7u);
}
uint32 TableSize(void)
{
  return 3u;
}
void Leaf(uint8 idxEntry, uint8 numClass)
{
  uint8 x = tbl[(numClass)];
  (void)idxEntry; (void)x;
}
void Caller(uint16 dfc)
{
  uint8 numClass;
  numClass = GetCls(dfc);
  Leaf(0, numClass);
}
void LocalIdx(uint16 did)
{
  uint32 posn;
  uint32 size;
  size = TableSize();
  posn = (size - 1u) / 2u;
  if (tbl[posn] == did) { posn = 0u; }
}
"""


def _source(tmp_path: Path) -> SourceIndex:
    src = tmp_path / "input.c"
    src.write_text(_SRC, encoding="utf-8")
    return SourceIndex(src)


def _line_of(text: str) -> int:
    for i, row in enumerate(_SRC.splitlines(), start=1):
        if text in row:
            return i
    raise AssertionError(text)


def _alarm(**over):
    alarm_line = _line_of("tbl[(numClass)]")
    base = {
        "group_id": 7,
        "location": f"input.c:{alarm_line}.10-20",
        "enclosing_function": "Leaf",
        "variable": "(numClass",
        "paths": [
            {
                "path_id": 1,
                "call_stack": [
                    f"call#Caller|separate at input.c:{_line_of('void Caller')}.1-5",
                    f"call#Leaf at input.c:{_line_of('Leaf(0, numClass)')}.3-10",
                ],
                "function_sequence": ["Caller", "Leaf"],
            }
        ],
        "variable_info": {
            "symbol_name": "tbl",
            "role": "flagged_array",
            "kind": "array",
            "index_expression": "(numClass",
            "declaration_line": _line_of("const uint8 tbl[] ="),
            "declaration_text": "const uint8 tbl[] =",
            "array_dims": "[]",
            "resolved_sizes": [],
            "occurrences": [],
            "used_in_functions": ["Leaf"],
        },
        "symbol_infos": [
            {
                # analyzer picked the *global* numClass — wrong scope
                "symbol_name": "numClass",
                "role": "index",
                "kind": "global",
                "declaration_line": _line_of("uint16 numClass;"),
                "declaration_text": "uint16 numClass;",
                "occurrences": [],
                "used_in_functions": ["global", "Leaf", "Caller"],
            }
        ],
    }
    base.update(over)
    return base


def test_clean_index_expression() -> None:
    assert clean_index_expression("(numClass") == "numClass"
    assert clean_index_expression("numClass)") == "numClass"
    assert clean_index_expression("(a + b)") == "a + b"
    assert clean_index_expression(" idx ") == "idx"


def test_prep_restores_separate_frame_and_repoints_index(tmp_path: Path) -> None:
    source = _source(tmp_path)
    prep = build_prep(pver_id="t", order="7", alarm=_alarm(), source=source)

    # ``call#Caller|separate`` must survive
    assert [s.function for s in prep.path_classes[0].sequence] == ["Caller", "Leaf"]
    assert prep.index_expression == "numClass"
    idx = next(s for s in prep.symbols if s.symbol_name == "numClass")
    assert idx.declaration_line == _line_of("void Leaf(")
    assert idx.kind == "parameter"
    # initializer with nested braces → 3 elements
    assert prep.array_size == 3
    assert prep.index_origin == "parameter"
    assert "GetCls" in prep.value_origin_callees
    assert "GetCls" in var_value_sequence(prep, source)

    seeds = seed_facts(prep, source)
    hint = next(f for f in seeds if f.kind == "array_size_hint")
    assert "count=3" in (hint.note or "")
    assert source.verify_quote(hint.line, hint.quote, radius=0)


def test_locate_declaration_and_origin(tmp_path: Path) -> None:
    source = _source(tmp_path)
    hit = locate_declaration(source, function="Leaf", near_line=_line_of("tbl[(numClass)]"), token="numClass")
    assert hit and hit[2] == "parameter"

    alarm_line = _line_of("tbl[posn]")
    origin, callees = index_origin(source, function="LocalIdx", alarm_line=alarm_line, tokens=["posn"])
    assert origin == "local"  # ``2u`` must not leak an identifier ``u``
    assert callees == ["TableSize"]

    assert caller_value_origin_callees(
        source,
        path_classes=build_prep(pver_id="t", order="7", alarm=_alarm(), source=source).path_classes,
        tokens=["numClass"],
    ) == ["GetCls"]


def test_local_origin_keeps_coverage_full_when_capped(tmp_path: Path) -> None:
    source = _source(tmp_path)
    alarm_line = _line_of("tbl[posn]")
    paths = [
        {
            "path_id": i,
            "call_stack": [f"call#Top{i} at input.c:1.1-2", f"call#LocalIdx at input.c:{alarm_line}.1-2"],
            "function_sequence": [f"Top{i}", "LocalIdx"],
        }
        for i in range(1, 5)
    ]
    alarm = _alarm(
        location=f"input.c:{alarm_line}.6-10",
        enclosing_function="LocalIdx",
        variable="posn",
        paths=paths,
        symbol_infos=[],
    )
    alarm["variable_info"]["index_expression"] = "posn"
    prep = build_prep(pver_id="t", order="8", alarm=alarm, source=source, path_class_cap=2)
    assert prep.index_origin == "local"
    assert prep.coverage_cap == "full"
    assert any("computed locally" in n for n in prep.notes)
    # callers are irrelevant for a local index → not in the VAR sequence
    seq = var_value_sequence(prep, source)
    assert seq[0] == "LocalIdx"
    assert "TableSize" in seq
    assert "Caller1" not in seq and "Caller4" not in seq
    # Leaf may appear via the symbol's used_in / function_sequence (dataflow)


def test_mining_does_not_treat_declaration_as_access(tmp_path: Path) -> None:
    source = _source(tmp_path)
    prep = build_prep(pver_id="t", order="7", alarm=_alarm(), source=source)
    body = {"snippet": f"{_line_of('const uint8 tbl[] =')}: const uint8 tbl[] =\n{_line_of('tbl[(numClass)]')}:   uint8 x = tbl[(numClass)];"}
    mined = mine_facts_from_body(agent="X", function="Leaf", body=body, prep=prep)
    kinds = {(f.kind, f.line) for f in mined}
    assert ("declaration", _line_of("const uint8 tbl[] =")) in kinds
    assert ("access", _line_of("const uint8 tbl[] =")) not in kinds
    assert ("alarm_site", _line_of("tbl[(numClass)]")) in kinds


def test_normalize_llm_fact(tmp_path: Path) -> None:
    source = _source(tmp_path)
    prep = build_prep(pver_id="t", order="7", alarm=_alarm(), source=source)
    relevant = relevance_tokens(prep, ["Caller", "Leaf"])
    ln = _line_of("numClass = GetCls(dfc);")

    # off-by-one line is corrected
    f = normalize_llm_fact({"kind": "write", "line": ln + 1, "quote": "numClass = GetCls(dfc);", "symbol": "numClass"},
                           function="Caller", prep=prep, source=source, relevant=relevant)
    assert f and f.line == ln and f.kind == "write"

    # bogus alarm_site elsewhere is re-kinded, never kept as alarm_site
    f = normalize_llm_fact({"kind": "alarm_site", "line": ln, "quote": "numClass = GetCls(dfc);"},
                           function="Caller", prep=prep, source=source, relevant=relevant)
    assert f and f.kind != "alarm_site"

    # "write" where the symbol is on the RHS becomes a read
    f = normalize_llm_fact({"kind": "write", "line": _line_of("Leaf(0, numClass)"), "quote": "Leaf(0, numClass);", "symbol": "numClass"},
                           function="Caller", prep=prep, source=source, relevant=relevant)
    assert f and f.kind == "read"

    # irrelevant declaration is dropped
    f = normalize_llm_fact({"kind": "declaration", "line": _line_of("uint32 size;"), "quote": "uint32 size;", "symbol": "size"},
                           function="LocalIdx", prep=prep, source=source, relevant=relevant)
    assert f is None


def test_merge_dedups_and_brief_is_compact(tmp_path: Path) -> None:
    source = _source(tmp_path)
    prep = build_prep(pver_id="t", order="7", alarm=_alarm(), source=source)
    seeds = seed_facts(prep, source)
    call = ExplorePack(agent="CALL_PATH_EXPLORE", facts=list(seeds))
    var = ExplorePack(agent="VAR_VALUE_EXPLORE", facts=list(seeds))
    merged = merge_packs(prep, call, var, source)
    assert len(merged.call_path.facts) == len(seeds)
    assert merged.var_value.facts == []  # duplicates removed across packs

    rows = compact_facts(merged)
    assert len(rows) == len(seeds)
    assert all(r["path_classes"] in ("all", ["C1"]) for r in rows)
    text, stats = build_brief(merged, max_chars=100000)
    assert stats["trimmed"] == 0
    assert '"facts_by_class"' not in text


def test_prover_failure_short_circuits_final(tmp_path: Path) -> None:
    source = _source(tmp_path)
    prep = build_prep(pver_id="t", order="7", alarm=_alarm(), source=source)
    merged = merge_packs(prep, ExplorePack(agent="CALL_PATH_EXPLORE", facts=seed_facts(prep, source)),
                         ExplorePack(agent="VAR_VALUE_EXPLORE"), source)
    tp = ProveReport(agent="TP_PROVE", rationale="", error="prover_failure: x")
    fp = ProveReport(agent="FP_PROVE", rationale="", error="prover_failure: y")
    assert report_is_empty(ProveReport(agent="FP_PROVE"))
    validated = validate_reports(merged, tp, fp, source)
    assert any("prover failed" in d for d in validated.dropped_claims)
    verdict = run_final_classification(validated, runs=3, merged=merged)  # no LLM call happens
    assert verdict.label == "uncertain"
    assert verdict.run_details[0]["reason"] == "prover_failure"


def test_ctx_grows_to_fit_prompt() -> None:
    assert ctx_for_prompt(1000, 8192) == 8192
    assert ctx_for_prompt(60000, 8192) == 32768
    assert ctx_for_prompt(260000, 8192) == 32768  # capped at AOOB_MAX_NUM_CTX default


def test_validate_local_origin_addresses_all_classes(tmp_path: Path) -> None:
    source = _source(tmp_path)
    alarm_line = _line_of("tbl[posn]")
    paths = [
        {"path_id": i, "call_stack": [f"call#Top{i} at input.c:1.1-2", f"call#LocalIdx at input.c:{alarm_line}.1-2"],
         "function_sequence": [f"Top{i}", "LocalIdx"]}
        for i in range(1, 4)
    ]
    alarm = _alarm(location=f"input.c:{alarm_line}.6-10", enclosing_function="LocalIdx",
                   variable="posn", paths=paths, symbol_infos=[])
    alarm["variable_info"]["index_expression"] = "posn"
    prep = build_prep(pver_id="t", order="9", alarm=alarm, source=source)
    assert prep.index_origin == "local"
    facts = [Fact(kind="guard", function="LocalIdx", line=alarm_line, quote="if (tbl[posn] == did)", symbol="posn")]
    merged = merge_packs(prep, ExplorePack(agent="CALL_PATH_EXPLORE", facts=facts),
                         ExplorePack(agent="VAR_VALUE_EXPLORE"), source)
    fp = ProveReport(agent="FP_PROVE", claim="no_credible_case", coverage="full",
                     addressed_path_classes=["C1"], rationale="bounded locally",
                     findings=[{"function": "LocalIdx", "line": alarm_line, "quote": "if (tbl[posn] == did)"}])
    validated: ValidatedReports = validate_reports(merged, ProveReport(agent="TP_PROVE", rationale="none"), fp, source)
    assert validated.fp.unaddressed_path_classes == []


def test_multi_symbol_var_sequence_and_compact_pack(tmp_path: Path) -> None:
    source = _source(tmp_path)
    prep = build_prep(pver_id="t", order="7", alarm=_alarm(), source=source)
    names = {s.symbol_name for s in prep.symbols}
    assert "tbl" in names and "numClass" in names

    paths = dataflow_symbol_paths(prep)
    assert {p["symbol"] for p in paths} >= {"tbl", "numClass"}
    for p in paths:
        assert p["functions"], f"symbol {p['symbol']} needs a function chain"

    seq = var_value_sequence(prep, source)
    # Every dataflow symbol's used_in / declaration function appears
    for p in paths:
        for fn in p["functions"]:
            assert fn in seq, f"{fn} missing from var sequence for {p['symbol']}"

    seeds = seed_facts(prep, source)
    # Noise facts that compact should drop for call_path role
    noise = [
        Fact(kind="access", function="Leaf", line=1, quote="x", symbol="tbl"),
        Fact(kind="write", function="Leaf", line=2, quote="y", symbol="numClass"),
        *seeds,
        *seeds,  # duplicates
    ]
    pack = ExplorePack(agent="CALL_PATH_EXPLORE", facts=noise)
    compact = compact_explore_pack(pack, role="call_path", max_facts=8)
    assert len(compact.facts) <= 8
    assert all(f.kind != "access" for f in compact.facts)  # access not in call allowlist
    assert any(f.kind == "declaration" for f in compact.facts)
