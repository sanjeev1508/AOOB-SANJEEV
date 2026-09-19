"""LangGraph ReAct explorers (call-path + var-value) with tools + live events."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from aoob_pipeline.events import EventBus, preview_json
from aoob_pipeline.llm import build_agent_llm
from aoob_pipeline.schemas import ExplorePack, Fact, PrepPack
from aoob_pipeline.source_index import SourceIndex
from aoob_pipeline.tools import make_explore_tools


class ExploreState(TypedDict):
    messages: Annotated[list, add_messages]
    tool_rounds: int
    done: int


CALL_PATH_SYSTEM = """You are CALL_PATH_EXPLORE for Astrée array-OOB triage.

The orchestrator already listed path classes and call edges. Confirm each edge
using get_func / get_lines. Record what argument the caller passes at the call site.

Rules:
- Every fact MUST be a quoted snippet with exact line and function from the tools.
- Do not invent values, sizes, or guards.
- Prefer one fact per confirmed edge (kind=call_edge or arg_binding).
- When finished, call submit_explore_pack with a JSON array of facts.
"""

VAR_VALUE_SYSTEM = """You are VAR_VALUE_EXPLORE for Astrée array-OOB triage.

Gather declaration, writes, and value-shaping evidence for the flagged symbols:
guards, clamps, masks, loop bounds, and call-argument bindings.
Skip pure reads EXCEPT when the read site is a guard/clamp/mask/loop-bound.

Rules:
- Every fact MUST quote real source with line + function from get_func / get_lines.
- Do not invent constants or ranges.
- kind examples: declaration, write, guard, clamp, mask, loop_bound, arg_binding.
- When finished, call submit_explore_pack with a JSON array of facts.
"""


def _prep_brief(prep: PrepPack, role: Literal["call_path", "var_value"]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order": prep.order,
        "location": prep.location,
        "enclosing_function": prep.enclosing_function,
        "alarm_line": prep.alarm_line,
        "index_expression": prep.index_expression,
        "array_name": prep.array_name,
        "array_size": prep.array_size,
        "local_guard": prep.local_guard.model_dump(),
        "coverage_cap": prep.coverage_cap,
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
    return payload


def _tool_focus(name: str, args: dict[str, Any]) -> tuple[list[str], str]:
    if name == "get_func":
        fn = str(args.get("name") or "")
        return ([fn] if fn else [], f"reading function {fn}")
    if name == "get_lines":
        return ([], f"reading lines {args.get('start')}–{args.get('end')}")
    if name == "submit_explore_pack":
        return ([], "submitting explore pack")
    return ([], f"tool {name}")


def _build_explore_graph(
    llm: Any,
    tools: list,
    sink: dict[str, Any],
    max_rounds: int,
    *,
    agent: str,
    bus: EventBus | None,
):
    bound = llm.bind_tools(tools)

    def agent_node(state: ExploreState) -> dict:
        if sink.get("pack") is not None:
            return {"done": 1}
        if bus:
            bus.emit(
                "agent_thinking",
                agent=agent,
                stage="explore",
                title=f"{agent} reasoning",
                activity="calling model / planning next tool",
            )
        response = bound.invoke(state["messages"])
        if bus and isinstance(response, AIMessage) and response.tool_calls:
            for call in response.tool_calls:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {}) or {}
                funcs, activity = _tool_focus(str(name), dict(args))
                bus.emit(
                    "tool_call",
                    agent=agent,
                    stage="explore",
                    title=f"{agent} → {name}",
                    tool=str(name),
                    detail=preview_json(args, 800),
                    functions=funcs,
                    activity=activity,
                )
        return {"messages": [response], "tool_rounds": state.get("tool_rounds", 0)}

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
                    if isinstance(parsed, dict) and parsed.get("function"):
                        funcs = [str(parsed["function"])]
                except Exception:  # noqa: BLE001
                    parsed = None
                bus.emit(
                    "tool_result",
                    agent=agent,
                    stage="explore",
                    title=f"{agent} ← {name}",
                    tool=str(name),
                    output_preview=content[:1200],
                    functions=funcs,
                    activity=f"got result from {name}",
                )
        return {**result, "tool_rounds": int(state.get("tool_rounds", 0)) + 1}

    def route(state: ExploreState) -> str:
        if sink.get("pack") is not None or state.get("done"):
            return "end"
        if int(state.get("tool_rounds", 0)) >= max_rounds:
            return "end"
        last = state["messages"][-1] if state.get("messages") else None
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "end"

    graph = StateGraph(ExploreState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", "end": END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def _pack_from_sink(agent: str, sink: dict[str, Any], error: str | None = None) -> ExplorePack:
    submitted = sink.get("pack") or {}
    facts = []
    for item in submitted.get("facts") or []:
        try:
            facts.append(Fact(**item))
        except Exception:  # noqa: BLE001
            continue
    return ExplorePack(
        agent=agent,
        facts=facts,
        notes=list(submitted.get("notes") or []),
        error=error,
    )


def _run_explore(
    *,
    agent: str,
    system: str,
    role: Literal["call_path", "var_value"],
    prep: PrepPack,
    source: SourceIndex,
    max_rounds: int,
    focus: str | None,
    bus: EventBus | None,
    skip: bool,
) -> ExplorePack:
    if skip:
        facts = []
        if prep.local_guard.found and prep.local_guard.line and prep.local_guard.quote:
            facts.append(
                Fact(
                    kind="local_guard_skip",
                    function=prep.local_guard.function or prep.enclosing_function or "",
                    line=prep.local_guard.line,
                    quote=prep.local_guard.quote,
                    note="path explore skipped: local guard candidate",
                )
            )
        pack = ExplorePack(agent=agent, facts=facts, notes=["skipped path explore"])
        if bus:
            bus.emit(
                "agent_output",
                agent=agent,
                stage="explore",
                title=f"{agent} skipped (local guard)",
                output_preview=preview_json(pack.model_dump()),
                functions=[f.function for f in facts if f.function],
                activity="skipped",
            )
        return pack

    brief = _prep_brief(prep, role)
    if focus:
        brief["reexplore_focus"] = focus
    if bus:
        funcs = []
        for edge in prep.edges[:40]:
            if edge.get("caller"):
                funcs.append(str(edge["caller"]))
            if edge.get("callee"):
                funcs.append(str(edge["callee"]))
        if prep.enclosing_function:
            funcs.append(prep.enclosing_function)
        bus.emit(
            "agent_input",
            agent=agent,
            stage="explore",
            title=f"Input → {agent}",
            input_preview=preview_json(brief),
            functions=list(dict.fromkeys(funcs)),
            activity="receiving prep pack",
            edges=[
                {"s": str(e.get("caller")), "t": str(e.get("callee"))}
                for e in prep.edges[:30]
                if e.get("caller") and e.get("callee")
            ],
        )

    tools, sink = make_explore_tools(source)
    llm = build_agent_llm(agent)
    graph = _build_explore_graph(llm, tools, sink, max_rounds=max_rounds, agent=agent, bus=bus)
    try:
        graph.invoke(
            {
                "messages": [
                    SystemMessage(content=system),
                    HumanMessage(
                        content=(
                            "Use get_func/get_lines as needed, then submit_explore_pack.\n\nPREP:\n"
                            + json.dumps(brief, indent=2, ensure_ascii=False)
                        )
                    ),
                ],
                "tool_rounds": 0,
                "done": 0,
            }
        )
        pack = _pack_from_sink(agent, sink)
        if bus:
            bus.emit(
                "agent_output",
                agent=agent,
                stage="explore",
                title=f"Output ← {agent}",
                output_preview=preview_json(pack.model_dump()),
                functions=[f.function for f in pack.facts if f.function],
                activity=f"produced {len(pack.facts)} facts",
            )
        return pack
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
        return ExplorePack(agent=agent, facts=[], error=str(exc), notes=[])


def run_call_path_explore(
    prep: PrepPack,
    source: SourceIndex,
    *,
    max_rounds: int = 12,
    focus: str | None = None,
    bus: EventBus | None = None,
) -> ExplorePack:
    return _run_explore(
        agent="CALL_PATH_EXPLORE",
        system=CALL_PATH_SYSTEM,
        role="call_path",
        prep=prep,
        source=source,
        max_rounds=max_rounds,
        focus=focus,
        bus=bus,
        skip=bool(prep.skip_path_explore and not focus),
    )


def run_var_value_explore(
    prep: PrepPack,
    source: SourceIndex,
    *,
    max_rounds: int = 12,
    focus: str | None = None,
    bus: EventBus | None = None,
) -> ExplorePack:
    return _run_explore(
        agent="VAR_VALUE_EXPLORE",
        system=VAR_VALUE_SYSTEM,
        role="var_value",
        prep=prep,
        source=source,
        max_rounds=max_rounds,
        focus=focus,
        bus=bus,
        skip=False,
    )
