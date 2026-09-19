"""LangGraph prover agents (no tools) — TP and FP structured reports."""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from aoob_pipeline import prompts
from aoob_pipeline.events import EventBus, preview_json
from aoob_pipeline.llm import build_agent_llm
from aoob_pipeline.schemas import MergedPack, ProveReport


class ProveState(TypedDict):
    report: dict


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
        raise ValueError("expected JSON object")
    return data


def _merged_brief(merged: MergedPack) -> str:
    return json.dumps(
        {
            "coverage": merged.coverage,
            "notes": merged.notes,
            "prep": {
                "order": merged.prep.order,
                "index_expression": merged.prep.index_expression,
                "array_name": merged.prep.array_name,
                "array_size": merged.prep.array_size,
                "path_classes": [c.model_dump() for c in merged.prep.path_classes],
                "local_guard": merged.prep.local_guard.model_dump(),
            },
            "facts_by_class": {
                cid: [f.model_dump() for f in facts] for cid, facts in merged.facts_by_class.items()
            },
            "call_path_facts": [f.model_dump() for f in merged.call_path.facts],
            "var_value_facts": [f.model_dump() for f in merged.var_value.facts],
        },
        indent=2,
        ensure_ascii=False,
    )


def _to_report(agent: str, data: dict[str, Any], error: str | None = None) -> ProveReport:
    claim = str(data.get("claim") or "no_credible_case").lower()
    if claim not in {"tp", "fp", "no_credible_case"}:
        claim = "no_credible_case"
    if agent == "TP_PROVE" and claim == "fp":
        claim = "no_credible_case"
    if agent == "FP_PROVE" and claim == "tp":
        claim = "no_credible_case"
    return ProveReport(
        agent=agent,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        index_range=data.get("index_range") or "unknown",  # type: ignore[arg-type]
        array_size=data.get("array_size") or "unknown",  # type: ignore[arg-type]
        witness=data.get("witness") or "none",  # type: ignore[arg-type]
        coverage=data.get("coverage") or "partial",  # type: ignore[arg-type]
        findings=list(data.get("findings") or []),
        missing_evidence=[str(x) for x in (data.get("missing_evidence") or [])],
        addressed_path_classes=[str(x) for x in (data.get("addressed_path_classes") or [])],
        unaddressed_path_classes=[str(x) for x in (data.get("unaddressed_path_classes") or [])],
        rationale=str(data.get("rationale") or ""),
        error=error,
    )


def _run_prove(
    agent_name: str,
    system: str,
    merged: MergedPack,
    bus: EventBus | None = None,
) -> ProveReport:
    llm = build_agent_llm(agent_name)
    brief = _merged_brief(merged)
    funcs = sorted(
        {
            *(s.function for c in merged.prep.path_classes for s in c.sequence if s.function),
            *([merged.prep.enclosing_function] if merged.prep.enclosing_function else []),
        }
    )
    if bus:
        bus.emit(
            "agent_input",
            agent=agent_name,
            stage="prove",
            title=f"Input → {agent_name}",
            input_preview=brief,
            functions=funcs,
            activity="consuming merged pack",
        )

    def node(_state: ProveState) -> dict:
        if bus:
            bus.emit(
                "agent_thinking",
                agent=agent_name,
                stage="prove",
                title=f"{agent_name} reasoning",
                activity="proving from merged facts (no tools)",
                functions=funcs,
            )
        msg = llm.invoke(
            [
                SystemMessage(content=system),
                HumanMessage(content="MERGED PACK:\n" + brief),
            ]
        )
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        try:
            data = _extract_json(content)
            return {"report": _to_report(agent_name, data).model_dump()}
        except Exception as exc:  # noqa: BLE001
            return {
                "report": _to_report(
                    agent_name,
                    {"claim": "no_credible_case", "rationale": content[:1000]},
                    error=str(exc),
                ).model_dump()
            }

    graph = StateGraph(ProveState)
    graph.add_node("prove", node)
    graph.add_edge(START, "prove")
    graph.add_edge("prove", END)
    out = graph.compile().invoke({"report": {}})
    report = ProveReport(**out["report"])
    if bus:
        bus.emit(
            "agent_output",
            agent=agent_name,
            stage="prove",
            title=f"Output ← {agent_name}",
            output_preview=preview_json(report.model_dump()),
            functions=funcs,
            activity=f"claim={report.claim} witness={report.witness}",
        )
    return report


def run_tp_prove(merged: MergedPack, bus: EventBus | None = None) -> ProveReport:
    return _run_prove("TP_PROVE", prompts.TP_PROVE, merged, bus=bus)


def run_fp_prove(merged: MergedPack, bus: EventBus | None = None) -> ProveReport:
    return _run_prove("FP_PROVE", prompts.FP_PROVE, merged, bus=bus)
