"""FastAPI service for browsing Astree alarm and variable-analysis data."""

from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
from bisect import bisect_right
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PVER_ROOT = Path(os.getenv("ASTREE_PVER_ROOT", PROJECT_ROOT / "PVERs")).expanduser()
ANALYZER_PATH = PROJECT_ROOT / "array_oob_analyzer.py"
OUTPUT_NAME = "array_oob_variable_info.json"
CFG_OUTPUT_NAME = "full_control_flow_graph.json"
PVER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_ALARMS_PATH = PROJECT_ROOT / "Full_alarms.csv"
DEFAULT_VARIABLE_INFO_PATH = PROJECT_ROOT / OUTPUT_NAME
DEFAULT_SOURCE_PATH = PROJECT_ROOT / "input.c"


def _setting_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default


def _canonical_order(value: str) -> str:
    """Compare order IDs consistently with Astree's thousands separators."""
    return value.strip().replace(",", "")


def _read_alarm_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        first = stream.readline()
        # The export starts with the Excel-style separator declaration ``sep=;``.
        if not first.lower().startswith("sep="):
            stream.seek(0)
        reader = csv.DictReader(stream, delimiter=";")
        return [{key.strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def _read_variable_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    if not isinstance(payload, list):
        raise ValueError("variable info JSON must contain an array")
    return [row for row in payload if isinstance(row, dict)]


# Control-flow and declaration keywords that look like calls but never name a function.
_C_KEYWORDS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "default",
        "return", "goto", "break", "continue", "sizeof", "typedef", "struct",
        "union", "enum", "static", "const", "volatile", "inline", "extern",
        "register", "auto", "signed", "unsigned", "void", "char", "short",
        "int", "long", "float", "double", "_Bool", "_Atomic", "restrict",
        "asm", "__asm", "__asm__", "__inline", "__inline__", "__attribute__",
        "__extension__", "defined",
    }
)

# Comments, literals and preprocessor lines are blanked before parsing so their
# braces/parentheses cannot confuse the bracket matching below.
_NON_CODE_RE = re.compile(
    r"/\*.*?\*/"
    r"|//(?:\\\r?\n|[^\n])*"
    r"|\"(?:\\.|[^\"\\\n])*\""
    r"|'(?:\\.|[^'\\\n])*'"
    r"|^[ \t]*#(?:\\\r?\n|[^\n])*",
    re.DOTALL | re.MULTILINE,
)
_CANDIDATE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_PAREN_RE = re.compile(r"[()]")
_BRACE_RE = re.compile(r"[{}]")

# How far back (in lines) a return type may sit above the function name.
_MAX_RETURN_TYPE_LINES = 5


def _blank_non_code(text: str) -> str:
    """Replace comments/literals/directives with spaces, keeping every offset."""

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _NON_CODE_RE.sub(blank, text)


def _match_bracket(code: str, start: int, pattern: re.Pattern[str], opener: str) -> int | None:
    """Return the offset of the bracket closing the one at ``start``."""
    depth = 0
    for match in pattern.finditer(code, start):
        if match.group() == opener:
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return match.start()
    return None


def _definition_brace(code: str, index: int) -> int | None:
    """Return the body brace following a signature, or ``None`` for declarations.

    Only whitespace, qualifiers/attributes and balanced parenthesis groups
    (e.g. ``__attribute__((noinline))``) may sit between ``)`` and ``{``.
    """
    limit = min(len(code), index + 2000)
    i = index
    while i < limit:
        char = code[i]
        if char.isspace():
            i += 1
        elif char == "{":
            return i
        elif char == "_" or char.isalpha():
            while i < limit and (code[i].isalnum() or code[i] == "_"):
                i += 1
        elif char == "(":
            closing = _match_bracket(code, i, _PAREN_RE, "(")
            if closing is None or closing >= limit:
                return None
            i = closing + 1
        else:
            # ``;`` (prototype) or anything else means this is not a definition.
            return None
    return None


def _definition_start(code: str, name_start: int, floor: int) -> int:
    """Walk back over the return type so snippets start at the declaration."""
    i = name_start - 1
    while i >= floor:
        char = code[i]
        if char.isspace() or char.isalnum() or char in "_*&":
            i -= 1
            continue
        break
    start = i + 1
    while start < name_start and code[start].isspace():
        start += 1
    return start


def _read_function_snippets(path: Path) -> dict[str, dict[str, Any]]:
    """Index function bodies so graph-node clicks can show real source text.

    The parser works on raw offsets instead of single lines so definitions whose
    name, parameter list and brace are spread over several lines (very common in
    preprocessed AUTOSAR code) are indexed as well.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    code = _blank_non_code(text)
    lines = [line.rstrip("\r") for line in text.split("\n")]

    line_starts = [0]
    line_starts.extend(match.end() for match in re.finditer("\n", code))

    def line_of(offset: int) -> int:
        return bisect_right(line_starts, offset) - 1

    snippets: dict[str, dict[str, Any]] = {}
    position = 0
    while True:
        match = _CANDIDATE_RE.search(code, position)
        if match is None:
            break
        position = match.end()

        name = match.group(1)
        if name in _C_KEYWORDS:
            continue

        close_paren = _match_bracket(code, match.end() - 1, _PAREN_RE, "(")
        if close_paren is None:
            continue

        open_brace = _definition_brace(code, close_paren + 1)
        if open_brace is None:
            continue

        close_brace = _match_bracket(code, open_brace, _BRACE_RE, "{")
        if close_brace is None:
            continue

        name_line = line_of(match.start())
        floor = line_starts[max(0, name_line - _MAX_RETURN_TYPE_LINES)]
        start_line = line_of(_definition_start(code, match.start(), floor))
        end_line = line_of(close_brace)

        if name not in snippets:
            snippets[name] = {
                "start_line": start_line + 1,
                "end_line": end_line + 1,
                "text": "\n".join(lines[start_line : end_line + 1]),
            }

        # Definitions never nest, so continue after the body.
        position = close_brace + 1

    return snippets


def _display_variable(row: dict[str, Any]) -> str | None:
    name = row.get("variable")
    if name:
        return str(name)
    info = row.get("variable_info") or {}
    symbol = info.get("symbol_name") or info.get("array_name")
    if not symbol:
        return None
    index = info.get("index_expression")
    return f"{symbol}[{index}]" if index else str(symbol)


def _location_line(location: str) -> int | None:
    match = re.search(r":(\d+)\.", location)
    return int(match.group(1)) if match else None


def _alarm_function(record: dict[str, Any]) -> str | None:
    named = record.get("enclosing_function")
    if named:
        return str(named)
    line = _location_line(str(record.get("location") or ""))
    info = record.get("variable_info") or {}
    for occurrence in info.get("occurrences") or []:
        function = occurrence.get("function")
        if function and function != "global" and occurrence.get("line") == line:
            return str(function)
    for function in info.get("used_in_functions") or []:
        if function and function != "global":
            return str(function)
    return None


def _index_callers(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    callers: dict[str, list[str]] = {name: [] for name in graph}
    for caller, callees in graph.items():
        for callee in callees:
            callers.setdefault(callee, []).append(caller)
    return callers


def _cfg_neighborhood(function: str) -> dict[str, Any] | None:
    if not function or _cfg_graph is None:
        return None
    return {
        "function": function,
        "callers": list((_cfg_callers or {}).get(function, [])),
        "callees": list(_cfg_graph.get(function, [])),
    }


def _order_functions_by_cfg(
    names: list[str],
    *,
    leaf: str | None = None,
    path: list[str] | None = None,
) -> list[str]:
    """Declaration first, then callers before callees, alarm leaf last."""
    unique: list[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    declaration = [name for name in unique if name == "global"]
    funcs = [name for name in unique if name != "global"]
    if not funcs:
        return declaration
    cfg = _cfg_graph or {}
    callers = _cfg_callers or {}
    funcset = set(funcs)
    incoming = {name: 0 for name in funcs}
    outgoing: dict[str, list[str]] = {name: [] for name in funcs}
    for caller in funcs:
        for callee in cfg.get(caller, []):
            if callee in funcset and callee != caller:
                outgoing[caller].append(callee)
                incoming[callee] += 1
    path_pos = {name: index for index, name in enumerate(path or []) if name in funcset}
    dist_from_leaf: dict[str, int] = {}
    if leaf:
        queue = [leaf]
        dist_from_leaf[leaf] = 0
        seen = {leaf}
        for node in queue:
            for pred in callers.get(node, []):
                if pred in funcset and pred not in seen:
                    seen.add(pred)
                    dist_from_leaf[pred] = dist_from_leaf[node] + 1
                    queue.append(pred)

    def priority(name: str) -> tuple[int, int, str]:
        if name in path_pos:
            return (0, path_pos[name], name)
        if name in dist_from_leaf:
            return (1, -dist_from_leaf[name], name)
        return (2, 0, name)

    ready = sorted((name for name in funcs if incoming[name] == 0), key=priority)
    remaining = dict(incoming)
    ordered: list[str] = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for nxt in outgoing[node]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=priority)
    leftover = [name for name in funcs if name not in ordered]
    leftover.sort(key=priority)
    ordered.extend(leftover)
    if leaf and leaf in ordered:
        ordered = [name for name in ordered if name != leaf] + [leaf]
    return declaration + ordered


def _info_function_names(info: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for name in info.get("used_in_functions") or []:
        if name:
            names.append(str(name))
    for occurrence in info.get("occurrences") or []:
        if isinstance(occurrence, dict):
            names.append(str(occurrence.get("function") or "global"))
    return names


def _enrich_dataflow_order(payload: dict[str, Any]) -> None:
    highlight = payload.get("graph_highlight") or {}
    leaf = highlight.get("center") or payload.get("enclosing_function")
    path = list(highlight.get("function_sequence") or highlight.get("cf_nodes") or [])
    infos = [
        payload.get("variable_info"),
        *(payload.get("variable_infos") or []),
        *(payload.get("symbol_infos") or []),
    ]
    for info in infos:
        if not isinstance(info, dict):
            continue
        ordered = _order_functions_by_cfg(_info_function_names(info), leaf=leaf, path=path)
        info["used_in_functions_ordered"] = ordered
        if ordered:
            info["used_in_functions"] = ordered


def _symbol_functions(record: dict[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    info = record.get("variable_info") or {}
    for source in (info.get("used_in_functions") or [], info.get("occurrences") or []):
        values = source if isinstance(source, list) else []
        for item in values:
            name = item if isinstance(item, str) else item.get("function")
            if name and name != "global" and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _compute_sigma_layout(graph: dict[str, list[str]], seed: int = 42) -> dict[str, tuple[float, float]]:
    """Hub-ring + BFS spiral layout, same structure as cf_viz.export.compute_layout."""
    nodes = list(graph)
    if not nodes:
        return {}
    und: dict[str, set[str]] = {name: set() for name in nodes}
    for caller, callees in graph.items():
        for callee in callees:
            und.setdefault(caller, set()).add(callee)
            und.setdefault(callee, set()).add(caller)
    degrees = sorted(((name, len(und.get(name, ()))) for name in und), key=lambda item: item[1], reverse=True)
    hubs = [name for name, _ in degrees[: min(24, len(degrees))]]
    rng = random.Random(seed)
    pos: dict[str, tuple[float, float]] = {}
    hub_r = 1200.0
    for index, hub in enumerate(hubs):
        angle = (2 * math.pi * index) / max(1, len(hubs))
        pos[hub] = (hub_r * math.cos(angle), hub_r * math.sin(angle))
    assigned = set(pos)
    queue: list[tuple[str, str, int]] = []
    for hub in hubs:
        for neighbor in und.get(hub, ()):
            if neighbor not in assigned:
                queue.append((neighbor, hub, 1))
    while queue:
        node, root, depth = queue.pop(0)
        if node in assigned:
            continue
        rx, ry = pos[root]
        angle = rng.random() * 2 * math.pi
        radius = 40.0 * depth + rng.random() * 28.0
        pos[node] = (rx + radius * math.cos(angle), ry + radius * math.sin(angle))
        assigned.add(node)
        if depth < 8:
            for neighbor in und.get(node, ()):
                if neighbor not in assigned:
                    queue.append((neighbor, root, depth + 1))
    leftovers = [name for name in und if name not in pos]
    outer = 1600.0
    for index, name in enumerate(leftovers):
        angle = (2 * math.pi * index) / max(1, len(leftovers))
        jitter = rng.random() * 120.0
        pos[name] = ((outer + jitter) * math.cos(angle), (outer + jitter) * math.sin(angle))
    return pos


def _build_full_graph(graph: dict[str, list[str]], *, pver_id: str, cfg_path: Path) -> dict[str, Any]:
    pos = _compute_sigma_layout(graph)
    indeg: dict[str, int] = {}
    for callees in graph.values():
        for callee in callees:
            indeg[callee] = indeg.get(callee, 0) + 1
    nodes = []
    for name, callees in graph.items():
        degree = len(callees) + indeg.get(name, 0)
        x, y = pos.get(name, (0.0, 0.0))
        nodes.append({"id": name, "x": round(x, 2), "y": round(y, 2), "s": round(1.6 + min(6.0, math.sqrt(degree)), 2)})
    seen = {item["id"] for item in nodes}
    for name, (x, y) in pos.items():
        if name not in seen:
            nodes.append({"id": name, "x": round(x, 2), "y": round(y, 2), "s": 1.6})
    edges = []
    for caller, callees in graph.items():
        counts: dict[str, int] = {}
        for callee in callees:
            counts[callee] = counts.get(callee, 0) + 1
        for callee, weight in counts.items():
            edges.append({"id": f"{caller}->{callee}", "s": caller, "t": callee, "w": weight})
    return {
        "pver": pver_id,
        "file": CFG_OUTPUT_NAME,
        "path": str(cfg_path),
        "version": 2,
        "renderer": "sigma-webgl",
        "nodes": nodes,
        "edges": edges,
        "stats": {"nodes": len(nodes), "edges": len(edges)},
        "function_count": len(nodes),
        "cf_edge_count": len(edges),
        "df_edge_count": 0,
    }


_CALL_NAME_RE = re.compile(r"^call#(.+?)(?:\||\s+at\s)")


def _bare_function(name: str) -> str:
    return name.split("|", 1)[0].strip()


def _path_functions(path: dict[str, Any]) -> list[str]:
    raw = [_bare_function(str(name)) for name in (path.get("function_sequence") or []) if name]
    if not raw:
        for entry in path.get("call_stack") or []:
            match = _CALL_NAME_RE.match(str(entry))
            if match:
                raw.append(_bare_function(match.group(1)))
    collapsed: list[str] = []
    for name in raw:
        if name and (not collapsed or collapsed[-1] != name):
            collapsed.append(name)
    return collapsed


def _graph_highlight(record: dict[str, Any]) -> dict[str, Any]:
    center = record.get("enclosing_function") or _alarm_function(record)
    symbol_nodes = _symbol_functions(record)
    unique_paths: list[list[str]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for path in record.get("paths") or []:
        if not isinstance(path, dict):
            continue
        sequence = tuple(_path_functions(path))
        if sequence and sequence not in seen_paths:
            seen_paths.add(sequence)
            unique_paths.append(list(sequence))
    if not unique_paths and center:
        callers = _cfg_callers or {}
        chain = [center]
        seen = {center}
        current = center
        while len(chain) < 12:
            preds = [name for name in callers.get(current, []) if name not in seen]
            if not preds:
                break
            current = preds[0]
            seen.add(current)
            chain.append(current)
        unique_paths = [list(reversed(chain))]
    all_nodes: list[str] = []
    seen_nodes: set[str] = set()
    for sequence in unique_paths:
        for name in sequence:
            if name not in seen_nodes:
                seen_nodes.add(name)
                all_nodes.append(name)
    firsts = {sequence[0] for sequence in unique_paths if sequence}
    lasts = {sequence[-1] for sequence in unique_paths if sequence}
    roles: dict[str, str] = {}
    for name in all_nodes:
        if name in firsts and name in lasts:
            roles[name] = "origin_and_alarm"
        elif name == center or name in lasts:
            roles[name] = "alarm"
        elif name in firsts:
            roles[name] = "origin"
        else:
            roles[name] = "hop"
    primary = []
    if unique_paths:
        primary = max(unique_paths, key=lambda item: (item[-1] == center if item else False, len(item)))
    segments = []
    edge_ids: list[str] = []
    cf_edges: list[list[str]] = []
    cfg = _cfg_graph or {}
    for sequence in unique_paths:
        for source, target in zip(sequence, sequence[1:]):
            kind = "direct" if target in cfg.get(source, []) else "path"
            edge_id = f"{source}->{target}"
            if edge_id not in edge_ids:
                edge_ids.append(edge_id)
                cf_edges.append([source, target])
            segments.append({"from": source, "to": target, "kind": kind, "nodes": [source, target], "edges": [edge_id]})
    return {
        "center": center,
        "cf_nodes": all_nodes,
        "cf_edges": cf_edges,
        "symbol_nodes": symbol_nodes,
        "function_sequence": primary,
        "function_sequences": unique_paths,
        "path_count": len(unique_paths),
        "function_roles": roles,
        "highlight_nodes": all_nodes,
        "highlight_edge_ids": edge_ids,
        "segments": segments,
        "first_access": {"function": primary[0]} if primary else None,
    }


class AlarmStore:
    """Load the two source exports once and expose merged alarm records."""

    def __init__(self, alarms_path: Path, variable_info_path: Path, source_path: Path | None = None) -> None:
        self.alarms_path = alarms_path
        self.variable_info_path = variable_info_path
        csv_rows = _read_alarm_rows(alarms_path)
        variable_rows = _read_variable_rows(variable_info_path)
        snippets = _read_function_snippets(source_path) if source_path and source_path.exists() else {}
        self.snippets = snippets
        by_location = {
            str(row.get("location", "")).strip(): row for row in variable_rows
        }
        self.records: list[dict[str, Any]] = []
        self.by_order: dict[str, dict[str, Any]] = {}

        for alarm in csv_rows:
            variable = by_location.get(alarm.get("Location", ""))
            if variable is None:
                variable = {}

            order_id = alarm.get("Order", "")
            record = {
                "order_id": order_id,
                "order_key": _canonical_order(order_id),
                "type": alarm.get("Type", ""),
                "category": alarm.get("Category", ""),
                "location": alarm.get("Location", ""),
                "classification": alarm.get("Classification", ""),
                "comment": alarm.get("Comment", ""),
                "message": alarm.get("Message", ""),
                "group_id": variable.get("group_id"),
                "variable": _display_variable(variable),
                "path_count": variable.get("no_of_paths", len(variable.get("paths", []))),
                "function_names": self._function_names(variable.get("paths", [])),
                "variable_info": variable.get("variable_info"),
                "variable_infos": variable.get("variable_infos", []),
                "symbol_infos": variable.get("symbol_infos", []),
                "paths": variable.get("paths", []),
                "enclosing_function": variable.get("enclosing_function"),
                "function_snippets": {
                    name.split("|", 1)[0]: snippets.get(name.split("|", 1)[0])
                    for path in variable.get("paths", [])
                    for name in path.get("function_sequence", [])
                },
            }
            record["alarm"] = {
                "order": alarm.get("Order", ""),
                "type": alarm.get("Type", ""),
                "category": alarm.get("Category", ""),
                "location": alarm.get("Location", ""),
                "message": alarm.get("Message", ""),
            }
            record["group"] = variable or None
            self.records.append(record)
            self.by_order[record["order_key"]] = record

    @staticmethod
    def _function_names(paths: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for path in paths:
            for entry in path.get("function_sequence", []):
                if entry not in names:
                    names.append(entry)
        return names

    def summaries(self) -> list[dict[str, Any]]:
        return [
            {
                key: record[key]
                for key in (
                    "order_id",
                    "type",
                    "category",
                    "location",
                    "classification",
                    "comment",
                    "message",
                    "group_id",
                    "variable",
                    "path_count",
                    "function_names",
                )
            }
            for record in self.records
        ]

    def get(self, order_id: str) -> dict[str, Any] | None:
        return self.by_order.get(_canonical_order(order_id))


def _build_store() -> AlarmStore:
    return AlarmStore(
        _setting_path("ASTREE_ALARMS_PATH", DEFAULT_ALARMS_PATH),
        _setting_path("ASTREE_VARIABLE_INFO_PATH", DEFAULT_VARIABLE_INFO_PATH),
        _setting_path("ASTREE_SOURCE_PATH", DEFAULT_SOURCE_PATH),
    )


def _safe_pver_id(pver_id: str) -> str:
    value = pver_id.strip()
    if not PVER_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid PVER id: {pver_id}")
    return value


def _pver_dir(pver_id: str) -> Path:
    folder = (PVER_ROOT / _safe_pver_id(pver_id)).resolve()
    root = PVER_ROOT.resolve()
    if folder != root and root not in folder.parents:
        raise HTTPException(status_code=400, detail="PVER path is outside the PVER root")
    return folder


def _find_named(folder: Path, names: tuple[str, ...]) -> Path | None:
    lower = {path.name.lower(): path for path in folder.iterdir() if path.is_file()}
    for name in names:
        match = lower.get(name.lower())
        if match:
            return match
    return None


def _find_alarms(folder: Path) -> Path | None:
    exact = _find_named(folder, ("Full_alarms.csv", "fULL_ALARMS.csv", "full_alarms.csv"))
    if exact:
        return exact
    matches = [path for path in folder.glob("*.csv") if "alarm" in path.name.lower()]
    return matches[0] if matches else None


def _find_source(folder: Path) -> Path | None:
    exact = _find_named(folder, ("input.c", "AP1_bc_with_context.c"))
    if exact:
        return exact
    c_files = sorted(folder.glob("*.c"))
    return c_files[0] if c_files else None


def _find_log(folder: Path) -> Path | None:
    return _find_named(folder, ("messeges.txt", "messages.txt", "message.txt"))


def _output_path(folder: Path) -> Path:
    return folder / OUTPUT_NAME


def _cfg_path(folder: Path) -> Path:
    return folder / CFG_OUTPUT_NAME


def _read_cfg(path: Path) -> dict[str, list[str]]:
    with path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("control-flow graph JSON must be an object of caller -> callees")
    graph: dict[str, list[str]] = {}
    for caller, callees in payload.items():
        if not isinstance(caller, str):
            continue
        if isinstance(callees, list):
            graph[caller] = [str(name) for name in callees]
        elif callees is None:
            graph[caller] = []
    return graph


def _count_csv_rows(path: Path) -> int:
    try:
        return len(_read_alarm_rows(path))
    except OSError:
        return 0


def _job_snapshot(pver_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = dict(_jobs.get(pver_id, {}))
    return job


def _describe_pver(folder: Path) -> dict[str, Any]:
    alarms = _find_alarms(folder)
    source = _find_source(folder)
    output = _output_path(folder)
    cfg = _cfg_path(folder)
    log = _find_log(folder)
    job = _job_snapshot(folder.name)
    has_output = output.exists()
    has_cfg = cfg.exists()
    status = job.get("status")
    if status == "running":
        state = "building"
    elif status == "error":
        state = "error"
    elif has_cfg:
        state = "ready"
    elif alarms and source:
        state = "needs_build"
    else:
        state = "incomplete"
    return {
        "id": folder.name,
        "name": folder.name,
        "path": str(folder),
        "alarms_file": alarms.name if alarms else None,
        "source_file": source.name if source else None,
        "log_file": log.name if log else None,
        "output_file": OUTPUT_NAME if has_output else None,
        "cfg_file": CFG_OUTPUT_NAME if has_cfg else None,
        "has_alarms": bool(alarms),
        "has_source": bool(source),
        "has_output": has_output,
        "has_cfg": has_cfg,
        "alarm_count": _count_csv_rows(alarms) if alarms else 0,
        "status": state,
        "active": _active_pver == folder.name,
        "message": job.get("message", ""),
    }


def list_pver_folders() -> list[dict[str, Any]]:
    if not PVER_ROOT.exists():
        return []
    folders = [path for path in PVER_ROOT.iterdir() if path.is_dir() and not path.name.startswith(".")]
    return [_describe_pver(path) for path in sorted(folders, key=lambda item: item.name.lower())]


def _activate_pver(pver_id: str) -> AlarmStore | None:
    global _store, _cfg_graph, _cfg_callers, _full_graph, _active_pver
    folder = _pver_dir(pver_id)
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"PVER {pver_id} was not found")
    cfg = _cfg_path(folder)
    if not cfg.exists():
        raise HTTPException(status_code=409, detail=f"PVER {pver_id} is missing {CFG_OUTPUT_NAME}")
    alarms = _find_alarms(folder)
    source = _find_source(folder)
    output = _output_path(folder)
    _cfg_graph = _read_cfg(cfg)
    _cfg_callers = _index_callers(_cfg_graph)
    _active_pver = folder.name
    _full_graph = None
    if alarms is not None and output.exists():
        _store = AlarmStore(alarms, output, source if source and source.exists() else None)
    else:
        _store = None
    return _store


def _run_analyzer_job(pver_id: str, cfg_only: bool = False) -> None:
    folder = _pver_dir(pver_id)
    alarms = _find_alarms(folder) or _find_log(folder)
    source = _find_source(folder)
    output = _output_path(folder)
    cfg = _cfg_path(folder)
    if source is None or (not cfg_only and alarms is None):
        with _jobs_lock:
            _jobs[pver_id] = {"status": "error", "message": "Missing alarms CSV or input.c"}
        return
    if not ANALYZER_PATH.exists():
        with _jobs_lock:
            _jobs[pver_id] = {"status": "error", "message": f"Analyzer not found: {ANALYZER_PATH}"}
        return
    if cfg_only:
        command = [sys.executable, str(ANALYZER_PATH), "--cfg-only", str(source), str(cfg)]
        message = f"Building full control-flow graph from {source.name}; writing {cfg.name}"
    else:
        command = [sys.executable, str(ANALYZER_PATH), str(alarms), str(source), str(output), str(cfg)]
        message = (
            f"Using {alarms.name} + {source.name}; writing {output.name} and {cfg.name} in {folder.name}"
        )
    with _jobs_lock:
        _jobs[pver_id] = {"status": "running", "message": message}
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        log = (completed.stdout or "") + (completed.stderr or "")
        missing = []
        if not cfg_only and not output.exists():
            missing.append(output.name)
        if not cfg.exists():
            missing.append(cfg.name)
        if completed.returncode != 0 or missing:
            with _jobs_lock:
                _jobs[pver_id] = {
                    "status": "error",
                    "message": log.strip() or f"Analyzer exited with {completed.returncode}; missing {', '.join(missing)}",
                }
            return
        _activate_pver(pver_id)
        with _jobs_lock:
            _jobs[pver_id] = {"status": "ready", "message": log.strip()}
    except Exception as exc:  # noqa: BLE001 - surface any analyzer/load failure
        with _jobs_lock:
            _jobs[pver_id] = {"status": "error", "message": str(exc)}


def _start_analyzer(pver_id: str, cfg_only: bool = False) -> dict[str, Any]:
    with _jobs_lock:
        current = _jobs.get(pver_id, {})
        if current.get("status") == "running":
            return {"status": "building", "message": current.get("message", "Analysis is already running")}
        _jobs[pver_id] = {"status": "running", "message": "Finding PVER files and starting the analyzer"}
    thread = threading.Thread(target=_run_analyzer_job, args=(pver_id, cfg_only), daemon=True)
    thread.start()
    return {"status": "building", "message": "Analysis started"}


app = FastAPI(title="Astree Alarm Explorer", version="1.0.0")
origins = [
    origin.strip()
    for origin in os.getenv(
        "ASTREE_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_store: AlarmStore | None = None
_cfg_graph: dict[str, list[str]] | None = None
_cfg_callers: dict[str, list[str]] | None = None
_full_graph: dict[str, Any] | None = None
_active_pver: str | None = None
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def get_store() -> AlarmStore | None:
    return _store


@app.get("/api/pvers")
def api_list_pvers() -> dict[str, Any]:
    items = list_pver_folders()
    return {"items": items, "total": len(items), "active": _active_pver}


@app.get("/api/pvers/{pver_id}")
def api_get_pver(pver_id: str) -> dict[str, Any]:
    folder = _pver_dir(pver_id)
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"PVER {pver_id} was not found")
    return _describe_pver(folder)


@app.post("/api/pvers/{pver_id}/open")
def api_open_pver(pver_id: str) -> dict[str, Any]:
    folder = _pver_dir(pver_id)
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"PVER {pver_id} was not found")
    info = _describe_pver(folder)
    if info["status"] == "incomplete" and not info["has_cfg"]:
        raise HTTPException(status_code=400, detail=f"PVER {pver_id} needs {CFG_OUTPUT_NAME}")
    if info["has_cfg"] and info["status"] != "building":
        store = _activate_pver(pver_id)
        info = _describe_pver(folder)
        info["total"] = len(store.records) if store is not None else 0
        info["graph_file"] = CFG_OUTPUT_NAME
        return info
    started = _start_analyzer(pver_id, cfg_only=bool(info["has_output"] and not info["has_cfg"]))
    info = _describe_pver(folder)
    info.update(started)
    return info


@app.get("/api/pvers/{pver_id}/status")
def api_pver_status(pver_id: str) -> dict[str, Any]:
    folder = _pver_dir(pver_id)
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"PVER {pver_id} was not found")
    return _describe_pver(folder)


@app.get("/api/alarms")
def list_alarms() -> dict[str, Any]:
    store = get_store()
    if store is None:
        return {"items": [], "total": 0, "pver": _active_pver}
    return {"items": store.summaries(), "total": len(store.records), "pver": _active_pver}


@app.get("/api/alarm")
def get_alarm_query(order: str) -> dict[str, Any]:
    return get_alarm(order)


@app.get("/api/alarm/{order_id}")
def get_alarm(order_id: str) -> dict[str, Any]:
    store = get_store()
    if store is None:
        raise HTTPException(status_code=409, detail="Select a PVER before opening an alarm")
    record = store.get(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Alarm {order_id} was not found")
    payload = {
        "alarm": record["alarm"],
        "group": record["group"],
        "pver": _active_pver,
        # Keep the flattened fields for the current frontend and older clients.
        **{key: value for key, value in record.items() if key not in {"alarm", "group"}},
    }
    function = _alarm_function(payload)
    neighborhood = _cfg_neighborhood(function) if function else None
    if neighborhood:
        payload["enclosing_function"] = function
        payload["cfg_neighborhood"] = neighborhood
        names = {function, *neighborhood["callers"], *neighborhood["callees"]}
        snippets = dict(payload.get("function_snippets") or {})
        for name in names:
            snippet = store.snippets.get(name)
            if snippet:
                snippets[name] = snippet
        payload["function_snippets"] = snippets
        if not any(
            path.get("function_sequence") or path.get("call_stack")
            for path in payload.get("paths") or []
        ):
            payload["function_names"] = [function]
            payload["path_count"] = max(len(neighborhood["callers"]), 1)
    payload["graph_highlight"] = _graph_highlight(payload)
    _enrich_dataflow_order(payload)
    return payload


def _cfg_degrees() -> tuple[dict[str, int], dict[str, int]]:
    graph = _cfg_graph or {}
    outdeg = {name: len(callees) for name, callees in graph.items()}
    indeg: dict[str, int] = {}
    for callees in graph.values():
        for callee in callees:
            indeg[callee] = indeg.get(callee, 0) + 1
    return indeg, outdeg


def _cfg_names() -> set[str]:
    indeg, outdeg = _cfg_degrees()
    return set(outdeg) | set(indeg)


@app.get("/api/graph")
def get_full_graph() -> dict[str, Any]:
    global _full_graph
    if _cfg_graph is None or not _active_pver:
        raise HTTPException(status_code=409, detail="Select a PVER before opening the full graph")
    if _full_graph is None:
        folder = _pver_dir(_active_pver)
        _full_graph = _build_full_graph(_cfg_graph, pver_id=_active_pver, cfg_path=_cfg_path(folder))
    return _full_graph


@app.get("/api/cfg/summary")
def get_cfg_summary(limit: int = 40) -> dict[str, Any]:
    if _cfg_graph is None or not _active_pver:
        raise HTTPException(status_code=409, detail="Select a PVER before opening the control-flow graph")
    indeg, outdeg = _cfg_degrees()
    names = set(outdeg) | set(indeg)
    ranked = sorted(names, key=lambda name: outdeg.get(name, 0) + indeg.get(name, 0), reverse=True)
    hubs = [
        {"name": name, "in": indeg.get(name, 0), "out": outdeg.get(name, 0)}
        for name in ranked[: max(1, min(limit, 80))]
    ]
    return {
        "pver": _active_pver,
        "file": CFG_OUTPUT_NAME,
        "path": str(_cfg_path(_pver_dir(_active_pver))),
        "function_count": len(names),
        "edge_count": sum(outdeg.values()),
        "hubs": hubs,
    }


@app.get("/api/cfg/search")
def search_cfg(q: str = "", limit: int = 40) -> dict[str, Any]:
    if _cfg_graph is None:
        raise HTTPException(status_code=409, detail="Select a PVER before searching the control-flow graph")
    query = q.strip().lower()
    indeg, outdeg = _cfg_degrees()
    names = sorted(set(outdeg) | set(indeg))
    matches = [name for name in names if not query or query in name.lower()][: max(1, min(limit, 80))]
    return {
        "pver": _active_pver,
        "query": q,
        "items": [
            {"name": name, "in": indeg.get(name, 0), "out": outdeg.get(name, 0)}
            for name in matches
        ],
    }


@app.get("/api/cfg/neighborhood")
def get_cfg_neighborhood(fn: str, limit: int = 40) -> dict[str, Any]:
    if _cfg_graph is None:
        raise HTTPException(status_code=409, detail="Select a PVER before opening the control-flow graph")
    function = fn.strip()
    if not function:
        raise HTTPException(status_code=400, detail="A function name is required")
    if function not in _cfg_names():
        raise HTTPException(status_code=404, detail=f"Function {function} was not found")
    cap = max(1, min(limit, 80))
    callers = list((_cfg_callers or {}).get(function, []))
    callees = list(_cfg_graph.get(function, []))
    return {
        "function": function,
        "callers": callers[:cap],
        "callees": callees[:cap],
        "caller_count": len(callers),
        "callee_count": len(callees),
    }


@app.get("/api/cfg")
def get_control_flow_graph() -> dict[str, Any]:
    if _cfg_graph is None:
        raise HTTPException(status_code=409, detail="Select a PVER before opening the control-flow graph")
    callers: dict[str, list[str]] = {name: [] for name in _cfg_graph}
    for caller, callees in _cfg_graph.items():
        for callee in callees:
            callers.setdefault(callee, []).append(caller)
    return {
        "pver": _active_pver,
        "file": CFG_OUTPUT_NAME,
        "graph": _cfg_graph,
        "callers": callers,
        "function_count": len(_cfg_graph),
        "edge_count": sum(len(callees) for callees in _cfg_graph.values()),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "pver": _active_pver}
