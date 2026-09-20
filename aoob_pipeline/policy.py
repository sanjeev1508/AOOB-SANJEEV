"""Classification policy: never miss real TPs; FP only with ironclad evidence."""

from __future__ import annotations

from aoob_pipeline.schemas import MergedPack, ProveReport, ValidatedReports


def fp_is_ironclad(validated: ValidatedReports, merged: MergedPack | None = None) -> tuple[bool, list[str]]:
    """FP is allowed only when every check below passes.

    A wrong FP hides a real bug — that is the costliest error. Prefer
    uncertain/TP whenever any check fails.
    """
    reasons: list[str] = []
    fp = validated.fp
    tp = validated.tp

    if tp.claim == "tp" and tp.witness == "found":
        reasons.append("TP witness exists — FP forbidden")
    if fp.claim != "fp":
        reasons.append("FP agent did not claim fp")
    if fp.coverage != "full":
        reasons.append("FP coverage is not full")
    if fp.unaddressed_path_classes:
        reasons.append(f"unaddressed path classes: {fp.unaddressed_path_classes}")
    if fp.error:
        reasons.append(f"FP prover error: {fp.error}")
    if fp.index_range == "unknown":
        reasons.append("index_range unknown — cannot prove safe bounds")
    if fp.array_size != "known":
        reasons.append("array_size not known")
    if fp.witness == "found":
        reasons.append("FP report still marks a witness — inconsistent")
    if not fp.findings:
        reasons.append("no validated FP findings/quotes")
    if fp.missing_evidence:
        reasons.append(f"missing_evidence still listed: {fp.missing_evidence}")

    all_ids = []
    if merged is not None:
        all_ids = [c.class_id for c in merged.prep.path_classes]
        if merged.coverage == "partial":
            reasons.append("merged explore coverage is partial")
        if merged.prep.coverage_cap == "partial":
            reasons.append("path-class cap left coverage partial")
        if all_ids and set(fp.addressed_path_classes) < set(all_ids):
            reasons.append("not every path class addressed")
        # Require at least one bound-related fact kind in the merged pack
        bound_kinds = {"guard", "clamp", "mask", "loop_bound", "local_guard", "array_size_hint", "declaration"}
        kinds = {f.kind for facts in merged.facts_by_class.values() for f in facts}
        kinds |= {f.kind for f in merged.var_value.facts}
        if not (kinds & bound_kinds) and fp.index_range != "constant":
            reasons.append("no guard/clamp/mask/size facts in merged pack")

    ok = not reasons
    return ok, reasons


def prefer_label_after_gates(
    vote: str,
    validated: ValidatedReports,
    *,
    ironclad: bool,
) -> tuple[str, str | None]:
    """Clamp a model vote under the never-miss-TP policy (less uncertain bias)."""
    label = vote
    note = None
    if validated.tp.claim == "tp" and validated.tp.witness == "found":
        if label == "FP":
            label, note = "TP", "clamped FP→TP (validated witness)"
        elif label == "uncertain":
            label, note = "TP", "clamped uncertain→TP (validated witness)"
        elif label != "TP":
            label, note = "TP", "clamped →TP (validated witness)"
    elif label == "FP" and not ironclad:
        # Prefer TP claim over parking as uncertain when TP argued a case
        if validated.tp.claim == "tp":
            label, note = "TP", "clamped FP→TP (not ironclad; TP claim stands)"
        else:
            label, note = "uncertain", "clamped FP→uncertain (evidence not ironclad)"
    return label, note
