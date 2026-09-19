"""LangGraph final classification (strict gates + multi-run majority)."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from aoob_pipeline import prompts
from aoob_pipeline.events import EventBus, preview_json
from aoob_pipeline.llm import build_agent_llm
from aoob_pipeline.schemas import FinalVerdict, ValidatedReports


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


def apply_hard_gates(validated: ValidatedReports) -> tuple[str | None, list[str]]:
    """Return forced label (or None) and gate notes."""
    notes: list[str] = []
    tp, fp = validated.tp, validated.fp
    if tp.claim == "tp" and tp.witness == "found":
        notes.append("gate: validated TP witness present")
        # Still allow LLM, but FP is forbidden.
    if fp.coverage != "full" or fp.unaddressed_path_classes:
        notes.append("gate: FP coverage incomplete — FP forbidden")
    if tp.claim == "tp" and tp.witness == "found" and fp.claim == "fp":
        notes.append("gate: TP witness vs FP claim conflict → prefer uncertain unless FP invalid")
    # Force uncertain when FP would be wrong under gates.
    force = None
    if tp.claim == "tp" and tp.witness == "found" and not (
        fp.coverage == "full" and not fp.unaddressed_path_classes
    ):
        force = "TP"
        notes.append("gate: force TP (validated witness, FP incomplete)")
    elif fp.claim == "fp" and (fp.coverage != "full" or fp.unaddressed_path_classes):
        force = "uncertain"
        notes.append("gate: force uncertain (FP claim without full coverage)")
    return force, notes


def _one_final_vote(validated: ValidatedReports, bus: EventBus | None = None, run_idx: int = 1) -> dict[str, Any]:
    llm = build_agent_llm("FINAL_CLASSIFICATION")
    payload = {
        "tp": validated.tp.model_dump(),
        "fp": validated.fp.model_dump(),
        "dropped_claims": validated.dropped_claims,
        "notes": validated.notes,
    }
    if bus:
        bus.emit(
            "agent_input",
            agent="FINAL_CLASSIFICATION",
            stage="final",
            title=f"Input → FINAL (run {run_idx})",
            input_preview=preview_json(payload),
            activity=f"adjudicating run {run_idx}",
        )

    def node(_state: FinalState) -> dict:
        if bus:
            bus.emit(
                "agent_thinking",
                agent="FINAL_CLASSIFICATION",
                stage="final",
                title=f"FINAL reasoning (run {run_idx})",
                activity="strict classification rules",
            )
        msg = llm.invoke(
            [
                SystemMessage(content=prompts.FINAL_CLASSIFICATION),
                HumanMessage(content=json.dumps(payload, indent=2, ensure_ascii=False)),
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
        if label == "UNCERTAIN":
            label = "uncertain"
        return {
            "result": {
                "label": label if label != "UNCERTAIN" else "uncertain",
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
) -> FinalVerdict:
    force, gates = apply_hard_gates(validated)
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
                activity="forced TP",
            )
        return FinalVerdict(
            label="TP",
            rationale="Hard gate: validated TP witness with incomplete/conflicting FP.",
            tp_summary=validated.tp.rationale,
            fp_summary=validated.fp.rationale,
            votes=["TP"],
            run_details=[{"forced": True}],
            hard_gates=gates,
        )

    for i in range(max(1, runs)):
        one = _one_final_vote(validated, bus=bus, run_idx=i + 1)
        label = one["label"]
        if force == "uncertain" and label == "FP":
            label = "uncertain"
            one = {**one, "label": "uncertain", "clamped": "FP→uncertain by hard gate"}
        if validated.tp.claim == "tp" and validated.tp.witness == "found" and label == "FP":
            label = "uncertain"
            one = {**one, "label": "uncertain", "clamped": "FP blocked: TP witness exists"}
        if (
            label == "FP"
            and (
                validated.fp.coverage != "full"
                or validated.fp.unaddressed_path_classes
                or validated.tp.claim == "tp"
            )
        ):
            label = "uncertain"
            one = {**one, "label": "uncertain", "clamped": "FP blocked by coverage/witness gates"}
        votes.append(label)
        details.append(one)
        if one.get("reexplore_requested"):
            reexplore = True
            focus = one.get("reexplore_focus") or focus

    counts = Counter(votes)
    if len(counts) > 1 and counts.most_common(1)[0][1] < len(votes):
        top_n = counts.most_common()
        if len(top_n) > 1 and top_n[0][1] == top_n[1][1]:
            winner = "uncertain"
        elif top_n[0][1] < len(votes):
            winner = "uncertain"
        else:
            winner = top_n[0][0]
    else:
        winner = counts.most_common(1)[0][0]

    rationale = details[-1].get("rationale") if details else ""
    if winner == "uncertain" and len(set(votes)) > 1:
        rationale = f"Majority disagreement among votes {votes}. " + str(rationale)

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
