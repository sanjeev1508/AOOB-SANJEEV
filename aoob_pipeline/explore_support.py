"""Visit sequences and deterministic seed facts for explorers."""

from __future__ import annotations

from aoob_pipeline.schemas import Fact, PrepPack
from aoob_pipeline.source_index import SourceIndex


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


def var_value_sequence(prep: PrepPack, *, cap: int = 12) -> list[str]:
    """Functions the var explorer must open (capped; path + writes preferred)."""
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

    # Prefer functions that appear on write sites for flagged symbols.
    write_funcs: list[str] = []
    for sym in prep.symbols:
        for line in sym.write_lines:
            # write_lines are line numbers; function_sequence lists names — use those
            pass
        for name in sym.function_sequence:
            if name and name != "global" and name not in seen:
                write_funcs.append(name)

    for name in write_funcs:
        if len(out) >= cap:
            break
        add(name)

    return out[:cap]


def seed_facts(prep: PrepPack, source: SourceIndex) -> list[Fact]:
    """Code-only facts from prep that already cite real source lines."""
    facts: list[Fact] = []
    for sym in prep.symbols:
        if not sym.declaration_line or not sym.declaration_text:
            continue
        quote = sym.declaration_text.strip()
        if not source.verify_quote(int(sym.declaration_line), quote, radius=3):
            # Still keep short declaration_text if line exists
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

    if prep.alarm_line:
        line_txt = source.line_text(int(prep.alarm_line))
        if line_txt:
            facts.append(
                Fact(
                    kind="alarm_site",
                    function=prep.enclosing_function or source.enclosing_function(int(prep.alarm_line)) or "",
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


def force_open_bodies(
    source: SourceIndex,
    sequence: list[str],
) -> list[dict]:
    """Deterministically load every sequence function body (coverage guarantee)."""
    bodies: list[dict] = []
    for name in sequence:
        payload = source.get_func(name)
        bodies.append(payload if isinstance(payload, dict) else {"function": name, "error": str(payload)})
    return bodies
