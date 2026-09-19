"""LangGraph prover agents (no tools) — TP and FP structured reports."""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from aoob_pipeline import prompts
from aoob_pipeline.config import pipeline_config
from aoob_pipeline.events import EventBus, preview_json
from aoob_pipeline.llm import build_agent_llm
from aoob_pipeline.schemas import Fact, MergedPack, ProveReport


class ProveState(TypedDict):
    report: dict


# Higher = more useful to a prover; low-priority facts are trimmed first.
_KIND_PRIORITY: dict[str, int] = {
    "alarm_site": 100,
    "array_size_hint": 95,
    "local_guard": 95,
    "guard": 90,
    "clamp": 90,
    "mask": 90,
    "loop_bound": 90,
    "declaration": 80,
    "write": 70,
    "arg_binding": 65,
    "value_site": 60,
    "path_step": 50,
    "call_edge": 40,
    "access": 35,
}


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


def _fact_key(f: Fact) -> tuple:
    return (f.line, f.kind, f.symbol or "", " ".join(f.quote.split())[:80])


def compact_facts(merged: MergedPack) -> list[dict[str, Any]]:
    """One row per unique fact with the path classes it applies to.

    The raw merged pack repeats global facts under every class and again in
    each explorer pack — that alone blew a 15-class alarm to ~260k chars.
    """
    rows: dict[tuple, dict[str, Any]] = {}
    all_ids = [c.class_id for c in merged.prep.path_classes]

    def add(f: Fact, cid: str | None) -> None:
        key = _fact_key(f)
        row = rows.get(key)
        if row is None:
            row = {
                "kind": f.kind,
                "function": f.function,
                "line": f.line,
                "quote": f.quote[:300],
                "symbol": f.symbol,
                "note": f.note,
                "verified": bool(f.verified),
                "_classes": set(),
            }
            rows[key] = row
        if cid:
            row["_classes"].add(cid)

    for cid, facts in merged.facts_by_class.items():
        for f in facts:
            add(f, cid)
    for f in [*merged.call_path.facts, *merged.var_value.facts]:
        add(f, None)

    out: list[dict[str, Any]] = []
    for row in rows.values():
        classes = row.pop("_classes")
        if all_ids and classes >= set(all_ids):
            row["path_classes"] = "all"
        else:
            row["path_classes"] = sorted(classes) or []
        out.append(row)

    out.sort(key=lambda r: (-_KIND_PRIORITY.get(str(r["kind"]), 20), r["line"]))
    return out


def build_brief(merged: MergedPack, *, max_chars: int | None = None) -> tuple[str, dict[str, Any]]:
    """Return (json_text, stats). Trims low-priority facts to fit ``max_chars``."""
    cfg_max = max_chars or pipeline_config().prover_brief_max_chars
    facts = compact_facts(merged)
    prep = merged.prep
    head = {
        "coverage": merged.coverage,
        "notes": merged.notes,
        "prep": {
            "order": prep.order,
            "enclosing_function": prep.enclosing_function,
            "alarm_line": prep.alarm_line,
            "index_expression": prep.index_expression,
            "index_origin": prep.index_origin,
            "array_name": prep.array_name,
            "array_size": prep.array_size,
            "local_guard": prep.local_guard.model_dump(),
            "path_classes": [
                {
                    "class_id": c.class_id,
                    "sequence": [s.function for s in c.sequence],
                    "members": len(c.member_path_ids),
                }
                for c in prep.path_classes
            ],
        },
    }
    total = len(facts)
    while True:
        payload = {**head, "facts": facts, "fact_count": total, "facts_trimmed": total - len(facts)}
        text = json.dumps(payload, indent=1, ensure_ascii=False)
        if len(text) <= cfg_max or len(facts) <= 8:
            return text, {"chars": len(text), "facts": len(facts), "trimmed": total - len(facts)}
        # drop the lowest-priority quarter
        facts = facts[: max(8, int(len(facts) * 0.75))]


def _to_report(agent: str, data: dict[str, Any], error: str | None = None) -> ProveReport:
    claim = str(data.get("claim") or "no_credible_case").lower()
    if claim not in {"tp", "fp", "no_credible_case"}:
        claim = "no_credible_case"
    if agent == "TP_PROVE" and claim == "fp":
        claim = "no_credible_case"
    if agent == "FP_PROVE" and claim == "tp":
        claim = "no_credible_case"

    def lit(key: str, allowed: set[str], default: str) -> str:
        val = str(data.get(key) or default).lower()
        return val if val in allowed else default

    return ProveReport(
        agent=agent,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        index_range=lit("index_range", {"constant", "guard_bounded", "unknown"}, "unknown"),  # type: ignore[arg-type]
        array_size=lit("array_size", {"known", "unknown"}, "unknown"),  # type: ignore[arg-type]
        witness=lit("witness", {"found", "none"}, "none"),  # type: ignore[arg-type]
        coverage=lit("coverage", {"full", "partial"}, "partial"),  # type: ignore[arg-type]
        findings=[x for x in (data.get("findings") or []) if isinstance(x, dict)],
        missing_evidence=[str(x) for x in (data.get("missing_evidence") or [])],
        addressed_path_classes=[str(x) for x in (data.get("addressed_path_classes") or [])],
        unaddressed_path_classes=[str(x) for x in (data.get("unaddressed_path_classes") or [])],
        rationale=str(data.get("rationale") or ""),
        error=error,
    )


def report_is_empty(report: ProveReport) -> bool:
    """A 'valid' JSON reply that carries no analysis is still a failure."""
    return (
        not report.rationale.strip()
        and not report.findings
        and not report.missing_evidence
        and not report.addressed_path_classes
    )


def _run_prove(
    agent_name: str,
    system: str,
    merged: MergedPack,
    bus: EventBus | None = None,
) -> ProveReport:
    brief, stats = build_brief(merged)
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
            activity=f"brief {stats['chars']} chars, {stats['facts']} facts"
            + (f" (trimmed {stats['trimmed']})" if stats["trimmed"] else ""),
        )

    def attempt(text: str, retry_note: str | None) -> tuple[ProveReport | None, str, str | None]:
        prompt_chars = len(system) + len(text) + 200
        llm = build_agent_llm(agent_name, prompt_chars=prompt_chars)
        human = "MERGED PACK:\n" + text
        if retry_note:
            human = retry_note + "\n\n" + human
        msg = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        try:
            data = _extract_json(content)
        except Exception as exc:  # noqa: BLE001
            return None, content, f"{type(exc).__name__}: {exc}"
        report = _to_report(agent_name, data)
        if report_is_empty(report):
            return None, content, "empty report (no rationale/findings/missing_evidence)"
        return report, content, None

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
        report, content, err = attempt(brief, None)
        if report is not None:
            return {"report": report.model_dump()}

        # One retry: half-size brief + explicit JSON-only reminder.
        if bus:
            bus.emit(
                "stage",
                agent=agent_name,
                stage="prove",
                title=f"{agent_name} retry",
                detail=f"first reply unusable: {err}",
                activity="retrying with compact brief + JSON-only reminder",
            )
        small, _ = build_brief(merged, max_chars=max(12000, stats["chars"] // 2))
        reminder = (
            "Your previous reply was NOT the required JSON object. Do not summarise the "
            "input. Reply with ONLY the JSON object described in your instructions."
        )
        report, content2, err2 = attempt(small, reminder)
        if report is not None:
            report = report.model_copy(update={"error": None})
            return {"report": report.model_dump()}

        failure = f"prover_failure: {err}; retry: {err2}"
        return {
            "report": _to_report(
                agent_name,
                {
                    "claim": "no_credible_case",
                    "coverage": "partial",
                    "missing_evidence": ["prover did not return a usable JSON report"],
                    "rationale": "PROVER FAILURE (not analysis). Raw reply head: "
                    + " ".join((content2 or content)[:400].split()),
                },
                error=failure,
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
            activity=f"claim={report.claim} witness={report.witness}"
            + (" ERROR" if report.error else ""),
        )
    return report


def run_tp_prove(merged: MergedPack, bus: EventBus | None = None) -> ProveReport:
    return _run_prove("TP_PROVE", prompts.TP_PROVE, merged, bus=bus)


def run_fp_prove(merged: MergedPack, bus: EventBus | None = None) -> ProveReport:
    return _run_prove("FP_PROVE", prompts.FP_PROVE, merged, bus=bus)
