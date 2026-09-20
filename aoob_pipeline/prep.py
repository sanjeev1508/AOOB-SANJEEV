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

# Astree frames look like ``call#Fn at file.c:123.4-9`` or ``call#Fn|separate at …``;
# the ``|separate`` (or other ``|tag``) suffix must not drop the frame.
_CALL_RE = re.compile(
    r"call#(?P<fn>[A-Za-z_]\w*)(?:\|[^\s]*)?\s+at\s+[^:]+:(?P<line>\d+)",
    re.IGNORECASE,
)
_C_KEYWORDS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "default", "return",
        "goto", "break", "continue", "sizeof", "typedef", "struct", "union", "enum",
    }
)
_TYPE_QUALIFIERS = r"(?:(?:const|volatile|static|unsigned|signed|register)\s+)*"
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
    key_lines: list[int] = []
    for occ in occurrences:
        if not isinstance(occ, dict) or not occ.get("line"):
            continue
        ln = int(occ["line"])
        key_lines.append(ln)
        text = occ.get("line_text") or ""
        access = (occ.get("access") or "").lower()
        if access == "read" and _GUARD_HINTS.search(text):
            guard_lines.append(ln)
        elif access in {"write", "declaration"}:
            continue
    used = [str(x) for x in (info.get("used_in_functions") or []) if x]
    decl = info.get("declaration_line")
    if decl:
        try:
            key_lines.append(int(decl))
        except (TypeError, ValueError):
            pass
    # stable unique preserve order
    seen_ln: set[int] = set()
    uniq_keys: list[int] = []
    for ln in key_lines:
        if ln in seen_ln:
            continue
        seen_ln.add(ln)
        uniq_keys.append(ln)
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
        key_lines=uniq_keys,
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


def clean_index_expression(expr: str | None) -> str | None:
    """Strip whitespace and unbalanced parentheses from an analyzer index slice.

    Astree column ranges sometimes cut ``[(numClass)]`` to ``(numClass``.
    """
    if not expr:
        return expr
    text = expr.strip()
    while text.startswith("(") and text.count("(") > text.count(")"):
        text = text[1:].strip()
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].strip()
    if text.startswith("(") and text.endswith(")") and text.count("(") == 1 and text.count(")") == 1:
        text = text[1:-1].strip()
    return text or None


# Identifier that does not start inside a number literal (``2u`` → no ``u``).
_IDENT_RE = re.compile(r"(?<![\w.])[A-Za-z_]\w*")


def index_tokens(expr: str | None) -> list[str]:
    toks = _IDENT_RE.findall(expr or "")
    return [t for t in dict.fromkeys(toks) if t not in _C_KEYWORDS]


def _function_header_end(source: SourceIndex, start_line: int, end_line: int) -> int:
    """Line of the opening ``{`` of the function body (parameters live before it)."""
    for ln, text in source.get_lines(start_line, min(end_line, start_line + 60)):
        if "{" in text:
            return ln
    return start_line


def locate_declaration(
    source: SourceIndex,
    *,
    function: str | None,
    near_line: int | None,
    token: str,
) -> tuple[int, str, str] | None:
    """Find ``T token`` inside ``function``. Returns (line, text, 'parameter'|'local')."""
    if not function or not token:
        return None
    span = source.function_span(function, near_line=near_line)
    if span is None:
        return None
    header_end = _function_header_end(source, span.start_line, span.end_line)
    pat = re.compile(
        rf"(?<![\w.>])(?P<type>{_TYPE_QUALIFIERS}[A-Za-z_]\w*)\s*\**\s+\**\s*\b{re.escape(token)}\b\s*(?:[,;)=\[]|$)"
    )
    stop = near_line if near_line and near_line > span.start_line else span.end_line
    for ln, text in source.get_lines(span.start_line, min(span.end_line, stop)):
        code = text.split("//", 1)[0]
        for m in pat.finditer(code):
            type_word = m.group("type").split()[-1]
            if type_word in _C_KEYWORDS or type_word == token:
                continue
            return ln, text.strip(), "parameter" if ln <= header_end else "local"
    return None


_ASSIGN_RE_TMPL = r"(?<![\w.>])(?:\*\s*)?{tok}\s*(?:\[[^\]]*\])?\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)\s*(?P<rhs>[^;]*);?"


def _rhs_identifiers(rhs: str) -> tuple[list[str], list[str]]:
    """Split an RHS into (identifier tokens, called function names)."""
    callees = [c for c in re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", rhs) if c not in _C_KEYWORDS]
    idents = [t for t in _IDENT_RE.findall(rhs) if t not in _C_KEYWORDS]
    # struct access ``a->b.c`` → keep the base object only (members are skipped
    # by the look-behind on ``.``; ``->`` members follow ``>`` and are dropped here)
    idents = [t for t in idents if t not in callees and not re.search(rf"->\s*{re.escape(t)}\b", rhs)]
    return list(dict.fromkeys(idents)), list(dict.fromkeys(callees))


def _out_param_callees(text: str, tok: str, source: SourceIndex) -> list[str]:
    """``Fn(..., &tok, ...)`` — the callee fills ``tok`` through a pointer."""
    out: list[str] = []
    for m in re.finditer(rf"(?<![\w.])([A-Za-z_]\w*)\s*\([^;{{}}]*&\s*{re.escape(tok)}\b", text):
        name = m.group(1)
        if name not in _C_KEYWORDS and name not in out and source.function_span(name) is not None:
            out.append(name)
    return out


def index_origin(
    source: SourceIndex,
    *,
    function: str | None,
    alarm_line: int | None,
    tokens: list[str],
) -> tuple[str, list[str]]:
    """Classify how the index gets its value inside the alarm function.

    Returns (origin, value_origin_callees). origin:
    - ``parameter``: some value feeding the index is a function parameter →
      callers matter, path coverage matters.
    - ``global``: fed by a symbol not declared in the function (callers cannot
      pass it directly, but ordering can matter) → keep conservative.
    - ``local``: computed only from locals / constants / call results →
      caller paths cannot change the index.
    """
    if not function or not tokens:
        return "unknown", []
    span = source.function_span(function, near_line=alarm_line)
    if span is None:
        return "unknown", []
    header_end = _function_header_end(source, span.start_line, span.end_line)
    header = " ".join(t for _, t in source.get_lines(span.start_line, header_end))
    header = header.split("{", 1)[0]
    params = {
        t for t in re.findall(r"[A-Za-z_]\w*", header.split("(", 1)[1] if "(" in header else "")
        if t not in _C_KEYWORDS
    }
    body_rows = source.get_lines(header_end, span.end_line)
    body_text = "\n".join(t.split("//", 1)[0] for _, t in body_rows)

    seen: set[str] = set()
    work = list(tokens)
    callees: list[str] = []
    saw_param = False
    saw_global = False
    while work:
        tok = work.pop(0)
        if tok in seen:
            continue
        seen.add(tok)
        if tok in params:
            saw_param = True
            continue
        decl = locate_declaration(source, function=function, near_line=alarm_line, token=tok)
        declared_local = decl is not None and decl[2] == "local"
        if decl is None:
            # not a param, not declared here → global / static file scope
            saw_global = True
        for m in re.finditer(_ASSIGN_RE_TMPL.format(tok=re.escape(tok)), body_text):
            idents, calls = _rhs_identifiers(m.group("rhs"))
            for c in calls:
                if c not in callees and source.function_span(c) is not None:
                    callees.append(c)
            for ident in idents:
                if ident not in seen:
                    work.append(ident)
        for c in _out_param_callees(body_text, tok, source):
            if c not in callees:
                callees.append(c)
        if declared_local and decl is not None and "=" in decl[1]:
            idents, calls = _rhs_identifiers(decl[1].split("=", 1)[1])
            for c in calls:
                if c not in callees and source.function_span(c) is not None:
                    callees.append(c)
            work.extend(i for i in idents if i not in seen)
        if len(seen) > 40:
            break

    if saw_param:
        return "parameter", callees[:6]
    if saw_global:
        return "global", callees[:6]
    return "local", callees[:6]


def caller_value_origin_callees(
    source: SourceIndex,
    *,
    path_classes: list[PathClass],
    tokens: list[str],
    limit: int = 6,
) -> list[str]:
    """Functions whose return value feeds the index in *caller* functions.

    For each path class walk caller→callee: scan the caller body for
    ``tok = Callee(...)`` where ``tok`` is an index token (parameter names
    are usually preserved across this codebase).
    """
    out: list[str] = []
    if not tokens:
        return out
    pats = [re.compile(_ASSIGN_RE_TMPL.format(tok=re.escape(t))) for t in tokens]
    visited: set[str] = set()
    for cls in path_classes:
        for step in cls.sequence:
            fn = step.function
            if not fn or fn in visited:
                continue
            visited.add(fn)
            body = source.get_func(fn)
            snippet = str(body.get("snippet") or "")
            if not snippet:
                continue
            for tok, pat in zip(tokens, pats):
                found = [
                    c
                    for m in pat.finditer(snippet)
                    for c in _rhs_identifiers(m.group("rhs"))[1]
                ]
                found.extend(_out_param_callees(snippet, tok, source))
                for c in found:
                    if c in out or c in visited:
                        continue
                    if source.function_span(c) is not None:
                        out.append(c)
                    if len(out) >= limit:
                        return out
    return out


def _fix_index_symbols(
    source: SourceIndex,
    symbols: list[SymbolSkeleton],
    *,
    enclosing: str | None,
    alarm_line: int | None,
    tokens: list[str],
    notes: list[str],
) -> list[SymbolSkeleton]:
    if not enclosing or not tokens:
        return symbols
    out: list[SymbolSkeleton] = []
    handled: set[str] = set()
    for sym in symbols:
        if sym.role == "index" or sym.symbol_name in tokens:
            hit = locate_declaration(
                source, function=enclosing, near_line=alarm_line, token=sym.symbol_name
            )
            if hit and hit[0] != sym.declaration_line:
                line, text, scope = hit
                notes.append(
                    f"index symbol {sym.symbol_name}: declaration re-pointed to "
                    f"{enclosing}:{line} ({scope}) from {sym.declaration_line}"
                )
                sym = sym.model_copy(
                    update={
                        "declaration_line": line,
                        "declaration_text": text,
                        "kind": scope,
                        "function_sequence": [enclosing],
                        "write_lines": [line],
                    }
                )
            elif hit:
                sym = sym.model_copy(update={"kind": sym.kind or hit[2]})
            handled.add(sym.symbol_name)
        out.append(sym)
    # Analyzer sometimes omits the index symbol entirely — synthesise it.
    for tok in tokens:
        if tok in handled:
            continue
        hit = locate_declaration(source, function=enclosing, near_line=alarm_line, token=tok)
        if not hit:
            continue
        line, text, scope = hit
        out.append(
            SymbolSkeleton(
                symbol_name=tok,
                role="index",
                kind=scope,
                declaration_line=line,
                declaration_text=text,
                function_sequence=[enclosing],
                write_lines=[line],
            )
        )
        notes.append(f"index symbol {tok} synthesised from {enclosing}:{line} ({scope})")
    return out


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
    notes_size: str | None = None
    if primary and primary.resolved_sizes:
        array_size = primary.resolved_sizes[0]
    elif primary and primary.declaration_line:
        from aoob_pipeline.explore_support import _count_initializer_elements

        count, _, _ = _count_initializer_elements(source, int(primary.declaration_line))
        if count:
            array_size = count
            notes_size = f"array_size recovered from initializer count={count}"

    enclosing = alarm.get("enclosing_function") or (
        source.enclosing_function(alarm_line) if alarm_line else None
    )
    index_expr = clean_index_expression(
        (primary.index_expression if primary else None) or alarm.get("variable")
    )
    idx_tokens = index_tokens(index_expr)
    notes: list[str] = []

    # Re-point the index symbol at the declaration that is actually in scope at
    # the alarm (parameter/local of the enclosing function), not the first
    # same-named symbol anywhere in the translation unit.
    symbols = _fix_index_symbols(source, symbols, enclosing=enclosing, alarm_line=alarm_line,
                                 tokens=idx_tokens, notes=notes)

    origin, origin_callees = index_origin(
        source, function=enclosing, alarm_line=alarm_line, tokens=idx_tokens
    )
    callees = list(origin_callees)
    if origin == "parameter":
        for c in caller_value_origin_callees(source, path_classes=path_classes, tokens=idx_tokens):
            if c not in callees:
                callees.append(c)
    if callees:
        notes.append(f"value-origin callees: {callees}")

    local_guard = _local_guard_check(
        source,
        enclosing=enclosing,
        alarm_line=alarm_line,
        index_expression=index_expr,
        array_size=array_size,
    )

    if notes_size:
        notes.append(notes_size)
    if coverage_cap == "partial":
        if origin == "local":
            coverage_cap = "full"
            notes.append(
                f"path classes capped at {path_class_cap}, but index is computed locally "
                "in the alarm function — caller paths cannot change it; coverage kept full"
            )
        else:
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
        index_origin=origin,
        value_origin_callees=callees,
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
