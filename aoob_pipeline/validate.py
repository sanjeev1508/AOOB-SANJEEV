"""Evidence validator: drop uncited claims; downgrade weak FP/TP."""

from __future__ import annotations

from typing import Any

from aoob_pipeline.policy import fp_is_ironclad
from aoob_pipeline.schemas import MergedPack, ProveReport, ValidatedReports
from aoob_pipeline.source_index import SourceIndex, collapse_ws


def _fact_index(merged: MergedPack) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for facts in merged.facts_by_class.values():
        for fact in facts:
            rows.append((fact.line, fact.function, collapse_ws(fact.quote)))
    for fact in [*merged.call_path.facts, *merged.var_value.facts]:
        rows.append((fact.line, fact.function, collapse_ws(fact.quote)))
    return rows


def _finding_supported(finding: dict[str, Any], index: list[tuple[int, str, str]], source: SourceIndex) -> bool:
    line = finding.get("line")
    quote = finding.get("quote") or finding.get("evidence") or ""
    function = finding.get("function") or ""
    if line is None or not quote:
        return False
    try:
        line_i = int(line)
    except (TypeError, ValueError):
        return False
    q = collapse_ws(str(quote))
    for fline, ffn, fquote in index:
        if fline == line_i and (not function or function == ffn) and (q in fquote or fquote in q):
            return True
    return source.verify_quote(line_i, str(quote))


def validate_reports(
    merged: MergedPack,
    tp: ProveReport,
    fp: ProveReport,
    source: SourceIndex,
) -> ValidatedReports:
    index = _fact_index(merged)
    dropped: list[str] = []

    def scrub(report: ProveReport, label: str) -> ProveReport:
        kept: list[dict[str, Any]] = []
        for finding in report.findings:
            if _finding_supported(finding, index, source):
                kept.append(finding)
            else:
                dropped.append(f"{label}: dropped unsupported finding {finding!r}")
        witness = report.witness
        if report.claim == "tp" and witness == "found" and not kept:
            witness = "none"
            dropped.append(f"{label}: cleared witness — no validated findings")
        all_ids = [c.class_id for c in merged.prep.path_classes]
        addressed = [c for c in report.addressed_path_classes if c in all_ids]
        if report.agent == "FP_PROVE":
            unaddressed = [c for c in all_ids if c not in addressed]
        else:
            unaddressed = list(report.unaddressed_path_classes)
        coverage = report.coverage
        if report.agent == "FP_PROVE" and unaddressed:
            coverage = "partial"
        if merged.coverage == "partial":
            coverage = "partial"
        claim = report.claim
        if report.agent == "TP_PROVE" and claim == "tp" and witness != "found":
            claim = "no_credible_case"
            dropped.append(f"{label}: TP claim downgraded — no validated witness")
        return report.model_copy(
            update={
                "findings": kept,
                "witness": witness,
                "claim": claim,
                "coverage": coverage,
                "addressed_path_classes": addressed,
                "unaddressed_path_classes": unaddressed,
            }
        )

    scrubbed = ValidatedReports(
        tp=scrub(tp, "TP"),
        fp=scrub(fp, "FP"),
        dropped_claims=list(dropped),
        notes=["validator checked quotes against merged facts and source"],
    )

    # Downgrade FP claims that are not ironclad — never let a soft FP reach the judge as fp.
    ok, reasons = fp_is_ironclad(scrubbed, merged)
    if scrubbed.fp.claim == "fp" and not ok:
        scrubbed = scrubbed.model_copy(
            update={
                "fp": scrubbed.fp.model_copy(
                    update={
                        "claim": "no_credible_case",
                        "coverage": "partial" if scrubbed.fp.coverage != "full" else scrubbed.fp.coverage,
                        "missing_evidence": list(
                            dict.fromkeys([*scrubbed.fp.missing_evidence, *reasons])
                        ),
                        "rationale": (
                            scrubbed.fp.rationale
                            + " | Downgraded from fp: evidence not ironclad ("
                            + "; ".join(reasons)
                            + ")."
                        ),
                    }
                ),
                "dropped_claims": [
                    *scrubbed.dropped_claims,
                    "FP: claim downgraded fp→no_credible_case (not ironclad)",
                ],
                "notes": [
                    *scrubbed.notes,
                    "FP precision policy: only ironclad full-trace FP may stand",
                ],
            }
        )

    return scrubbed
