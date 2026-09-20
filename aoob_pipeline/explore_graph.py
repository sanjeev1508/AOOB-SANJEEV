"""LangGraph explorers with move/get visit-all workflow + live events."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from aoob_pipeline import prompts
from aoob_pipeline.events import EventBus, preview_json
from aoob_pipeline.explore_support import (
    build_symbol_package,
    call_path_edges,
    call_path_sequence,
    compact_explore_pack,
    dataflow_symbol_paths,
    force_open_bodies,
    mine_facts_from_body,
    normalize_llm_fact,
    relevance_tokens,
    seed_facts,
    var_value_sequence,
)
from aoob_pipeline.llm import build_agent_llm
from aoob_pipeline.schemas import ExplorePack, Fact, PrepPack
from aoob_pipeline.session import ExploreCursor
from aoob_pipeline.source_index import SourceIndex
from aoob_pipeline.pseudocode import enrich_func_payload
from aoob_pipeline.tools import make_explore_tools


class ExploreState(TypedDict):
    messages: Annotated[list, add_messages]
    tool_rounds: int
    idle_turns: int
    done: int


def _prep_brief(
    prep: PrepPack,
    role: Literal["call_path", "var_value"],
    sequence: list[str],
    *,
    code_driven: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": role,
        "order": prep.order,
        "location": prep.location,
        "enclosing_function": prep.enclosing_function,
        "alarm_line": prep.alarm_line,
        "index_expression": prep.index_expression,
        "index_origin": prep.index_origin,
        "value_origin_callees": prep.value_origin_callees,
        "array_name": prep.array_name,
        "array_size": prep.array_size,
        "local_guard": prep.local_guard.model_dump(),
        "coverage_cap": prep.coverage_cap,
        "function_sequence": sequence,
        "visit_instructions": (
            [
                "Functions are opened for you one at a time (no tools).",
                "For each body, return ONLY facts about the listed symbols (ALL of them for var_value).",
            ]
            if code_driven
            else [
                "Start at step 1 (already positioned).",
                "Call get_current() to load the current function (pseudocode + CITE lines).",
                "Extract facts: copy quotes ONLY from the CITE block (exact C).",
                "Call move_func(direction='next') and repeat until remaining=[].",
                "Call visit_status() anytime to check coverage.",
                "Then submit_explore_pack with a non-empty facts array.",
            ]
        ),
        "path_classes": [
            {
                "class_id": c.class_id,
                "sequence": [s.model_dump() for s in c.sequence],
                "member_path_ids": c.member_path_ids,
                "cluster_note": c.cluster_note,
            }
            for c in prep.path_classes
        ],
        "edges": prep.edges,
    }
    if role == "var_value":
        payload["symbols"] = [s.model_dump() for s in prep.symbols]
        payload["symbol_paths"] = dataflow_symbol_paths(prep)
        payload["extract_rule"] = (
            "For EVERY symbol in symbols[], record declaration + writes/guards/size "
            "when present in the opened bodies. Tag each fact.symbol explicitly."
        )
    return payload


def _tool_focus(name: str, args: dict[str, Any], cursor: ExploreCursor) -> tuple[list[str], str]:
    if name == "get_current":
        cur = cursor.current() or ""
        return ([cur] if cur else [], f"reading current function {cur}")
    if name == "get_func":
        fn = str(args.get("name") or "")
        return ([fn] if fn else [], f"reading function {fn}")
    if name == "move_func":
        return ([cursor.current() or ""], f"move {args.get('direction') or args.get('step') or 'next'}")
    if name == "get_lines":
        return ([], f"reading lines {args.get('start')}–{args.get('end')}")
    if name == "visit_status":
        return ([], "checking visit coverage")
    if name == "submit_explore_pack":
        return ([], "submitting explore pack")
    return ([], f"tool {name}")


def _cursor_payload(
    cursor: ExploreCursor,
    *,
    role: Literal["call_path", "var_value"],
    symbols: list[str],
    done: bool = False,
) -> dict[str, Any]:
    seq = list(cursor.sequence)
    cur = cursor.current()
    idx = cursor.index + 1 if cur else len(cursor.opened)
    payload: dict[str, Any] = {
        "current": cur,
        "index": idx,
        "total": len(seq),
        "sequence": seq,
        "done": done,
    }
    if role == "var_value":
        payload["symbols"] = symbols
    return payload


def _build_explore_graph(
    llm: Any,
    tools: list,
    sink: dict[str, Any],
    cursor: ExploreCursor,
    max_rounds: int,
    *,
    agent: str,
    bus: EventBus | None,
    role: Literal["call_path", "var_value"] = "call_path",
    symbols: list[str] | None = None,
):
    # Prefer requiring a tool call while exploration is incomplete.
    try:
        bound = llm.bind_tools(tools, tool_choice="any")
    except TypeError:
        bound = llm.bind_tools(tools)
    sym_names = list(symbols or [])

    def agent_node(state: ExploreState) -> dict:
        if sink.get("pack") is not None:
            return {"done": 1}
        if bus:
            bus.emit(
                "agent_thinking",
                agent=agent,
                stage="explore",
                title=f"{agent} reasoning",
                activity="planning next move/get/submit",
                functions=[cursor.current()] if cursor.current() else cursor.remaining()[:5],
                cursor=_cursor_payload(cursor, role=role, symbols=sym_names),
            )
        response = bound.invoke(state["messages"])
        idle = int(state.get("idle_turns", 0))
        if isinstance(response, AIMessage) and response.tool_calls:
            idle = 0
            for call in response.tool_calls:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {}) or {}
                funcs, activity = _tool_focus(str(name), dict(args), cursor)
                if bus:
                    bus.emit(
                        "tool_call",
                        agent=agent,
                        stage="explore",
                        title=f"{agent} → {name}",
                        tool=str(name),
                        detail=preview_json(args, 800),
                        functions=funcs,
                        activity=activity,
                        cursor=_cursor_payload(cursor, role=role, symbols=sym_names),
                    )
        else:
            idle += 1
            # Nudge instead of ending — this was the main failure mode.
            remaining = cursor.remaining()
            nudge = (
                f"You must use tools. visit_status={json.dumps(cursor.status())}. "
                f"Remaining functions to open: {remaining or '[] (then submit_explore_pack)'}. "
                "Call get_current() now if remaining is non-empty; otherwise submit_explore_pack."
            )
            return {
                "messages": [response, HumanMessage(content=nudge)],
                "tool_rounds": state.get("tool_rounds", 0),
                "idle_turns": idle,
            }
        return {
            "messages": [response],
            "tool_rounds": state.get("tool_rounds", 0),
            "idle_turns": idle,
        }

    def tools_node(state: ExploreState) -> dict:
        result = ToolNode(tools).invoke(state)
        if bus:
            for msg in result.get("messages") or []:
                if not isinstance(msg, ToolMessage):
                    continue
                content = str(msg.content)
                name = getattr(msg, "name", None) or "tool"
                funcs: list[str] = []
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        if parsed.get("function"):
                            funcs = [str(parsed["function"])]
                        elif parsed.get("current"):
                            funcs = [str(parsed["current"])]
                except Exception:  # noqa: BLE001
                    pass
                bus.emit(
                    "tool_result",
                    agent=agent,
                    stage="explore",
                    title=f"{agent} ← {name}",
                    tool=str(name),
                    output_preview=content[:1200],
                    functions=funcs,
                    activity=f"got result from {name}",
                    cursor=_cursor_payload(cursor, role=role, symbols=sym_names),
                )
        return {
            **result,
            "tool_rounds": int(state.get("tool_rounds", 0)) + 1,
            "idle_turns": 0,
        }

    def route(state: ExploreState) -> str:
        if sink.get("pack") is not None or state.get("done"):
            return "end"
        if int(state.get("tool_rounds", 0)) >= max_rounds:
            return "end"
        if int(state.get("idle_turns", 0)) >= 4:
            return "end"
        last = state["messages"][-1] if state.get("messages") else None
        # After a nudge HumanMessage, go back to agent
        if isinstance(last, HumanMessage):
            return "agent"
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        if isinstance(last, AIMessage):
            return "agent"
        return "end"

    graph = StateGraph(ExploreState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", "agent": "agent", "end": END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def _pack_from_sink(
    agent: str,
    sink: dict[str, Any],
    seeds: list[Fact],
    error: str | None = None,
) -> ExplorePack:
    submitted = sink.get("pack") or {}
    facts: list[Fact] = list(seeds)
    seen = {(f.function, f.line, f.quote) for f in facts}
    for item in submitted.get("facts") or []:
        try:
            fact = Fact(**item)
        except Exception:  # noqa: BLE001
            continue
        key = (fact.function, fact.line, fact.quote)
        if key in seen:
            continue
        seen.add(key)
        facts.append(fact)
    notes = list(submitted.get("notes") or [])
    if seeds:
        notes.append(f"seeded {len(seeds)} deterministic facts from prep/source")
    if sink.get("rejects"):
        notes.append(f"submit_rejects={len(sink['rejects'])}")
    return ExplorePack(agent=agent, facts=facts, notes=notes, error=error)


def _fallback_facts_from_bodies(
    agent: str,
    sequence: list[str],
    bodies: list[dict],
    prep: PrepPack,
) -> list[Fact]:
    """If the LLM never submitted, keep at least alarm/index lines from opened bodies."""
    facts: list[Fact] = []
    needles = [x for x in [prep.index_expression, prep.array_name] if x]
    for name, body in zip(sequence, bodies):
        snippet = str(body.get("raw_snippet") or body.get("snippet") or "")
        if body.get("error") or not snippet:
            continue
        for line in snippet.splitlines():
            if ":" not in line:
                continue
            num_s, text = line.split(":", 1)
            try:
                ln = int(num_s.strip())
            except ValueError:
                continue
            text = text.strip()
            if not text or text.startswith("PSEUDO") or text.startswith("CITE") or text.startswith("#"):
                continue
            if needles and not any(n in text for n in needles):
                continue
            facts.append(
                Fact(
                    kind="path_step" if agent.startswith("CALL") else "value_site",
                    function=name,
                    line=ln,
                    quote=text[:300],
                    symbol=prep.index_expression,
                    note="fallback extract after forced open",
                )
            )
            break
    return facts


def _extract_json_list(text: str) -> list[dict[str, Any]]:
    raw = (text or "").strip()
    if "</think>" in raw:
        raw = raw.split("</think>")[-1].strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                return []
            data = json.loads(raw[start : end + 1])
        else:
            data = json.loads(raw[start : end + 1])
    if isinstance(data, dict) and "facts" in data:
        data = data["facts"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _run_explore_code_driven(
    *,
    agent: str,
    system: str,
    role: Literal["call_path", "var_value"],
    prep: PrepPack,
    source: SourceIndex,
    sequence: list[str],
    focus: str | None,
    bus: EventBus | None,
) -> ExplorePack:
    """Python opens every function; LLM only extracts facts (7B-friendly)."""
    seeds = seed_facts(prep, source)
    if not sequence:
        sequence = [prep.enclosing_function] if prep.enclosing_function else []

    brief = _prep_brief(prep, role, sequence, code_driven=True)
    if focus:
        brief["reexplore_focus"] = focus
    seq_edges = [
        {"s": sequence[i], "t": sequence[i + 1]}
        for i in range(len(sequence) - 1)
    ]
    symbol_paths = dataflow_symbol_paths(prep) if role == "var_value" else []
    if bus:
        bus.emit(
            "agent_input",
            agent=agent,
            stage="explore",
            title=f"Input → {agent} (code-driven)",
            input_preview=preview_json(brief),
            functions=sequence,
            edges=seq_edges,
            activity=f"code opens {len(sequence)} funcs; LLM extracts only",
            cursor={
                "current": sequence[0] if sequence else None,
                "index": 1 if sequence else 0,
                "total": len(sequence),
                "sequence": sequence,
                "symbols": [s.symbol_name for s in prep.symbols] if role == "var_value" else [],
            },
            call_path=(
                {"sequence": sequence, "edges": seq_edges}
                if role == "call_path"
                else None
            ),
            var_paths=symbol_paths if role == "var_value" else None,
        )

    extract_system = prompts.code_driven_extract_prompt(role)
    relevant = relevance_tokens(prep, sequence)
    all_facts: list[Fact] = list(seeds)
    opened: list[str] = []
    notes = ["explore_mode=code_driven"]
    dropped_llm = 0

    for step_i, name in enumerate(sequence, start=1):
        body = source.get_func(name, near_line=prep.alarm_line if name == prep.enclosing_function else None)
        opened.append(name)
        prev_name = sequence[step_i - 2] if step_i > 1 else None
        if bus:
            bus.emit(
                "tool_result",
                agent=agent,
                stage="explore",
                title=f"{agent} ← get_func({name})",
                tool="get_func",
                output_preview=str(body.get("snippet") or body.get("error") or "")[:1200],
                functions=[name],
                edges=seq_edges,
                activity=f"opened {name} ({step_i}/{len(sequence)})",
                cursor={
                    "current": name,
                    "prev": prev_name,
                    "index": step_i,
                    "total": len(sequence),
                    "sequence": sequence,
                    "symbols": [s.symbol_name for s in prep.symbols] if role == "var_value" else [],
                },
            )
        mined = mine_facts_from_body(agent=agent, function=name, body=body, prep=prep)
        for f in mined:
            all_facts.append(f)

        # Small LLM pass: extract additional quoted facts from this body only
        if body.get("error") or not body.get("snippet"):
            continue
        enriched = enrich_func_payload(body, focus_tokens=relevant)
        llm_view = str(enriched.get("snippet") or "")
        if not llm_view:
            continue
        if bus:
            bus.emit(
                "agent_thinking",
                agent=agent,
                stage="explore",
                title=f"{agent} extract @ {name}",
                activity="JSON fact extract on pseudocode+cite (no tools)",
                functions=[name],
                cursor={
                    "current": name,
                    "index": step_i,
                    "total": len(sequence),
                    "sequence": sequence,
                    "symbols": [s.symbol_name for s in prep.symbols] if role == "var_value" else [],
                },
            )
            ratio = enriched.get("compress_ratio")
            bus.emit(
                "tool_result",
                agent=agent,
                stage="explore",
                title=f"{agent} ← pseudo({name})",
                tool="pseudocode",
                output_preview=llm_view[:1200],
                functions=[name],
                activity=(
                    f"compressed {enriched.get('original_chars')}→"
                    f"{enriched.get('compressed_chars')} chars"
                    + (f" ({ratio})" if ratio is not None else "")
                ),
            )
        sym_names = [s.symbol_name for s in prep.symbols if s.symbol_name]
        human = (
            f"function={name}\nrole={role}\n"
            f"index_expression={prep.index_expression}\n"
            f"index_tokens={sorted(t for t in relevant if t != prep.array_name and t not in sequence)}\n"
            f"array={prep.array_name}\narray_size={prep.array_size}\n"
            f"symbols={sym_names}\n"
            f"alarm_line={prep.alarm_line}\n"
            f"BODY (PSEUDO for logic; CITE for exact quotes):\n{llm_view[:16000]}"
        )
        try:
            llm = build_agent_llm(agent, prompt_chars=len(extract_system) + len(human))
            msg = llm.invoke(
                [SystemMessage(content=extract_system), HumanMessage(content=human)]
            )
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            for item in _extract_json_list(content):
                fact = normalize_llm_fact(
                    item, function=name, prep=prep, source=source, relevant=relevant
                )
                if fact is None:
                    dropped_llm += 1
                    continue
                all_facts.append(fact)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"extract_failed@{name}:{exc}")

    if dropped_llm:
        notes.append(f"dropped {dropped_llm} irrelevant/unverifiable LLM facts")

    # Dedup
    seen: set[tuple] = set()
    deduped: list[Fact] = []
    for f in all_facts:
        key = (f.function, f.line, f.kind, f.quote[:100])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)

    pack = compact_explore_pack(
        ExplorePack(
            agent=agent,
            facts=deduped,
            notes=[*notes, f"opened={opened}", f"seeded {len(seeds)} deterministic facts"],
        ),
        role=role,
    )
    if bus:
        bus.emit(
            "agent_output",
            agent=agent,
            stage="explore",
            title=f"{agent} done — returned explore pack",
            output_preview=preview_json(
                {
                    "agent": pack.agent,
                    "fact_count": len(pack.facts),
                    "facts": [f.model_dump() for f in pack.facts[:20]],
                    "notes": pack.notes,
                }
            ),
            functions=opened,
            edges=seq_edges,
            activity=f"done · {len(pack.facts)} facts (code-driven, compacted)",
            cursor={
                "current": opened[-1] if opened else None,
                "prev": opened[-2] if len(opened) > 1 else None,
                "index": len(opened),
                "total": len(sequence),
                "sequence": sequence,
                "done": True,
                "symbols": [s.symbol_name for s in prep.symbols] if role == "var_value" else [],
            },
        )
    return pack


def _run_explore(
    *,
    agent: str,
    system: str,
    role: Literal["call_path", "var_value"],
    prep: PrepPack,
    source: SourceIndex,
    sequence: list[str],
    max_rounds: int,
    focus: str | None,
    bus: EventBus | None,
) -> ExplorePack:
    import os

    mode = (os.getenv("AOOB_EXPLORE_MODE") or "code_driven").strip().lower()
    if mode in {"code", "code_driven", "deterministic"}:
        return _run_explore_code_driven(
            agent=agent,
            system=system,
            role=role,
            prep=prep,
            source=source,
            sequence=sequence,
            focus=focus,
            bus=bus,
        )

    # Legacy tool-agent path (needs strong tool-calling models)
    seeds = seed_facts(prep, source)
    if not sequence:
        sequence = [prep.enclosing_function] if prep.enclosing_function else []

    brief = _prep_brief(prep, role, sequence)
    if focus:
        brief["reexplore_focus"] = focus

    sym_names = [s.symbol_name for s in prep.symbols if s.symbol_name]
    seq_edges = [
        {"s": sequence[i], "t": sequence[i + 1]}
        for i in range(len(sequence) - 1)
    ]
    symbol_paths = dataflow_symbol_paths(prep) if role == "var_value" else []
    if bus:
        bus.emit(
            "agent_input",
            agent=agent,
            stage="explore",
            title=f"Input → {agent}",
            input_preview=preview_json(brief),
            functions=sequence,
            activity=f"sequence n={len(sequence)}; tool-agent mode",
            edges=seq_edges
            or [
                {"s": str(e.get("caller")), "t": str(e.get("callee"))}
                for e in prep.edges[:30]
                if e.get("caller") and e.get("callee")
            ],
            cursor={
                "current": sequence[0] if sequence else None,
                "index": 1 if sequence else 0,
                "total": len(sequence),
                "sequence": sequence,
                "symbols": sym_names if role == "var_value" else [],
            },
            call_path=(
                {"sequence": sequence, "edges": seq_edges}
                if role == "call_path"
                else None
            ),
            var_paths=symbol_paths if role == "var_value" else None,
        )

    cursor = ExploreCursor(sequence=list(sequence))
    relevant = relevance_tokens(prep, sequence)
    tools, sink = make_explore_tools(source, cursor, focus_tokens=relevant)
    llm = build_agent_llm(agent)
    rounds = max(max_rounds, max(8, len(sequence) * 3 + 4))
    graph = _build_explore_graph(
        llm,
        tools,
        sink,
        cursor,
        max_rounds=rounds,
        agent=agent,
        bus=bus,
        role=role,
        symbols=sym_names,
    )

    start_msg = (
        f"You are {agent}. function_sequence has {len(sequence)} function(s). "
        "Open EVERY one with get_current/move_func before submit.\n\nCASE BRIEF:\n"
        + json.dumps(brief, indent=2, ensure_ascii=False)
    )

    try:
        graph.invoke(
            {
                "messages": [
                    SystemMessage(content=system),
                    HumanMessage(content=start_msg),
                ],
                "tool_rounds": 0,
                "idle_turns": 0,
                "done": 0,
            }
        )
    except Exception as exc:  # noqa: BLE001
        if bus:
            bus.emit(
                "agent_error",
                agent=agent,
                stage="explore",
                title=f"{agent} error",
                detail=str(exc),
                activity="error",
            )

    if sink.get("pack") is None:
        if bus:
            bus.emit(
                "stage",
                agent=agent,
                stage="explore",
                title=f"{agent} code-driven fallback",
                activity="tool-agent failed; switching to code-driven",
                functions=sequence,
            )
        return _run_explore_code_driven(
            agent=agent,
            system=system,
            role=role,
            prep=prep,
            source=source,
            sequence=sequence,
            focus=focus,
            bus=bus,
        )

    pack = compact_explore_pack(_pack_from_sink(agent, sink, seeds), role=role)
    if bus:
        bus.emit(
            "agent_output",
            agent=agent,
            stage="explore",
            title=f"Output ← {agent}",
            output_preview=preview_json(pack.model_dump()),
            functions=[f.function for f in pack.facts if f.function],
            activity=f"produced {len(pack.facts)} facts; opened={sorted(cursor.opened)}",
            cursor=_cursor_payload(
                cursor, role=role, symbols=sym_names, done=True
            ),
        )
    return pack


def run_call_path_explore(
    prep: PrepPack,
    source: SourceIndex,
    *,
    max_rounds: int = 12,
    focus: str | None = None,
    bus: EventBus | None = None,
) -> ExplorePack:
    sequence = call_path_sequence(prep)
    if prep.skip_path_explore and not focus:
        seeds = seed_facts(prep, source)
        if bus:
            bus.emit(
                "agent_output",
                agent="CALL_PATH_EXPLORE",
                stage="explore",
                title="CALL_PATH_EXPLORE short-circuit (local guard)",
                output_preview=preview_json([f.model_dump() for f in seeds]),
                functions=sequence,
                activity="local guard present — seed facts only",
                cursor={
                    "current": sequence[0] if sequence else None,
                    "index": 1 if sequence else 0,
                    "total": len(sequence),
                    "sequence": sequence,
                    "done": True,
                },
                call_path={
                    "sequence": sequence,
                    "edges": call_path_edges(prep),
                },
            )
        return compact_explore_pack(
            ExplorePack(
                agent="CALL_PATH_EXPLORE",
                facts=seeds,
                notes=["local guard short-circuit; sequence tools skipped"],
            ),
            role="call_path",
        )
    return _run_explore(
        agent="CALL_PATH_EXPLORE",
        system=prompts.CALL_PATH_EXPLORE,
        role="call_path",
        prep=prep,
        source=source,
        sequence=sequence,
        max_rounds=max_rounds,
        focus=focus,
        bus=bus,
    )


def _run_var_symbol_driven(
    *,
    prep: PrepPack,
    source: SourceIndex,
    focus: str | None,
    bus: EventBus | None,
) -> ExplorePack:
    """Visit each dataflow symbol once; LLM extracts from ±3 window packages."""
    agent = "VAR_VALUE_EXPLORE"
    seeds = seed_facts(prep, source)
    symbols = [s for s in prep.symbols if (s.symbol_name or "").strip()]
    sym_names = [s.symbol_name for s in symbols]
    symbol_paths = dataflow_symbol_paths(prep)
    packages = [build_symbol_package(s, prep, source, radius=3) for s in symbols]
    # Marker hops: primary function of each symbol (symbol→symbol travel)
    hop_funcs = [str(p.get("primary_function") or prep.enclosing_function or "") for p in packages]
    hop_edges = [
        {"s": hop_funcs[i], "t": hop_funcs[i + 1], "kind": "symbol_hop"}
        for i in range(len(hop_funcs) - 1)
        if hop_funcs[i] and hop_funcs[i + 1]
    ]

    brief = {
        "role": "var_value",
        "mode": "symbol_driven",
        "order": prep.order,
        "location": prep.location,
        "enclosing_function": prep.enclosing_function,
        "alarm_line": prep.alarm_line,
        "index_expression": prep.index_expression,
        "array_name": prep.array_name,
        "array_size": prep.array_size,
        "symbols": sym_names,
        "symbol_paths": symbol_paths,
        "visit_instructions": [
            "Symbols are visited one at a time (not function-to-function).",
            "Each package merges ±3 line windows around that symbol's dataflow lines.",
            "Return ONLY facts about the current symbol.",
        ],
    }
    if focus:
        brief["reexplore_focus"] = focus

    first_fn = hop_funcs[0] if hop_funcs else prep.enclosing_function
    if bus:
        bus.emit(
            "agent_input",
            agent=agent,
            stage="explore",
            title=f"Input → {agent} (symbol-driven)",
            input_preview=preview_json(brief),
            functions=[first_fn] if first_fn else [],
            edges=hop_edges,
            activity=f"symbol-by-symbol · {len(symbols)} symbols · ±3 windows",
            cursor={
                "current": first_fn,
                "prev": None,
                "index": 1 if symbols else 0,
                "total": len(symbols),
                "sequence": hop_funcs,
                "symbols": sym_names,
                "current_symbol": sym_names[0] if sym_names else None,
            },
            var_paths=symbol_paths,
        )

    extract_system = prompts.code_driven_extract_prompt("var_value")
    relevant = relevance_tokens(prep, hop_funcs)
    all_facts: list[Fact] = list(seeds)
    notes = ["explore_mode=symbol_driven", "window_radius=±3"]
    dropped_llm = 0
    covered: list[str] = []
    prev_fn: str | None = None

    for step_i, (sym, package) in enumerate(zip(symbols, packages), start=1):
        name = sym.symbol_name
        primary = str(package.get("primary_function") or "")
        covered.append(name)
        if bus:
            bus.emit(
                "tool_result",
                agent=agent,
                stage="explore",
                title=f"{agent} ← symbol_package({name})",
                tool="symbol_package",
                output_preview=str(package.get("package_text") or "")[:1200],
                functions=[primary] if primary else [],
                edges=hop_edges,
                activity=f"symbol {name} ({step_i}/{len(symbols)}) · windows={len(package.get('windows') or [])}",
                cursor={
                    "current": primary or None,
                    "prev": prev_fn,
                    "index": step_i,
                    "total": len(symbols),
                    "sequence": hop_funcs,
                    "symbols": sym_names,
                    "current_symbol": name,
                },
                var_paths=symbol_paths,
            )

        # Deterministic mining on each function that touches this symbol
        for fn in package.get("functions") or []:
            body = source.get_func(
                fn,
                near_line=prep.alarm_line if fn == prep.enclosing_function else None,
            )
            for f in mine_facts_from_body(agent=agent, function=fn, body=body, prep=prep):
                if f.symbol and f.symbol != name and f.kind not in {"alarm_site", "array_size_hint"}:
                    # keep only facts for this symbol (plus size/alarm)
                    if name not in (f.quote or "") and name not in (f.note or ""):
                        continue
                all_facts.append(f.model_copy(update={"symbol": f.symbol or name}))

        pkg_text = str(package.get("package_text") or "")
        if not pkg_text.strip():
            notes.append(f"empty_package@{name}")
            prev_fn = primary or prev_fn
            continue

        if bus:
            bus.emit(
                "agent_thinking",
                agent=agent,
                stage="explore",
                title=f"{agent} extract @ symbol {name}",
                activity="JSON fact extract on ±3 window package (no tools)",
                functions=[primary] if primary else [],
                cursor={
                    "current": primary or None,
                    "prev": prev_fn,
                    "index": step_i,
                    "total": len(symbols),
                    "sequence": hop_funcs,
                    "symbols": sym_names,
                    "current_symbol": name,
                },
            )

        human = (
            f"current_symbol={name}\nrole=var_value\n"
            f"functions={package.get('functions')}\n"
            f"key_lines={package.get('key_lines')}\n"
            f"windows={package.get('windows')}\n"
            f"index_expression={prep.index_expression}\n"
            f"array={prep.array_name}\narray_size={prep.array_size}\n"
            f"alarm_line={prep.alarm_line}\n"
            f"PACKAGE (±3 merged windows; quote ONLY from these CITE lines):\n{pkg_text[:16000]}"
        )
        try:
            llm = build_agent_llm(agent, prompt_chars=len(extract_system) + len(human))
            msg = llm.invoke(
                [SystemMessage(content=extract_system), HumanMessage(content=human)]
            )
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            for item in _extract_json_list(content):
                # Force symbol tag to current
                if isinstance(item, dict) and not item.get("symbol"):
                    item = {**item, "symbol": name}
                fn_hint = primary or prep.enclosing_function or ""
                fact = normalize_llm_fact(
                    item, function=fn_hint, prep=prep, source=source, relevant=relevant
                )
                if fact is None:
                    dropped_llm += 1
                    continue
                if not fact.symbol:
                    fact = fact.model_copy(update={"symbol": name})
                all_facts.append(fact)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"extract_failed@{name}:{exc}")

        prev_fn = primary or prev_fn

    if dropped_llm:
        notes.append(f"dropped {dropped_llm} irrelevant/unverifiable LLM facts")

    seen: set[tuple] = set()
    deduped: list[Fact] = []
    for f in all_facts:
        key = (f.function, f.line, f.kind, f.quote[:100], f.symbol or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)

    pack = compact_explore_pack(
        ExplorePack(
            agent=agent,
            facts=deduped,
            notes=[
                *notes,
                f"symbols_covered={covered}",
                f"seeded {len(seeds)} deterministic facts",
            ],
        ),
        role="var_value",
    )
    if bus:
        bus.emit(
            "agent_output",
            agent=agent,
            stage="explore",
            title=f"{agent} done — returned explore pack",
            output_preview=preview_json(
                {
                    "agent": pack.agent,
                    "fact_count": len(pack.facts),
                    "facts": [f.model_dump() for f in pack.facts[:30]],
                    "notes": pack.notes,
                    "symbols_covered": covered,
                }
            ),
            functions=hop_funcs,
            edges=hop_edges,
            activity=f"done · {len(pack.facts)} facts · {len(covered)} symbols",
            cursor={
                "current": hop_funcs[-1] if hop_funcs else None,
                "prev": hop_funcs[-2] if len(hop_funcs) > 1 else None,
                "index": len(symbols),
                "total": len(symbols),
                "sequence": hop_funcs,
                "symbols": sym_names,
                "current_symbol": sym_names[-1] if sym_names else None,
                "done": True,
            },
            var_paths=symbol_paths,
        )
    return pack


def run_var_value_explore(
    prep: PrepPack,
    source: SourceIndex,
    *,
    max_rounds: int = 12,
    focus: str | None = None,
    bus: EventBus | None = None,
) -> ExplorePack:
    """Symbol-by-symbol VAR explore (dataflow packages with ±3 windows)."""
    _ = max_rounds  # retained for API compatibility with call_path explore
    return _run_var_symbol_driven(prep=prep, source=source, focus=focus, bus=bus)
