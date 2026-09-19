"""Merge explore packs and attach facts to path classes."""

from __future__ import annotations

from aoob_pipeline.schemas import ExplorePack, Fact, MergedPack, PrepPack
from aoob_pipeline.source_index import SourceIndex


def _fact_key(fact: Fact) -> tuple:
    return (fact.line, fact.kind, fact.symbol or "", " ".join(fact.quote.split())[:80])


def _verify_facts(facts: list[Fact], source: SourceIndex, seen: set[tuple]) -> list[Fact]:
    out: list[Fact] = []
    for fact in facts:
        key = _fact_key(fact)
        if key in seen:
            continue
        ok = source.verify_quote(fact.line, fact.quote)
        if not ok:
            continue
        seen.add(key)
        out.append(fact.model_copy(update={"verified": True}))
    return out


def merge_packs(
    prep: PrepPack,
    call_path: ExplorePack,
    var_value: ExplorePack,
    source: SourceIndex,
) -> MergedPack:
    # Dedup across BOTH packs — seeds are emitted by each explorer, and the
    # duplicated rows used to be repeated again per path class downstream.
    seen: set[tuple] = set()
    call_facts = _verify_facts(call_path.facts, source, seen)
    var_facts = _verify_facts(var_value.facts, source, seen)

    # Seed local-guard as a verified var fact when present.
    if prep.local_guard.found and prep.local_guard.line and prep.local_guard.quote:
        seed = Fact(
            kind="local_guard",
            function=prep.local_guard.function or prep.enclosing_function or "",
            line=prep.local_guard.line,
            quote=prep.local_guard.quote,
            symbol=prep.index_expression,
            note=prep.local_guard.note,
            verified=True,
        )
        if _fact_key(seed) not in seen and source.verify_quote(seed.line, seed.quote):
            seen.add(_fact_key(seed))
            var_facts.append(seed)

    class_ids = [c.class_id for c in prep.path_classes]
    class_funcs = {
        c.class_id: {s.function for s in c.sequence}
        for c in prep.path_classes
    }
    # Functions whose facts apply to every class: globals, the alarm function
    # itself, and value-origin callees (they are reached from every path).
    shared_funcs = {"global", "", prep.enclosing_function or "", *prep.value_origin_callees}

    by_class: dict[str, list[Fact]] = {cid: [] for cid in class_ids}
    unattached: list[Fact] = []
    for fact in [*call_facts, *var_facts]:
        attached = False
        for cid, funcs in class_funcs.items():
            if fact.function in funcs or fact.function in shared_funcs:
                by_class[cid].append(fact)
                attached = True
        if not attached:
            unattached.append(fact)

    coverage = prep.coverage_cap
    notes = list(prep.notes)
    if unattached:
        notes.append(f"{len(unattached)} facts not tied to a path-class function")
    # If explorers returned nothing useful and no local guard / seeds, mark partial.
    if not call_facts and not var_facts and not prep.local_guard.found:
        coverage = "partial"
        notes.append("explorers produced no verified facts")
    elif call_facts or var_facts:
        # Verified facts exist — keep prep coverage_cap unless already partial.
        pass

    return MergedPack(
        prep=prep,
        call_path=call_path.model_copy(update={"facts": call_facts}),
        var_value=var_value.model_copy(update={"facts": var_facts}),
        facts_by_class=by_class,
        coverage=coverage,  # type: ignore[arg-type]
        notes=notes,
    )
