"""Visit sequences, seed facts, and deterministic body mining for explorers."""

from __future__ import annotations

import re
from typing import Any

from aoob_pipeline.schemas import Fact, PrepPack
from aoob_pipeline.source_index import SourceIndex

_GUARD_RE = re.compile(
    r"\b(if|while|for|switch)\b|<=|>=|<|>|&&|\|\||&\s*0x|%\s*\d+|clamp|MIN|MAX",
    re.IGNORECASE,
)


def call_path_sequence(prep: PrepPack) -> list[str]:
    """Ordered unique functions from path classes (caller→callee order)."""
    seen: set[str] = set()
    out: list[str] = []
    for cls in prep.path_classes:
        for step in cls.sequence:
            name = (step.function or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
    enc = (prep.enclosing_function or "").strip()
    if enc and enc not in seen:
        out.append(enc)
    return out


def var_value_sequence(prep: PrepPack, source: SourceIndex, *, cap: int = 6) -> list[str]:
    """Tight visit list: path + enclosing + functions owning write lines.

    Do NOT expand every ``used_in_functions`` hit for a shared parameter name —
    that floods 7B explorers with unrelated siblings.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(name: str | None) -> None:
        n = (name or "").strip()
        if not n or n in seen or n == "global":
            return
        seen.add(n)
        out.append(n)

    for name in call_path_sequence(prep):
        add(name)
    add(prep.enclosing_function)

    for sym in prep.symbols:
        for line in sym.write_lines:
            try:
                ln = int(line)
            except (TypeError, ValueError):
                continue
            add(source.enclosing_function(ln))
        if sym.declaration_line:
            add(source.enclosing_function(int(sym.declaration_line)))

    return out[:cap]


def _count_initializer_elements(source: SourceIndex, decl_line: int) -> tuple[int | None, str | None, int | None]:
    """If ``name[] = { a, b, ... };`` follows decl_line, return (count, quote, end_line)."""
    rows = source.get_lines(decl_line, min(decl_line + 40, decl_line + 200))
    text = "\n".join(t for _, t in rows)
    if "{" not in text:
        return None, None, None
    # Take from first { after decl through matching }
    start = text.find("{")
    if start < 0:
        return None, None, None
    depth = 0
    end = None
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return None, None, None
    body = text[start + 1 : end]
    # Split top-level commas (ignore nested)
    parts: list[str] = []
    buf = ""
    nest = 0
    for ch in body:
        if ch in "{([":
            nest += 1
        elif ch in "})]":
            nest = max(0, nest - 1)
        if ch == "," and nest == 0:
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        parts.append(buf.strip())
    # Drop empty / comment-only
    parts = [p for p in parts if p and not p.startswith("/*")]
    if not parts:
        return None, None, None
    # Quote the brace block (first ~3 lines of the window that contain braces)
    quote_lines = []
    for ln, t in rows:
        quote_lines.append(t.strip())
        if "}" in t and len(quote_lines) >= 2:
            break
    quote = " ".join(quote_lines)[:240]
    end_line = rows[0][0]
    for ln, t in rows:
        if "}" in t:
            end_line = ln
            break
    return len(parts), quote, end_line


def seed_facts(prep: PrepPack, source: SourceIndex) -> list[Fact]:
    """Code-only facts from prep + initializer size recovery."""
    facts: list[Fact] = []
    for sym in prep.symbols:
        if not sym.declaration_line or not sym.declaration_text:
            continue
        quote = sym.declaration_text.strip()
        if not source.verify_quote(int(sym.declaration_line), quote, radius=3):
            line_txt = source.line_text(int(sym.declaration_line))
            if not line_txt:
                continue
            quote = line_txt.strip()
        fn = source.enclosing_function(int(sym.declaration_line)) or "global"
        facts.append(
            Fact(
                kind="declaration",
                function=fn,
                line=int(sym.declaration_line),
                quote=quote,
                symbol=sym.symbol_name,
                note="seeded from analyzer declaration",
                verified=True,
            )
        )
        # Recover size from local ``T name[] = { ... }``
        if "[]" in quote or (sym.array_dims in {"[]", None, ""} and sym.kind == "array"):
            count, init_quote, end_ln = _count_initializer_elements(source, int(sym.declaration_line))
            if count:
                facts.append(
                    Fact(
                        kind="array_size_hint",
                        function=fn,
                        line=int(end_ln or sym.declaration_line),
                        quote=init_quote or quote,
                        symbol=sym.symbol_name,
                        note=f"initializer element count={count}",
                        verified=True,
                    )
                )

    if prep.alarm_line:
        line_txt = source.line_text(int(prep.alarm_line))
        if line_txt:
            facts.append(
                Fact(
                    kind="alarm_site",
                    function=prep.enclosing_function
                    or source.enclosing_function(int(prep.alarm_line))
                    or "",
                    line=int(prep.alarm_line),
                    quote=line_txt.strip(),
                    symbol=prep.index_expression,
                    note="seeded alarm location line",
                    verified=True,
                )
            )

    if prep.local_guard.found and prep.local_guard.line and prep.local_guard.quote:
        facts.append(
            Fact(
                kind="local_guard",
                function=prep.local_guard.function or prep.enclosing_function or "",
                line=int(prep.local_guard.line),
                quote=prep.local_guard.quote,
                symbol=prep.index_expression,
                note=prep.local_guard.note,
                verified=True,
            )
        )
    return facts


def force_open_bodies(source: SourceIndex, sequence: list[str]) -> list[dict]:
    bodies: list[dict] = []
    for name in sequence:
        payload = source.get_func(name)
        bodies.append(payload if isinstance(payload, dict) else {"function": name, "error": str(payload)})
    return bodies


def mine_facts_from_body(
    *,
    agent: str,
    function: str,
    body: dict[str, Any],
    prep: PrepPack,
) -> list[Fact]:
    """Deterministic high-signal extracts (works without LLM tool calling)."""
    snippet = str(body.get("snippet") or "")
    if body.get("error") or not snippet:
        return []
    facts: list[Fact] = []
    array = prep.array_name or ""
    index = prep.index_expression or ""
    needles = [n for n in [array, index] if n]

    for line in snippet.splitlines():
        if ":" not in line:
            continue
        num_s, text = line.split(":", 1)
        try:
            ln = int(num_s.strip())
        except ValueError:
            continue
        text = text.strip()
        if not text or text.startswith("/*"):
            continue

        # Skip pure parameter declarations (weak fallback noise)
        if re.match(r"^(const\s+)?(uint\d+|int|unsigned|signed|size_t|bool)\b", text) and text.rstrip().endswith(","):
            continue
        if text.rstrip().endswith(",") and "(" not in text and "=" not in text and "[" not in text:
            # likely mid-parameter list
            if index and index in text and "[" not in text:
                continue

        if array and "[]" in text and array in text and "=" in text:
            facts.append(
                Fact(
                    kind="declaration",
                    function=function,
                    line=ln,
                    quote=text[:300],
                    symbol=array,
                    note="mined array declaration",
                )
            )
        if array and index and (
            f"{array}[" in text.replace(" ", "")
            or (array in text and "[" in text and index in text)
        ):
            facts.append(
                Fact(
                    kind="alarm_site" if prep.alarm_line and ln == prep.alarm_line else "access",
                    function=function,
                    line=ln,
                    quote=text[:300],
                    symbol=index,
                    note="mined indexed access",
                )
            )
        if index and _GUARD_RE.search(text) and index in text and "[" not in text.split(index, 1)[0][-3:]:
            # Prefer real control-flow guards, not array subscript lines
            if re.search(r"\b(if|while|for|switch)\b", text) or re.search(
                rf"\b{re.escape(index)}\b\s*(<|>|<=|>=|==|!=)", text
            ):
                facts.append(
                    Fact(
                        kind="guard",
                        function=function,
                        line=ln,
                        quote=text[:300],
                        symbol=index,
                        note="mined guard/bound candidate",
                    )
                )
        if index and re.search(rf"\b{re.escape(index)}\b\s*=", text):
            facts.append(
                Fact(
                    kind="write",
                    function=function,
                    line=ln,
                    quote=text[:300],
                    symbol=index,
                    note="mined index write/assign",
                )
            )

    # Dedup by (line, kind)
    seen: set[tuple] = set()
    out: list[Fact] = []
    for f in facts:
        key = (f.line, f.kind, f.quote[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out
