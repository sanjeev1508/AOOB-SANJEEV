"""Plain-code orchestrator for the five-agent AOOB triage pipeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from aoob_pipeline.artifacts import run_dir, write_json
from aoob_pipeline.config import pipeline_config
from aoob_pipeline.events import EventBus, preview_json
from aoob_pipeline.explore_graph import run_call_path_explore, run_var_value_explore
from aoob_pipeline.final_graph import run_final_classification
from aoob_pipeline.merge import merge_packs
from aoob_pipeline.prep import build_prep, load_alarm
from aoob_pipeline.prove_graph import run_fp_prove, run_tp_prove
from aoob_pipeline.schemas import ExplorePack, PipelineResult
from aoob_pipeline.source_index import SourceIndex
from aoob_pipeline.validate import validate_reports


def _pver_dir(root: Path, pver_id: str) -> Path:
    return root / "PVERs" / pver_id


def run_pipeline(
    *,
    pver_id: str,
    order: str,
    project_root: Path | None = None,
    parallel_explore: bool = True,
    parallel_prove: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> PipelineResult:
    cfg = pipeline_config(project_root)
    root = cfg.project_root
    pver = _pver_dir(root, pver_id)
    if not pver.is_dir():
        raise FileNotFoundError(f"PVER folder not found: {pver}")

    var_path = pver / "array_oob_variable_info.json"
    source_path = pver / "input.c"
    if not var_path.is_file():
        raise FileNotFoundError(f"missing {var_path}")
    if not source_path.is_file():
        raise FileNotFoundError(f"missing {source_path}")

    out = run_dir(pver, order, create=True)
    bus = EventBus(jsonl_path=out / "events.jsonl", on_event=on_event)

    bus.emit(
        "stage",
        stage="prep",
        title="Deterministic prep",
        activity="loading alarm + building path/symbol skeletons",
        detail=f"pver={pver_id} order={order}",
    )

    alarm = load_alarm(var_path, order, pver_dir=pver)
    source = SourceIndex(source_path)

    prep = build_prep(
        pver_id=pver_id,
        order=order,
        alarm=alarm,
        source=source,
        path_class_cap=cfg.path_class_cap,
    )
    write_json(out / "01_prep.json", prep)
    path_funcs = [s.function for c in prep.path_classes for s in c.sequence if s.function]
    bus.emit(
        "stage",
        stage="prep",
        title="Prep complete",
        output_preview=preview_json(
            {
                "group_id": prep.group_id,
                "path_classes": len(prep.path_classes),
                "edges": len(prep.edges),
                "coverage_cap": prep.coverage_cap,
                "local_guard": prep.local_guard.model_dump(),
                "array": prep.array_name,
                "array_size": prep.array_size,
            }
        ),
        functions=list(dict.fromkeys(path_funcs)),
        edges=[
            {"s": str(e.get("caller")), "t": str(e.get("callee"))}
            for e in prep.edges[:40]
            if e.get("caller") and e.get("callee")
        ],
        activity=f"{len(prep.path_classes)} path classes",
    )

    bus.emit(
        "stage",
        stage="explore",
        title="Explorers starting",
        detail="CALL_PATH_EXPLORE + VAR_VALUE_EXPLORE (LangGraph + tools)",
        activity="explore fan-out",
        functions=list(dict.fromkeys(path_funcs)),
    )

    if parallel_explore:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_cp = pool.submit(
                run_call_path_explore, prep, source, max_rounds=cfg.max_tool_rounds, bus=bus
            )
            fut_vv = pool.submit(
                run_var_value_explore, prep, source, max_rounds=cfg.max_tool_rounds, bus=bus
            )
            call_pack = fut_cp.result()
            var_pack = fut_vv.result()
    else:
        call_pack = run_call_path_explore(
            prep, source, max_rounds=cfg.max_tool_rounds, bus=bus
        )
        var_pack = run_var_value_explore(
            prep, source, max_rounds=cfg.max_tool_rounds, bus=bus
        )

    write_json(out / "02_call_path_explore.json", call_pack)
    write_json(out / "02_var_value_explore.json", var_pack)

    bus.emit(
        "stage",
        stage="merge",
        title="Merge + snippet check",
        activity="attaching facts to path classes",
    )
    merged = merge_packs(prep, call_pack, var_pack, source)
    write_json(out / "03_merged.json", merged)
    bus.emit(
        "stage",
        stage="merge",
        title="Merged pack ready",
        output_preview=preview_json(
            {
                "coverage": merged.coverage,
                "call_facts": len(merged.call_path.facts),
                "var_facts": len(merged.var_value.facts),
                "notes": merged.notes,
            }
        ),
        activity=f"coverage={merged.coverage}",
    )

    bus.emit(
        "stage",
        stage="prove",
        title="Provers starting",
        detail="TP_PROVE + FP_PROVE (no tools)",
        activity="prove fan-out",
    )
    if parallel_prove:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_tp = pool.submit(run_tp_prove, merged, bus)
            fut_fp = pool.submit(run_fp_prove, merged, bus)
            tp = fut_tp.result()
            fp = fut_fp.result()
    else:
        tp = run_tp_prove(merged, bus=bus)
        fp = run_fp_prove(merged, bus=bus)

    write_json(out / "04_tp_prove.json", tp)
    write_json(out / "04_fp_prove.json", fp)

    bus.emit(
        "stage",
        stage="validate",
        title="Evidence validator",
        activity="dropping uncited claims",
    )
    validated = validate_reports(merged, tp, fp, source)
    write_json(out / "05_validated.json", validated)
    bus.emit(
        "stage",
        stage="validate",
        title="Validation done",
        output_preview=preview_json(
            {
                "dropped": validated.dropped_claims,
                "tp_claim": validated.tp.claim,
                "fp_claim": validated.fp.claim,
                "fp_unaddressed": validated.fp.unaddressed_path_classes,
            }
        ),
        activity="reports scrubbed",
    )

    bus.emit(
        "stage",
        stage="final",
        title="Final classification",
        detail=f"multi-run ×{cfg.final_runs}",
        activity="adjudicating",
    )
    verdict = run_final_classification(validated, runs=cfg.final_runs, bus=bus)
    write_json(out / "06_final_vote.json", verdict)

    if verdict.reexplore_requested and cfg.max_reexplore >= 1 and verdict.reexplore_focus:
        focus = verdict.reexplore_focus
        bus.emit(
            "stage",
            stage="reexplore",
            title="Bounded re-explore",
            detail=focus,
            activity="one re-explore pass",
        )
        call2 = run_call_path_explore(
            prep, source, max_rounds=cfg.max_tool_rounds, focus=focus, bus=bus
        )
        var2 = run_var_value_explore(
            prep, source, max_rounds=cfg.max_tool_rounds, focus=focus, bus=bus
        )
        write_json(out / "07_reexplore_call_path.json", call2)
        write_json(out / "07_reexplore_var_value.json", var2)
        call_pack = ExplorePack(
            agent=call_pack.agent,
            facts=[*call_pack.facts, *call2.facts],
            notes=[*call_pack.notes, *call2.notes, f"reexplore:{focus}"],
            error=call2.error or call_pack.error,
        )
        var_pack = ExplorePack(
            agent=var_pack.agent,
            facts=[*var_pack.facts, *var2.facts],
            notes=[*var_pack.notes, *var2.notes, f"reexplore:{focus}"],
            error=var2.error or var_pack.error,
        )
        merged = merge_packs(prep, call_pack, var_pack, source)
        write_json(out / "08_merged_after_reexplore.json", merged)
        if parallel_prove:
            with ThreadPoolExecutor(max_workers=2) as pool:
                tp = pool.submit(run_tp_prove, merged, bus).result()
                fp = pool.submit(run_fp_prove, merged, bus).result()
        else:
            tp = run_tp_prove(merged, bus=bus)
            fp = run_fp_prove(merged, bus=bus)
        write_json(out / "09_tp_prove.json", tp)
        write_json(out / "09_fp_prove.json", fp)
        validated = validate_reports(merged, tp, fp, source)
        write_json(out / "10_validated.json", validated)
        verdict = run_final_classification(validated, runs=cfg.final_runs, bus=bus)
        verdict = verdict.model_copy(update={"reexplore_requested": False})
        write_json(out / "11_final.json", verdict)

    result = PipelineResult(
        pver_id=pver_id,
        order=str(order),
        run_dir=str(out),
        verdict=verdict,
        prep=prep,
        merged_coverage=merged.coverage,  # type: ignore[arg-type]
        tp=validated.tp,
        fp=validated.fp,
    )
    write_json(out / "result.json", result)
    bus.emit(
        "done",
        stage="done",
        title=f"Verdict: {verdict.label}",
        output_preview=preview_json(result.model_dump()),
        activity=verdict.label,
        functions=list(dict.fromkeys(path_funcs)),
    )
    return result
