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
_C_KEYWORDS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "default", "return",
        "goto", "break", "continue", "sizeof", "typedef", "struct", "union", "enum",
        "const", "volatile", "static", "unsigned", "signed", "void",
    }
)
_BOUND_KINDS = frozenset({"guard", "clamp", "mask", "loop_bound", "local_guard", "array_size_hint"})
_ALLOWED_KINDS = frozenset(
    {
        "declaration", "alarm_site", "access", "write", "read", "guard", "clamp", "mask",
        "loop_bound", "arg_binding", "call_edge", "path_step", "value_site",
        "array_size_hint", "local_guard", "fact",
    }
)


def _index_tokens(expr: str | None) -> list[str]:
    return [t for t in dict.fromkeys(re.findall(r"(?<![\w.])[A-Za-z_]\w*", expr or "")) if t not in _C_KEYWORDS]


def is_lhs_write(text: str, token: str) -> bool:
    """True if ``token`` is assigned on this line (``tok = …``, ``tok += …``, ``tok++``)."""
    code = text.split("//", 1)[0]
    if re.search(rf"(?<![\w.>])\*?\s*\b{re.escape(token)}\b\s*(?:\[[^\]]*\])?\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)", code):
        # make sure the match is not inside a comparison / condition body
        head = code.split("=", 1)[0]
        return token in head and not re.search(r"\b(if|while|return)\b", head)
    if re.search(rf"(?<![\w.>])\b{re.escape(token)}\b\s*(\+\+|--)|(\+\+|--)\s*\b{re.escape(token)}\b", code):
        return True
    return False


def relevance_tokens(prep: PrepPack, sequence: list[str]) -> set[str]:
    """Identifiers an explorer fact must mention to be kept."""
    toks: set[str] = set(_index_tokens(prep.index_expression))
    if prep.array_name:
        toks.add(prep.array_name)
    toks.update(n for n in sequence if n)
    toks.update(prep.value_origin_callees)
    for sym in prep.symbols:
        if sym.symbol_name:
            toks.add(sym.symbol_name)
    return {t for t in toks if t}


def normalize_llm_fact(
    item: dict[str, Any],
    *,
    function: str,
    prep: PrepPack,
    source: SourceIndex,
    relevant: set[str],
) -> Fact | None:
    """Validate an LLM-extracted fact: exact line, sane kind, relevant symbol.

    - Line is corrected to the line that actually holds the quote (±2) or dropped.
    - ``alarm_site`` only at the real alarm line; otherwise re-kinded.
    - ``write`` only when the symbol is really on the LHS.
    - Facts that mention none of the relevant identifiers are dropped.
    """
    quote = str(item.get("quote") or "").strip()
    if not quote:
        return None
    try:
        line = int(item.get("line"))
    except (TypeError, ValueError):
        return None
    actual = source.find_quote_line(line, quote, radius=2)
    if actual is None:
        return None
    text = source.line_text(actual) or ""
    kind = str(item.get("kind") or "fact").strip().lower()
    if kind not in _ALLOWED_KINDS:
        kind = "fact"
    symbol = item.get("symbol")
    symbol = str(symbol).strip() if symbol else None

    idx_toks = _index_tokens(prep.index_expression)
    array = prep.array_name or ""

    # relevance: the *source line* must mention something we care about
    mentioned = {t for t in relevant if re.search(rf"\b{re.escape(t)}\b", text)}
    if not mentioned:
        return None
    if symbol and symbol not in mentioned and not re.search(rf"\b{re.escape(symbol)}\b", text):
        symbol = None

    if kind == "alarm_site" and actual != (prep.alarm_line or -1):
        kind = "access" if array and re.search(rf"\b{re.escape(array)}\s*\[", text) else "fact"
    if kind == "write":
        tok = symbol if symbol and is_lhs_write(text, symbol) else next(
            (t for t in idx_toks if is_lhs_write(text, t)), None
        )
        if tok is None:
            kind = "read" if any(re.search(rf"\b{re.escape(t)}\b", text) for t in idx_toks) else "fact"
        else:
            symbol = tok
    if kind == "call_edge" and "(" not in text:
        kind = "fact"
    if kind in _BOUND_KINDS and not (
        _GUARD_RE.search(text) or re.search(r"\[\s*\]|\bsizeof\b", text)
    ):
        kind = "fact"
    if kind in {"fact", "read"} and not (idx_toks and any(t in mentioned for t in idx_toks)) and array not in mentioned:
        # generic mention of a path function name only — not evidence
        return None

    return Fact(
        kind=kind,
        function=str(item.get("function") or function),
        line=actual,
        quote=text.strip()[:300] if len(quote) < 8 else quote[:300],
        symbol=symbol,
        note=str(item.get("note") or "llm extract"),
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


def var_value_sequence(prep: PrepPack, source: SourceIndex, *, cap: int = 8) -> list[str]:
    """Tight visit list: enclosing + value-origin callees + path callers + writers.

    Do NOT expand every ``used_in_functions`` hit for a shared parameter name —
    that floods 7B explorers with unrelated siblings. Functions that *produce*
    the index value (``idx = Fn(...)``) are visited even though Astree's call
    stack never lists them — that is where the real bound usually lives.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(name: str | None) -> None:
        n = (name or "").strip()
        if not n or n in seen or n == "global":
            return
        seen.add(n)
        out.append(n)

    add(prep.enclosing_function)
    for name in prep.value_origin_callees:
        add(name)
    if prep.index_origin != "local":
        # callers only matter when they can feed the index (parameter) or set
        # shared state before the call (global); a purely local index skips them
        for name in call_path_sequence(prep):
            add(name)

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


_INIT_SCAN_LINES = 20000


def _count_initializer_elements(source: SourceIndex, decl_line: int) -> tuple[int | None, str | None, int | None]:
    """If ``name[] = { a, b, ... };`` follows decl_line, return (count, quote, end_line).

    Large config tables (struct arrays) can run for thousands of lines, so the
    scan follows brace depth up to ``_INIT_SCAN_LINES``. The ``{`` must belong
    to this declaration: an ``=`` must precede it and no ``;`` may intervene.
    """
    rows = source.get_lines(decl_line, decl_line + _INIT_SCAN_LINES)
    text = "\n".join(t for _, t in rows)
    start = text.find("{")
    if start < 0:
        return None, None, None
    prefix = text[:start]
    if "=" not in prefix or ";" in prefix:
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
    # keep ``rows`` tight for the quote/end_line bookkeeping below
    end_line_no = decl_line + text.count("\n", 0, end)
    rows = [r for r in rows if r[0] <= end_line_no]
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
    # Quote only the declaration line itself so the fact stays verifiable
    # against a single source line; the note carries the counted range.
    quote = rows[0][1].strip()[:240] if rows else ""
    return len(parts), quote, end_line_no


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
                        line=int(sym.declaration_line),
                        quote=init_quote or quote,
                        symbol=sym.symbol_name,
                        note=(
                            f"initializer element count={count} "
                            f"(lines {sym.declaration_line}-{end_ln}) → array size {count}"
                        ),
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
        if isinstance(payload, dict):
            # Keep raw_snippet for fallback mining; optional pseudo for UI.
            if "snippet" in payload and "raw_snippet" not in payload:
                payload = {**payload, "raw_snippet": payload["snippet"]}
            bodies.append(payload)
        else:
            bodies.append({"function": name, "error": str(payload)})
    return bodies


def mine_facts_from_body(
    *,
    agent: str,
    function: str,
    body: dict[str, Any],
    prep: PrepPack,
) -> list[Fact]:
    """Deterministic high-signal extracts (works without LLM tool calling)."""
    # Prefer raw C — LLM-facing snippet may be pseudocode+cite.
    snippet = str(body.get("raw_snippet") or body.get("snippet") or "")
    if body.get("error") or not snippet:
        return []
    facts: list[Fact] = []
    array = prep.array_name or ""
    index = prep.index_expression or ""
    index_toks = _index_tokens(index)

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

        if array and re.search(rf"\b{re.escape(array)}\s*\[\s*\]", text) and "=" in text:
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
        # A real subscript: ``array[ <non-empty> ]`` — not the ``[]`` of a declaration.
        if array and re.search(rf"\b{re.escape(array)}\s*\[\s*[^\]\s]", text):
            facts.append(
                Fact(
                    kind="alarm_site" if prep.alarm_line and ln == prep.alarm_line else "access",
                    function=function,
                    line=ln,
                    quote=text[:300],
                    symbol=index if (index and index in text) else array,
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
        for tok in index_toks:
            if is_lhs_write(text, tok):
                facts.append(
                    Fact(
                        kind="write",
                        function=function,
                        line=ln,
                        quote=text[:300],
                        symbol=tok,
                        note="mined index write/assign",
                    )
                )
                break

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
