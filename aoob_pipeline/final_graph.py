"""LangGraph final classification (strict FP-precision gates + multi-run)."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from aoob_pipeline import prompts
from aoob_pipeline.config import pipeline_config
from aoob_pipeline.events import EventBus, preview_json
from aoob_pipeline.llm import build_agent_llm
from aoob_pipeline.policy import fp_is_ironclad, prefer_label_after_gates
from aoob_pipeline.schemas import FinalVerdict, MergedPack, ValidatedReports


class FinalState(TypedDict):
    result: dict


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if "</think>" in raw:
        raw = raw.split("</think>")[-1].strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("expected object")
    return data


def apply_hard_gates(
    validated: ValidatedReports,
    merged: MergedPack | None = None,
) -> tuple[str | None, list[str], bool]:
    """Return (forced_label, gate_notes, fp_ironclad).

    Forced labels:
    - TP when a validated witness exists
    - FP when ironclad and TP has no witness (decisive FP)
    - None otherwise (LLM may vote; FP still clamped if not ironclad)
    """
    notes: list[str] = []
    tp, fp = validated.tp, validated.fp
    ironclad, why = fp_is_ironclad(validated, merged)
    if not ironclad:
        notes.extend(f"fp-block: {r}" for r in why)
    else:
        notes.append("gate: FP checklist ironclad")

    force: str | None = None
    if tp.claim == "tp" and tp.witness == "found":
        notes.append("gate: validated TP witness present")
        force = "TP"
        notes.append("gate: force TP (never miss a witnessed bug)")
    elif fp.claim == "fp" and ironclad and not (tp.claim == "tp" and tp.witness == "found"):
        # Decisive ironclad FP — do not park as uncertain
        force = "FP"
        notes.append("gate: force FP (ironclad; no TP witness)")
    elif fp.claim == "fp" and not ironclad:
        # Do NOT force uncertain — let the judge weigh TP vs incomplete FP
        notes.append("gate: FP claimed but not ironclad — judge may pick TP/uncertain (FP votes clamped)")
    return force, notes, ironclad


def _one_final_vote(
    validated: ValidatedReports,
    *,
    ironclad: bool,
    bus: EventBus | None = None,
    run_idx: int = 1,
    temperature: float | None = None,
) -> dict[str, Any]:
    payload = {
        "policy": {
            "wrong_fp_is_worst_error": True,
            "fp_requires_ironclad_full_trace": True,
            "default_when_unsure": "pick_stronger_claim_else_uncertain",
            "fp_currently_ironclad": ironclad,
        },
        "tp": validated.tp.model_dump(),
        "fp": validated.fp.model_dump(),
        "dropped_claims": validated.dropped_claims,
        "notes": validated.notes,
    }
    payload_text = json.dumps(payload, indent=2, ensure_ascii=False)
    llm = build_agent_llm(
        "FINAL_CLASSIFICATION",
        prompt_chars=len(prompts.FINAL_CLASSIFICATION) + len(payload_text),
        temperature=temperature,
    )
    if bus:
        bus.emit(
            "agent_input",
            agent="FINAL_CLASSIFICATION",
            stage="final",
            title=f"Input → FINAL (run {run_idx})",
            input_preview=preview_json(payload),
            activity=f"adjudicating run {run_idx}"
            + (f" (T={temperature})" if temperature is not None else ""),
        )

    def node(_state: FinalState) -> dict:
        if bus:
            bus.emit(
                "agent_thinking",
                agent="FINAL_CLASSIFICATION",
                stage="final",
                title=f"FINAL reasoning (run {run_idx})",
                activity="FP-precision policy (never miss TP)",
            )
        msg = llm.invoke(
            [
                SystemMessage(content=prompts.FINAL_CLASSIFICATION),
                HumanMessage(content=payload_text),
            ]
        )
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        try:
            data = _extract_json(content)
        except Exception as exc:  # noqa: BLE001
            data = {
                "label": "uncertain",
                "rationale": f"parse failure: {exc}; raw={content[:500]}",
                "reexplore_requested": False,
                "reexplore_focus": None,
            }
        label = str(data.get("label") or "uncertain").upper()
        if label not in {"TP", "FP", "UNCERTAIN"}:
            label = "UNCERTAIN"
        return {
            "result": {
                "label": "uncertain" if label == "UNCERTAIN" else label,
                "rationale": str(data.get("rationale") or ""),
                "reexplore_requested": bool(data.get("reexplore_requested")),
                "reexplore_focus": data.get("reexplore_focus"),
            }
        }

    graph = StateGraph(FinalState)
    graph.add_node("final", node)
    graph.add_edge(START, "final")
    graph.add_edge("final", END)
    out = graph.compile().invoke({"result": {}})
    result = out["result"]
    lab = str(result.get("label") or "uncertain")
    if lab.upper() == "UNCERTAIN":
        result["label"] = "uncertain"
    elif lab.upper() == "TP":
        result["label"] = "TP"
    elif lab.upper() == "FP":
        result["label"] = "FP"
    else:
        result["label"] = "uncertain"
    if bus:
        bus.emit(
            "agent_output",
            agent="FINAL_CLASSIFICATION",
            stage="final",
            title=f"Output ← FINAL (run {run_idx})",
            output_preview=preview_json(result),
            activity=f"vote={result.get('label')}",
        )
    return result


def run_final_classification(
    validated: ValidatedReports,
    *,
    runs: int = 3,
    bus: EventBus | None = None,
    merged: MergedPack | None = None,
) -> FinalVerdict:
    force, gates, ironclad = apply_hard_gates(validated, merged)
    votes: list[str] = []
    details: list[dict[str, Any]] = []
    reexplore = False
    focus = None

    if force == "TP":
        if bus:
            bus.emit(
                "stage",
                agent="FINAL_CLASSIFICATION",
                stage="final",
                title="Hard gate → TP",
                detail="; ".join(gates),
                activity="forced TP (validated witness)",
            )
        return FinalVerdict(
            label="TP",
            rationale="Hard gate: validated TP witness — never auto-close as FP.",
            tp_summary=validated.tp.rationale,
            fp_summary=validated.fp.rationale,
            votes=["TP"],
            run_details=[{"forced": True, "reason": "tp_witness"}],
            hard_gates=gates,
        )

    if force == "FP":
        if bus:
            bus.emit(
                "stage",
                agent="FINAL_CLASSIFICATION",
                stage="final",
                title="Hard gate → FP",
                detail="; ".join(gates),
                activity="forced FP (ironclad; no TP witness)",
            )
        return FinalVerdict(
            label="FP",
            rationale="Hard gate: ironclad FP checklist and no validated TP witness.",
            tp_summary=validated.tp.rationale,
            fp_summary=validated.fp.rationale,
            votes=["FP"],
            run_details=[{"forced": True, "reason": "fp_ironclad"}],
            hard_gates=gates,
        )

    # Both provers failed → nothing to adjudicate. Do not burn N LLM calls on
    # garbage; the answer is uncertain by construction.
    if validated.tp.error and validated.fp.error:
        gates = [*gates, "gate: both provers failed — uncertain without final votes"]
        if bus:
            bus.emit(
                "stage",
                agent="FINAL_CLASSIFICATION",
                stage="final",
                title="Provers failed → uncertain",
                detail=f"TP: {validated.tp.error}; FP: {validated.fp.error}",
                activity="skipping final votes",
            )
        return FinalVerdict(
            label="uncertain",
            rationale=(
                "Both TP_PROVE and FP_PROVE failed to return usable reports "
                "(model/context problem, not evidence). Human review required."
            ),
            tp_summary=validated.tp.rationale,
            fp_summary=validated.fp.rationale,
            votes=["uncertain"],
            run_details=[{"forced": True, "reason": "prover_failure"}],
            hard_gates=gates,
        )

    temps = pipeline_config().final_temperatures
    for i in range(max(1, runs)):
        temp = temps[i % len(temps)] if temps else None
        one = _one_final_vote(
            validated, ironclad=ironclad, bus=bus, run_idx=i + 1, temperature=temp
        )
        label, note = prefer_label_after_gates(one["label"], validated, ironclad=ironclad)
        if note:
            one = {**one, "label": label, "clamped": note}
        else:
            one = {**one, "label": label}
        votes.append(label)
        details.append(one)
        # Re-explore only to gather missing bound/size evidence — never to hunt FP.
        if one.get("reexplore_requested") and label == "uncertain":
            reexplore = True
            focus = one.get("reexplore_focus") or focus

    counts = Counter(votes)
    # Majority FP when ironclad (no longer require unanimous — reduces uncertain bias).
    if "FP" in counts and ironclad and counts["FP"] * 2 >= len(votes):
        winner = "FP"
        if counts["FP"] < len(votes):
            gates = [*gates, "majority: FP accepted with ironclad majority"]
    elif "FP" in counts and not ironclad:
        winner = counts.most_common(1)[0][0]
        if winner == "FP":
            winner = "uncertain"
            gates = [*gates, "majority: FP clamped (not ironclad)"]
    elif len(counts) > 1 and counts.most_common(1)[0][1] < len(votes):
        top_n = counts.most_common()
        if len(top_n) > 1 and top_n[0][1] == top_n[1][1]:
            # Tie: prefer TP if plurality includes TP with witness, else stronger claim
            if "TP" in counts and validated.tp.witness == "found":
                winner = "TP"
            elif "TP" in counts and validated.tp.claim == "tp":
                winner = "TP"
            else:
                winner = "uncertain"
        else:
            winner = top_n[0][0]
    else:
        winner = counts.most_common(1)[0][0]

    # Final safety clamp
    winner, final_note = prefer_label_after_gates(winner, validated, ironclad=ironclad)
    if final_note:
        gates = [*gates, final_note]

    rationale = details[-1].get("rationale") if details else ""
    if winner == "uncertain" and len(set(votes)) > 1:
        rationale = f"Majority/policy disagreement among votes {votes}. " + str(rationale)
    if winner == "FP":
        rationale = (
            "Ironclad FP: full coverage, known size, bounded index on all path classes, "
            "no TP witness. " + str(rationale)
        )

    verdict = FinalVerdict(
        label=winner,  # type: ignore[arg-type]
        rationale=str(rationale),
        tp_summary=validated.tp.rationale,
        fp_summary=validated.fp.rationale,
        reexplore_requested=reexplore and winner == "uncertain",
        reexplore_focus=str(focus) if focus else None,
        votes=votes,
        run_details=details,
        hard_gates=gates,
    )
    if bus:
        bus.emit(
            "agent_output",
            agent="FINAL_CLASSIFICATION",
            stage="final",
            title="Final verdict",
            output_preview=preview_json(verdict.model_dump()),
            activity=f"label={verdict.label}",
        )
    return verdict
