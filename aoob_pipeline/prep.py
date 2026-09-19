"""Deterministic prep: path skeletons, path classes, local-guard check."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aoob_pipeline.schemas import (
    LocalGuardHit,
    PathClass,
    PathStep,
    PrepPack,
    RawPath,
    SymbolSkeleton,
)
from aoob_pipeline.source_index import SourceIndex, collapse_ws

_CALL_RE = re.compile(
    r"call#(?P<fn>[A-Za-z_]\w*)\s+at\s+[^:]+:(?P<line>\d+)",
    re.IGNORECASE,
)
_LOC_RE = re.compile(r":(?P<line>\d+)(?:\.\d+)?(?:-\d+)?$")
_GUARD_HINTS = re.compile(
    r"\b(if|while|for|switch)\b|<=|>=|<|>|&&|\|\||\bmin\b|\bmax\b|&\s*0x|%\s*\d+|clamp",
    re.IGNORECASE,
)


def _parse_location_line(location: str | None) -> int | None:
    if not location:
        return None
    match = _LOC_RE.search(location.strip())
    return int(match.group("line")) if match else None


def _stack_to_sequence(call_stack: list[str], function_sequence: list[str]) -> list[PathStep]:
    steps: list[PathStep] = []
    for entry in call_stack:
        match = _CALL_RE.search(entry)
        if match:
            steps.append(
                PathStep(function=match.group("fn"), call_site_line=int(match.group("line")))
            )
    if steps:
        return steps
    return [PathStep(function=name, call_site_line=None) for name in function_sequence]


def _symbol_from_info(info: dict[str, Any], *, role_fallback: str | None = None) -> SymbolSkeleton:
    occurrences = info.get("occurrences") or []
    write_lines = [
        int(o["line"])
        for o in occurrences
        if isinstance(o, dict) and o.get("access") in {"write", "declaration"} and o.get("line")
    ]
    guard_lines: list[int] = []
    for occ in occurrences:
        if not isinstance(occ, dict) or not occ.get("line"):
            continue
        text = occ.get("line_text") or ""
        access = (occ.get("access") or "").lower()
        if access == "read" and _GUARD_HINTS.search(text):
            guard_lines.append(int(occ["line"]))
        elif access in {"write", "declaration"}:
            continue
    used = [str(x) for x in (info.get("used_in_functions") or []) if x]
    return SymbolSkeleton(
        symbol_name=str(info.get("symbol_name") or info.get("array_name") or ""),
        role=str(info.get("role") or role_fallback or ""),
        kind=str(info.get("kind") or ""),
        declaration_line=info.get("declaration_line"),
        declaration_text=info.get("declaration_text"),
        array_dims=info.get("array_dims"),
        resolved_sizes=[int(x) for x in (info.get("resolved_sizes") or []) if str(x).isdigit() or isinstance(x, int)],
        size_known=bool(info.get("size_known")),
        index_expression=info.get("index_expression"),
        function_sequence=used,
        write_lines=write_lines,
        guard_candidate_lines=guard_lines,
    )


def _collect_symbols(alarm: dict[str, Any]) -> list[SymbolSkeleton]:
    out: list[SymbolSkeleton] = []
    seen: set[str] = set()
    primary = alarm.get("variable_info")
    if isinstance(primary, dict) and primary.get("symbol_name"):
        sym = _symbol_from_info(primary)
        out.append(sym)
        seen.add(sym.symbol_name)
    for key in ("variable_infos", "symbol_infos"):
        for info in alarm.get(key) or []:
            if not isinstance(info, dict):
                continue
            name = str(info.get("symbol_name") or "")
            if not name or name in seen:
                continue
            out.append(_symbol_from_info(info))
            seen.add(name)
    return out


def _local_guard_check(
    source: SourceIndex,
    *,
    enclosing: str | None,
    alarm_line: int | None,
    index_expression: str | None,
    array_size: int | None,
) -> LocalGuardHit:
    if not enclosing or not alarm_line:
        return LocalGuardHit(found=False, note="missing enclosing function or alarm line")
    span = source.function_span(enclosing, near_line=alarm_line)
    if span is None:
        return LocalGuardHit(found=False, note=f"could not resolve {enclosing}")
    # scan from function start to alarm line for bounds on index tokens
    tokens = re.findall(r"[A-Za-z_]\w*", index_expression or "")
    interesting = [t for t in tokens if t not in {"Mo_Inst_GetCoreIdx", "sizeof"}][:6]
    rows = source.get_lines(span.start_line, alarm_line)
    size_hint = str(array_size) if array_size is not None else None
    for line_no, text in rows:
        if not _GUARD_HINTS.search(text):
            continue
        if interesting and not any(tok in text for tok in interesting):
            continue
        if size_hint and size_hint in collapse_ws(text):
            return LocalGuardHit(
                found=True,
                function=enclosing,
                line=line_no,
                quote=text.strip(),
                note="index compared against declared size inside alarm function",
            )
        if re.search(r"(<\s*=?\s*\d+|&\s*0x[0-9A-Fa-f]+|%\s*\d+)", text):
            return LocalGuardHit(
                found=True,
                function=enclosing,
                line=line_no,
                quote=text.strip(),
                note="local clamp/mask/bound candidate on index-related tokens",
            )
    return LocalGuardHit(found=False, note="no local bound found in alarm function")


def _collapse_path_classes(raw_paths: list[RawPath], *, cap: int) -> tuple[list[PathClass], str]:
    buckets: dict[tuple[tuple[str, int | None], ...], list[RawPath]] = {}
    for path in raw_paths:
        key = tuple((s.function, s.call_site_line) for s in path.sequence)
        buckets.setdefault(key, []).append(path)

    classes: list[PathClass] = []
    for idx, (key, members) in enumerate(sorted(buckets.items(), key=lambda kv: -len(kv[1])), start=1):
        seq = [PathStep(function=fn, call_site_line=line) for fn, line in key]
        classes.append(
            PathClass(
                class_id=f"C{idx}",
                sequence=seq,
                member_path_ids=[m.path_id for m in members],
                representative_path_id=members[0].path_id,
            )
        )

    if len(classes) <= cap:
        return classes, "full"

    # Cluster by relevant suffix (last 3 edges) when over cap.
    suffix_map: dict[tuple[tuple[str, int | None], ...], list[PathClass]] = {}
    for cls in classes:
        suffix = tuple((s.function, s.call_site_line) for s in cls.sequence[-3:])
        suffix_map.setdefault(suffix, []).append(cls)

    clustered: list[PathClass] = []
    for idx, (_suffix, group) in enumerate(sorted(suffix_map.items(), key=lambda kv: -sum(len(c.member_path_ids) for c in kv[1])), start=1):
        if len(clustered) >= cap:
            break
        rep = max(group, key=lambda c: len(c.member_path_ids))
        member_ids: list[int] = []
        for g in group:
            member_ids.extend(g.member_path_ids)
        clustered.append(
            PathClass(
                class_id=f"K{idx}",
                sequence=rep.sequence,
                member_path_ids=sorted(set(member_ids)),
                representative_path_id=rep.representative_path_id,
                cluster_note=f"cluster of {len(group)} exact classes by last-3 suffix",
            )
        )
    return clustered, "partial"


def _unique_edges(classes: list[PathClass]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    edges: list[dict[str, Any]] = []
    for cls in classes:
        seq = cls.sequence
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            key = (a.function, a.call_site_line, b.function, b.call_site_line)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {
                    "caller": a.function,
                    "call_site_line": a.call_site_line,
                    "callee": b.function,
                    "path_class_id": cls.class_id,
                }
            )
    return edges


def build_prep(
    *,
    pver_id: str,
    order: str,
    alarm: dict[str, Any],
    source: SourceIndex,
    path_class_cap: int = 15,
) -> PrepPack:
    alarm_line = _parse_location_line(alarm.get("location"))
    raw_paths: list[RawPath] = []
    for path in alarm.get("paths") or []:
        if not isinstance(path, dict):
            continue
        seq = _stack_to_sequence(list(path.get("call_stack") or []), list(path.get("function_sequence") or []))
        raw_paths.append(
            RawPath(
                path_id=int(path.get("path_id") or len(raw_paths) + 1),
                sequence=seq,
                call_stack=list(path.get("call_stack") or []),
                alarm_text=path.get("alarm"),
                source_lines=list(path.get("source_lines") or []),
            )
        )

    path_classes, coverage_cap = _collapse_path_classes(raw_paths, cap=path_class_cap)
    symbols = _collect_symbols(alarm)
    primary = symbols[0] if symbols else None
    array_size = None
    if primary and primary.resolved_sizes:
        array_size = primary.resolved_sizes[0]
    elif primary and primary.size_known is False:
        array_size = None

    enclosing = alarm.get("enclosing_function")
    index_expr = (primary.index_expression if primary else None) or alarm.get("variable")
    local_guard = _local_guard_check(
        source,
        enclosing=enclosing,
        alarm_line=alarm_line,
        index_expression=index_expr,
        array_size=array_size,
    )

    notes: list[str] = []
    if coverage_cap == "partial":
        notes.append(f"path classes capped at {path_class_cap}; coverage marked partial")
    if local_guard.found:
        notes.append("local guard candidate found; path explore may be skipped for FP shortcut")

    return PrepPack(
        pver_id=pver_id,
        order=str(order),
        group_id=alarm.get("group_id"),
        location=alarm.get("location"),
        enclosing_function=enclosing,
        alarm_line=alarm_line,
        index_expression=index_expr,
        array_name=primary.symbol_name if primary else None,
        array_size=array_size,
        raw_paths=raw_paths,
        path_classes=path_classes,
        coverage_cap=coverage_cap,  # type: ignore[arg-type]
        symbols=symbols,
        local_guard=local_guard,
        skip_path_explore=bool(local_guard.found),
        edges=_unique_edges(path_classes),
        notes=notes,
    )


def _canonical_order(value: str) -> str:
    return str(value or "").strip().replace(",", "")


def _find_alarms_csv(pver_dir: Path) -> Path | None:
    for name in ("Full_alarms.csv", "fULL_ALARMS.csv", "full_alarms.csv"):
        path = pver_dir / name
        if path.is_file():
            return path
    matches = sorted(pver_dir.glob("*alarms*.csv")) + sorted(pver_dir.glob("*ALARMS*.csv"))
    return matches[0] if matches else None


def _location_for_astree_order(alarms_csv: Path, order: str) -> str | None:
    """Map Astree CSV ``Order`` (e.g. ``2,475``) → ``Location`` string."""
    import csv

    needle = _canonical_order(order)
    with alarms_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        first = stream.readline()
        if not first.lower().startswith("sep="):
            stream.seek(0)
        reader = csv.DictReader(stream, delimiter=";")
        for row in reader:
            if _canonical_order(row.get("Order") or "") == needle:
                loc = (row.get("Location") or "").strip()
                return loc or None
    return None


def load_alarm(
    variable_info_path: Path,
    order: str,
    *,
    alarms_csv: Path | None = None,
    pver_dir: Path | None = None,
) -> dict[str, Any]:
    """Load one enriched alarm.

    ``order`` may be:
    - JSON ``group_id`` (1..N), or
    - Astree CSV ``Order`` (e.g. ``2,475`` / ``2475``) — resolved via Location,
      same join the UI uses.
    """
    import json

    rows = json.loads(variable_info_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("variable info must be a list")
    needle = _canonical_order(order)

    for row in rows:
        if not isinstance(row, dict):
            continue
        gid = _canonical_order(str(row.get("group_id") or ""))
        if gid == needle:
            return row

    csv_path = alarms_csv
    if csv_path is None and pver_dir is not None:
        csv_path = _find_alarms_csv(pver_dir)
    if csv_path is None and variable_info_path.parent.is_dir():
        csv_path = _find_alarms_csv(variable_info_path.parent)

    if csv_path and csv_path.is_file():
        location = _location_for_astree_order(csv_path, order)
        if location:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("location") or "").strip() == location:
                    return row

    raise KeyError(
        f"alarm order {order!r} not found as group_id or Astree Order "
        f"in {variable_info_path}"
    )
