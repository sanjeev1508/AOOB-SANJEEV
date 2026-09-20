"""Structured packs exchanged between pipeline stages."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PathStep(BaseModel):
    function: str
    call_site_line: int | None = None


class RawPath(BaseModel):
    path_id: int
    sequence: list[PathStep] = Field(default_factory=list)
    call_stack: list[str] = Field(default_factory=list)
    alarm_text: str | None = None
    source_lines: list[str] = Field(default_factory=list)


class PathClass(BaseModel):
    class_id: str
    sequence: list[PathStep]
    member_path_ids: list[int]
    representative_path_id: int
    cluster_note: str | None = None


class SymbolSkeleton(BaseModel):
    symbol_name: str
    role: str | None = None
    kind: str | None = None
    declaration_line: int | None = None
    declaration_text: str | None = None
    array_dims: str | None = None
    resolved_sizes: list[int] = Field(default_factory=list)
    size_known: bool = False
    index_expression: str | None = None
    function_sequence: list[str] = Field(default_factory=list)
    write_lines: list[int] = Field(default_factory=list)
    guard_candidate_lines: list[int] = Field(default_factory=list)
    # All analyzer occurrence lines (reads/writes/decls) for ±3 window packages
    key_lines: list[int] = Field(default_factory=list)


class LocalGuardHit(BaseModel):
    found: bool = False
    function: str | None = None
    line: int | None = None
    quote: str | None = None
    note: str | None = None


class PrepPack(BaseModel):
    pver_id: str
    order: str
    group_id: int | None = None
    location: str | None = None
    enclosing_function: str | None = None
    alarm_line: int | None = None
    index_expression: str | None = None
    # parameter | local | global | unknown — whether callers can influence the index
    index_origin: str = "unknown"
    # functions called on the RHS of assignments to index tokens (value origins)
    value_origin_callees: list[str] = Field(default_factory=list)
    array_name: str | None = None
    array_size: int | None = None
    raw_paths: list[RawPath] = Field(default_factory=list)
    path_classes: list[PathClass] = Field(default_factory=list)
    coverage_cap: Literal["full", "partial"] = "full"
    symbols: list[SymbolSkeleton] = Field(default_factory=list)
    local_guard: LocalGuardHit = Field(default_factory=LocalGuardHit)
    skip_path_explore: bool = False
    edges: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    kind: str
    function: str
    line: int
    quote: str
    symbol: str | None = None
    path_class_id: str | None = None
    note: str | None = None
    verified: bool | None = None


class ExplorePack(BaseModel):
    agent: str
    facts: list[Fact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class MergedPack(BaseModel):
    prep: PrepPack
    call_path: ExplorePack
    var_value: ExplorePack
    facts_by_class: dict[str, list[Fact]] = Field(default_factory=dict)
    coverage: Literal["full", "partial"] = "full"
    notes: list[str] = Field(default_factory=list)


class ProveReport(BaseModel):
    agent: Literal["TP_PROVE", "FP_PROVE"]
    claim: Literal["tp", "fp", "no_credible_case"] = "no_credible_case"
    index_range: Literal["constant", "guard_bounded", "unknown"] = "unknown"
    array_size: Literal["known", "unknown"] = "unknown"
    witness: Literal["found", "none"] = "none"
    coverage: Literal["full", "partial"] = "partial"
    findings: list[dict[str, Any]] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    addressed_path_classes: list[str] = Field(default_factory=list)
    unaddressed_path_classes: list[str] = Field(default_factory=list)
    rationale: str = ""
    error: str | None = None


class ValidatedReports(BaseModel):
    tp: ProveReport
    fp: ProveReport
    dropped_claims: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class FinalVerdict(BaseModel):
    label: Literal["TP", "FP", "uncertain"]
    rationale: str
    tp_summary: str = ""
    fp_summary: str = ""
    reexplore_requested: bool = False
    reexplore_focus: str | None = None
    votes: list[str] = Field(default_factory=list)
    run_details: list[dict[str, Any]] = Field(default_factory=list)
    hard_gates: list[str] = Field(default_factory=list)


class PipelineResult(BaseModel):
    pver_id: str
    order: str
    run_dir: str
    verdict: FinalVerdict
    prep: PrepPack
    merged_coverage: Literal["full", "partial"] = "full"
    tp: ProveReport | None = None
    fp: ProveReport | None = None
