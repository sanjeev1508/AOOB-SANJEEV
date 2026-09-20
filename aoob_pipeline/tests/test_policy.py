"""Tests for never-miss-TP / ironclad-FP policy."""

from __future__ import annotations

from aoob_pipeline.policy import fp_is_ironclad, prefer_label_after_gates
from aoob_pipeline.schemas import (
    LocalGuardHit,
    MergedPack,
    PathClass,
    PathStep,
    PrepPack,
    ProveReport,
    ValidatedReports,
    ExplorePack,
)


def _validated(fp_claim: str = "fp", **fp_kw) -> ValidatedReports:
    fp = ProveReport(
        agent="FP_PROVE",
        claim=fp_claim,  # type: ignore[arg-type]
        index_range=fp_kw.get("index_range", "guard_bounded"),
        array_size=fp_kw.get("array_size", "known"),
        witness=fp_kw.get("witness", "none"),
        coverage=fp_kw.get("coverage", "full"),
        findings=fp_kw.get("findings", [{"function": "f", "line": 1, "quote": "if (i < 4)"}]),
        missing_evidence=fp_kw.get("missing_evidence", []),
        addressed_path_classes=fp_kw.get("addressed", ["C1"]),
        unaddressed_path_classes=fp_kw.get("unaddressed", []),
    )
    tp = ProveReport(
        agent="TP_PROVE",
        claim=fp_kw.get("tp_claim", "no_credible_case"),
        witness=fp_kw.get("tp_witness", "none"),
        findings=fp_kw.get("tp_findings", []),
    )
    return ValidatedReports(tp=tp, fp=fp)


def _merged() -> MergedPack:
    prep = PrepPack(
        pver_id="t",
        order="1",
        path_classes=[
            PathClass(
                class_id="C1",
                sequence=[PathStep(function="f", call_site_line=1)],
                member_path_ids=[1],
                representative_path_id=1,
            )
        ],
        coverage_cap="full",
        local_guard=LocalGuardHit(found=False),
    )
    return MergedPack(
        prep=prep,
        call_path=ExplorePack(agent="CALL_PATH_EXPLORE"),
        var_value=ExplorePack(
            agent="VAR_VALUE_EXPLORE",
            facts=[],
        ),
        facts_by_class={
            "C1": [],
        },
        coverage="full",
    )


def test_weak_fp_not_ironclad() -> None:
    v = _validated(coverage="partial", unaddressed=["C1"], missing_evidence=["size"])
    ok, reasons = fp_is_ironclad(v, _merged())
    assert ok is False
    assert reasons


def test_clamp_fp_to_uncertain() -> None:
    v = _validated(coverage="partial")
    label, note = prefer_label_after_gates("FP", v, ironclad=False)
    assert label == "uncertain"
    assert note


def test_tp_witness_blocks_fp() -> None:
    v = _validated(tp_claim="tp", tp_witness="found", tp_findings=[{"function": "f", "line": 1, "quote": "x"}])
    # mark tp claim properly
    v = ValidatedReports(
        tp=ProveReport(agent="TP_PROVE", claim="tp", witness="found", findings=[{"function": "f", "line": 1, "quote": "buf[i]"}]),
        fp=v.fp,
    )
    label, _ = prefer_label_after_gates("FP", v, ironclad=True)
    assert label == "TP"
