"""Lossy C → compact pseudocode for explorer LLM context (token reduction).

Preserves control flow, calls, assignments, and bounds. Drops braces-only
lines, preprocessor noise, and redundant boolean idioms. Emits a small
``cite`` block of exact ``line: C`` rows so facts remain verifiable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable


_PREPROC_RE = re.compile(r"^\s*#")
_BRACE_ONLY_RE = re.compile(r"^[{};]+$")
_FOR_COPY_RE = re.compile(
    r"for\s*\(\s*(?:uint\w*|int|unsigned|size_t)?\s*(\w+)\s*=\s*0\w*\s*;"
    r"\s*\1\s*<\s*(\w+)\s*;\s*\1\s*\+\+\s*\)",
    re.IGNORECASE,
)
_COPY_ASSIGN_RE = re.compile(
    r"^([\w.]+)\s*\[\s*(\w+)\s*\]\s*=\s*([\w.]+)\s*\[\s*\2\s*\]\s*;?\s*$"
)
_CASE_RE = re.compile(r"^case\s+(0x[0-9A-Fa-f]+|\d+)\w*\s*:")
_BOOL_TRUE_RE = re.compile(r"\(\s*1\s*!=\s*0\s*\)")
_BOOL_FALSE_RE = re.compile(r"\(\s*0\s*!=\s*0\s*\)")
_NE_FALSE_RE = re.compile(r"\(\s*\(?\s*([\w.]+)\s*\)?\s*!=\s*\(\s*0\s*!=\s*0\s*\)\s*\)")
_VOID_CAST_RE = re.compile(r"\(\s*\(void\)\s*\([^)]*\)\s*\)\s*;?")
_CAST_RE = re.compile(r"\(\s*(?:uint\d*|int|unsigned|signed|uint8|uint16|uint32|bool)\s*\)\s*")
_HEX_U_RE = re.compile(r"\b(0x[0-9A-Fa-f]+)u\b", re.IGNORECASE)
_DEC_U_RE = re.compile(r"\b(\d+)u\b")
_WS_RE = re.compile(r"\s+")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_INDEXED_SEQ_RE = re.compile(
    r"^([\w.]+?)(\d+)\s*=\s*([\w.]+)\s*\[\s*(\d+)\s*\]\s*;?\s*$"
)
_SKIP_DECL_RE = re.compile(
    r"^(?:static\s+|const\s+)*(?:struct\s+\w+|[\w*]+)\s+(\w+)\s*;?\s*$"
)
_CTRL_RE = re.compile(r"^(if|else\s+if|while|for|switch|elif)\b", re.IGNORECASE)

_PREFERRED_ALIASES = (
    ("imsData", "d"),
    ("B_newDatRx", "RX"),
    ("B_newDatTx", "TX"),
    ("B_newTCUDatRx", "TCU"),
    ("sia_TCU_buffer", "TCU_buf"),
    ("sia_buffer", "sia_buf"),
    ("siaCommState", "siaCommState"),  # keep — short enough / high signal
)


@dataclass(frozen=True)
class PseudoResult:
    function: str
    pseudo: str
    cite: str
    snippet: str
    original_chars: int
    compressed_chars: int

    @property
    def ratio(self) -> float:
        if self.original_chars <= 0:
            return 1.0
        return self.compressed_chars / self.original_chars


def pseudocode_enabled() -> bool:
    return (os.getenv("AOOB_PSEUDOCODE") or "1").strip().lower() not in {"0", "false", "no"}


def parse_snippet_rows(snippet: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line in (snippet or "").splitlines():
        if ":" not in line:
            continue
        num_s, text = line.split(":", 1)
        try:
            ln = int(num_s.strip())
        except ValueError:
            continue
        rows.append((ln, text.rstrip("\n")))
    return rows


def normalize_expr(text: str) -> str:
    """Collapse Astrée / MISRA boolean noise and trivial casts."""
    t = text.strip()
    t = _VOID_CAST_RE.sub("", t)
    t = _NE_FALSE_RE.sub(r"\1", t)
    t = _BOOL_TRUE_RE.sub("1", t)
    t = _BOOL_FALSE_RE.sub("0", t)
    t = _CAST_RE.sub("", t)
    t = _HEX_U_RE.sub(r"\1", t)
    t = _DEC_U_RE.sub(r"\1", t)
    # unwrap ((ident)) but never glue to a leading keyword
    for _ in range(6):
        nxt = re.sub(r"(?<![A-Za-z_0-9])\(\s*([\w.]+)\s*\)", r"\1", t)
        if nxt == t:
            break
        t = nxt
    t = _WS_RE.sub(" ", t).strip()
    # normalize `if cond` / `if(cond)` → `if (cond)`
    t = re.sub(r"\b(if|while|for|switch|elif)\(", r"\1 (", t)
    t = re.sub(r"\b(if|while|switch|elif)\s+([^(].+)$", r"\1 (\2)", t)
    return t


def _strip_line(text: str) -> str:
    t = text.strip()
    if "//" in t:
        t = t.split("//", 1)[0].rstrip()
    return t


def _is_noise(text: str) -> bool:
    t = _strip_line(text)
    if not t:
        return True
    if _PREPROC_RE.match(t):
        return True
    if _BRACE_ONLY_RE.match(t):
        return True
    if t.startswith("/*") and "*/" in t:
        return True
    return False


def _next_stmt(lines: list[tuple[int, str]], start: int, limit: int = 12) -> tuple[int, int, str] | None:
    j = start
    end = min(len(lines), start + limit)
    while j < end:
        t = _strip_line(lines[j][1])
        if _is_noise(t) or t in {"{", "}"}:
            j += 1
            continue
        return j, lines[j][0], t
    return None


def _alias_map(rows: Iterable[tuple[int, str]]) -> dict[str, str]:
    blob = "\n".join(t for _, t in rows)
    aliases: dict[str, str] = {}
    used: set[str] = set()
    for name, short in _PREFERRED_ALIASES:
        if short == name:
            continue
        if name in blob and short not in used:
            aliases[name] = short
            used.add(short)
    return aliases


def _apply_aliases(text: str, aliases: dict[str, str]) -> str:
    if not aliases:
        return text
    for name in sorted(aliases, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(name)}\b", aliases[name], text)
    return text


def _collapse_indexed_assigns(
    stmts: list[tuple[int, str, int]],
) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    i = 0
    while i < len(stmts):
        ln, stmt, depth = stmts[i]
        m = _INDEXED_SEQ_RE.match(stmt)
        if not m:
            out.append(stmts[i])
            i += 1
            continue
        prefix, start_s, rhs_base, rhs_i_s = m.groups()
        start, rhs_i = int(start_s), int(rhs_i_s)
        if start != rhs_i:
            out.append(stmts[i])
            i += 1
            continue
        end = start
        j = i + 1
        while j < len(stmts):
            m2 = _INDEXED_SEQ_RE.match(stmts[j][1])
            if (
                m2
                and m2.group(1) == prefix
                and m2.group(3) == rhs_base
                and int(m2.group(2)) == end + 1
                and int(m2.group(4)) == end + 1
            ):
                end += 1
                j += 1
                continue
            break
        if end > start:
            out.append((ln, f"{prefix}[{start}..{end}]={rhs_base}[{start}..{end}]", depth))
            i = j
        else:
            out.append(stmts[i])
            i += 1
    return out


def _try_for_copy(
    lines: list[tuple[int, str]], idx: int, aliases: dict[str, str]
) -> tuple[str | None, int, int]:
    text = normalize_expr(_strip_line(lines[idx][1]))
    m = _FOR_COPY_RE.search(text)
    if not m:
        return None, 0, idx
    var, bound = m.group(1), m.group(2)
    hit = _next_stmt(lines, idx + 1)
    if not hit:
        return None, 0, idx
    j, body_ln, body_t = hit
    cm = _COPY_ASSIGN_RE.match(normalize_expr(body_t))
    if not cm or cm.group(2) != var:
        return None, 0, idx
    stmt = _apply_aliases(f"{cm.group(1)}={cm.group(3)}[{bound}]", aliases)
    k = j + 1
    while k < len(lines) and k < j + 4:
        t = _strip_line(lines[k][1])
        if t == "}":
            k += 1
            break
        if _is_noise(t):
            k += 1
            continue
        break
    return stmt, body_ln, k


def _try_switch_map(
    lines: list[tuple[int, str]], idx: int, aliases: dict[str, str]
) -> tuple[str | None, int, int]:
    raw = normalize_expr(_strip_line(lines[idx][1]))
    sm = re.match(r"switch\s*\((.+)\)\s*\{?$", raw)
    if not sm:
        return None, 0, idx
    disc = _apply_aliases(normalize_expr(sm.group(1)), aliases)
    j = idx + 1
    mapping: list[str] = []
    default_action: str | None = None
    target: str | None = None
    while j < len(lines):
        t = _strip_line(lines[j][1])
        if t == "}":
            j += 1
            break
        if _is_noise(t) or t in {"{", "break;", "break"}:
            j += 1
            continue
        cm = _CASE_RE.match(normalize_expr(t))
        if cm:
            case_v = cm.group(1)
            hit = _next_stmt(lines, j + 1)
            if not hit:
                break
            k, _, stmt_raw = hit
            stmt = _apply_aliases(normalize_expr(stmt_raw).rstrip(";"), aliases)
            am = re.match(r"^([\w.]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)$", stmt)
            if am:
                target = target or am.group(1)
                mapping.append(f"{case_v}:{am.group(2)}")
            else:
                mapping.append(f"{case_v}:{stmt}")
            j = k + 1
            continue
        if t.startswith("default"):
            hit = _next_stmt(lines, j + 1)
            if hit:
                k, _, stmt_raw = hit
                default_action = _apply_aliases(normalize_expr(stmt_raw).rstrip(";"), aliases)
                j = k + 1
            else:
                j += 1
            continue
        return None, 0, idx
    if not mapping:
        return None, 0, idx
    lhs = target or "cmd"
    parts = ",".join(mapping)
    if default_action:
        parts = f"{parts},else:{default_action}"
    return f"{lhs}=map({disc}){{{parts}}}", lines[idx][0], j


def compress_function(
    function: str,
    rows: list[tuple[int, str]],
    *,
    focus_tokens: Iterable[str] | None = None,
    max_cite_lines: int = 36,
) -> PseudoResult:
    """Convert numbered C rows into compact pseudocode + citeable exact lines."""
    original = "\n".join(f"{ln}: {txt}" for ln, txt in rows)
    focus = {t for t in (focus_tokens or []) if t}
    aliases = _alias_map(rows)

    stmts: list[tuple[int, str, int]] = []
    i = 0
    depth = 0
    while i < len(rows):
        ln, raw = rows[i]
        text = _strip_line(raw)
        opens = text.count("{")
        closes = text.count("}")

        # skip function signature
        if function and re.search(rf"\b{re.escape(function)}\s*\(", text) and (
            text.rstrip().endswith("{") or text.rstrip().endswith(")")
            or "void" in text[:20]
        ):
            depth = max(0, depth + opens - closes)
            i += 1
            continue

        if _is_noise(text) or text in {"{", "}"}:
            depth = max(0, depth + opens - closes)
            i += 1
            continue

        stmt, sln, ni = _try_for_copy(rows, i, aliases)
        if stmt is not None:
            stmts.append((sln, stmt, depth))
            for k in range(i, ni):
                tt = rows[k][1]
                depth = max(0, depth + tt.count("{") - tt.count("}"))
            i = ni
            continue

        stmt, sln, ni = _try_switch_map(rows, i, aliases)
        if stmt is not None:
            stmts.append((sln, stmt, depth))
            for k in range(i, ni):
                tt = rows[k][1]
                depth = max(0, depth + tt.count("{") - tt.count("}"))
            i = ni
            continue

        if text in {"else", "else{"}:
            stmts.append((ln, "else:", max(0, depth)))
            depth = max(0, depth + opens - closes)
            i += 1
            continue

        if text.startswith("else if"):
            rest = text[len("else") :].lstrip()  # "if (...)"
            cond = _apply_aliases(normalize_expr(rest), aliases)
            # cond is "if (...)" → strip leading if
            cond = re.sub(r"^if\s*", "", cond).strip()
            stmts.append((ln, f"elif {cond}", max(0, depth)))
            depth = max(0, depth + opens - closes)
            i += 1
            continue

        norm = normalize_expr(text)
        dm = _SKIP_DECL_RE.match(norm)
        if dm and "=" not in norm:
            name = dm.group(1)
            if re.match(r"^(temp|idx|i|j|k|n|ctr|len|ret|flg|ptr)", name, re.I):
                depth = max(0, depth + opens - closes)
                i += 1
                continue
            if name.startswith("T_") or norm.startswith("struct ") or norm.startswith("Pdu"):
                depth = max(0, depth + opens - closes)
                i += 1
                continue

        norm = _apply_aliases(norm.rstrip(";"), aliases)
        if re.match(r"^(if|while|for)\b", norm):
            stmts.append((ln, norm if norm.endswith(":") else f"{norm}:", depth))
        else:
            # drop pointless wrapping parens around whole assignment: (x=0) → x=0
            if norm.startswith("(") and norm.endswith(")") and norm.count("(") == 1:
                norm = norm[1:-1].strip()
            stmts.append((ln, norm, depth))
        depth = max(0, depth + opens - closes)
        i += 1

    stmts = _collapse_indexed_assigns(stmts)

    out_lines: list[str] = [f"{function}:"]
    if aliases:
        legend = ", ".join(f"{v}={k}" for k, v in sorted(aliases.items(), key=lambda x: x[1]))
        out_lines.append(f" #aliases {legend}")

    for ln, stmt, d in stmts:
        pad = " " * min(d, 6)
        if stmt.rstrip(":") == "else" or stmt.startswith("elif ") or _CTRL_RE.match(stmt):
            out_lines.append(f"{pad}{stmt}")
        else:
            out_lines.append(f"{pad}@{ln} {stmt}")

    # Cite: prefer focus hits; otherwise keep only high-signal lines (bounds/calls/assigns)
    cite_rows: list[tuple[int, str]] = []
    for ln, text in rows:
        t = _strip_line(text)
        if _is_noise(t) or _BRACE_ONLY_RE.match(t) or _PREPROC_RE.match(t):
            continue
        if focus:
            if any(re.search(rf"\b{re.escape(tok)}\b", t) for tok in focus):
                cite_rows.append((ln, t))
            continue
        # no focus: keep control + indexed access + assignments, skip pure breaks
        if t in {"break;", "break", "continue;", "continue"}:
            continue
        if any(k in t for k in ("[", "if", "while", "for", "switch", "return", "case", "=")):
            cite_rows.append((ln, t))

    if len(cite_rows) > max_cite_lines:
        # Prefer lines with '[' or 'if' when trimming
        scored = sorted(
            cite_rows,
            key=lambda r: (
                0 if any(x in r[1] for x in ("[", "if", "<", ">", "sizeof")) else 1,
                r[0],
            ),
        )
        cite_rows = sorted(scored[:max_cite_lines], key=lambda r: r[0])

    cite = "\n".join(f"{ln}: {txt}" for ln, txt in cite_rows)
    pseudo = "\n".join(out_lines)
    snippet = (
        f"PSEUDO (logic-preserving, lossy):\n{pseudo}\n\n"
        f"CITE (copy quotes ONLY from these exact C lines):\n{cite or '(none)'}"
    )
    return PseudoResult(
        function=function,
        pseudo=pseudo,
        cite=cite,
        snippet=snippet,
        original_chars=len(original),
        compressed_chars=len(snippet),
    )


def enrich_func_payload(
    payload: dict[str, Any],
    *,
    focus_tokens: Iterable[str] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Attach pseudocode view; keep raw_snippet for mining / fallback."""
    if payload.get("error"):
        return payload
    use = pseudocode_enabled() if enabled is None else enabled
    raw = str(payload.get("raw_snippet") or payload.get("snippet") or "")
    out = dict(payload)
    out["raw_snippet"] = raw
    if not use or not raw:
        return out
    rows = parse_snippet_rows(raw)
    if not rows:
        return out
    name = str(payload.get("function") or "function")
    result = compress_function(name, rows, focus_tokens=focus_tokens)
    out["snippet"] = result.snippet
    out["pseudo"] = result.pseudo
    out["cite"] = result.cite
    out["format"] = "pseudocode+cite"
    out["compress_ratio"] = round(result.ratio, 3)
    out["original_chars"] = result.original_chars
    out["compressed_chars"] = result.compressed_chars
    return out
