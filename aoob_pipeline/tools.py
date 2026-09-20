"""LangChain tools: move/get over a function sequence + submit with visit gate."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from aoob_pipeline.pseudocode import enrich_func_payload
from aoob_pipeline.session import ExploreCursor
from aoob_pipeline.source_index import SourceIndex


def _clip_snippet(payload: dict[str, Any], limit: int = 24000) -> dict[str, Any]:
    snippet = payload.get("snippet") or ""
    if len(snippet) <= limit:
        return payload
    out = dict(payload)
    out["snippet"] = snippet[:limit] + "\n/* ... truncated ... */"
    out["truncated"] = True
    return out


def _prepare_body(
    payload: dict[str, Any],
    *,
    focus_tokens: set[str] | None = None,
) -> dict[str, Any]:
    """Compress C → pseudocode+cite before the LLM sees the tool result."""
    if payload.get("error"):
        return payload
    body = enrich_func_payload(payload, focus_tokens=focus_tokens)
    return _clip_snippet(body)


def make_explore_tools(
    source: SourceIndex,
    cursor: ExploreCursor,
    *,
    focus_tokens: set[str] | None = None,
) -> tuple[list[StructuredTool], dict[str, Any]]:
    """Return (tools, sink). ``sink['pack']`` set only on accepted submit."""
    sink: dict[str, Any] = {"pack": None, "rejects": []}
    focus = focus_tokens

    def visit_status() -> str:
        """Show current index, opened functions, and remaining required visits."""
        return json.dumps(cursor.status(), ensure_ascii=False)

    def move_func(direction: str = "next", step: int = 0) -> str:
        """Move the sequence cursor. direction=next|prev, or step=1-based index. Does not return source — call get_current next."""
        return json.dumps(cursor.move(direction=direction, step=step), ensure_ascii=False)

    def get_current() -> str:
        """Return compact pseudocode + citeable C lines for the current sequence function and mark it visited."""
        name = cursor.current()
        if not name:
            return json.dumps({"error": "function_sequence is empty"})
        payload = source.get_func(name)
        if payload.get("error"):
            # Still mark attempted so we do not soft-lock on missing spans.
            cursor.mark_opened(name)
            return json.dumps({**payload, "visit_status": cursor.status()}, ensure_ascii=False)
        cursor.mark_opened(name)
        body = _prepare_body(payload, focus_tokens=focus)
        body["visit_status"] = cursor.status()
        return json.dumps(body, ensure_ascii=False)

    def get_func(name: str, window: int = 0, near_line: int = 0) -> str:
        """Return compact pseudocode + citeable C for a named function. Marks visited if in sequence."""
        win = window if window and window > 0 else None
        near = near_line if near_line and near_line > 0 else None
        payload = source.get_func(name, window=win, near_line=near)
        if name in cursor.sequence:
            cursor.mark_opened(name)
        body = _prepare_body(payload, focus_tokens=focus) if not payload.get("error") else payload
        if isinstance(body, dict):
            body = dict(body)
            body["visit_status"] = cursor.status()
        return json.dumps(body, ensure_ascii=False)

    def get_lines(start: int, end: int) -> str:
        """Return raw 1-based source lines from start to end inclusive (max 400 lines)."""
        if end < start:
            return json.dumps({"error": "end < start"})
        end_i = int(end)
        start_i = int(start)
        if end_i - start_i > 400:
            end_i = start_i + 400
        rows = source.get_lines(start_i, end_i)
        return json.dumps(
            {
                "start": start_i,
                "end": end_i,
                "snippet": "\n".join(f"{ln}: {txt}" for ln, txt in rows),
                "visit_status": cursor.status(),
            },
            ensure_ascii=False,
        )

    def submit_explore_pack(facts_json: str, notes_json: str = "[]") -> str:
        """Finish exploration after ALL sequence functions were opened. facts_json: JSON list of {kind,function,line,quote,...}."""
        remaining = cursor.remaining()
        if remaining:
            msg = {
                "accepted": False,
                "error": "Cannot submit yet — open every sequence function with get_current/get_func first.",
                "remaining": remaining,
                "visit_status": cursor.status(),
            }
            sink["rejects"].append(msg)
            return json.dumps(msg, ensure_ascii=False)
        try:
            facts = json.loads(facts_json)
        except json.JSONDecodeError as exc:
            msg = {"accepted": False, "error": f"facts_json not valid JSON: {exc}"}
            sink["rejects"].append(msg)
            return json.dumps(msg)
        if not isinstance(facts, list):
            msg = {"accepted": False, "error": "facts_json must be a JSON array"}
            sink["rejects"].append(msg)
            return json.dumps(msg)
        try:
            notes = json.loads(notes_json) if notes_json else []
        except json.JSONDecodeError:
            notes = [notes_json]
        if not isinstance(notes, list):
            notes = [str(notes)]
        cleaned = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            if not item.get("quote") or item.get("line") is None or not item.get("function"):
                continue
            cleaned.append(
                {
                    "kind": str(item.get("kind") or "fact"),
                    "function": str(item["function"]),
                    "line": int(item["line"]),
                    "quote": str(item["quote"]),
                    "symbol": item.get("symbol"),
                    "path_class_id": item.get("path_class_id"),
                    "note": item.get("note"),
                }
            )
        if not cleaned:
            msg = {
                "accepted": False,
                "error": "Empty facts rejected — quote at least one real line from opened function bodies.",
                "visit_status": cursor.status(),
            }
            sink["rejects"].append(msg)
            return json.dumps(msg, ensure_ascii=False)
        sink["pack"] = {"facts": cleaned, "notes": [str(n) for n in notes]}
        return json.dumps({"accepted": True, "fact_count": len(cleaned), "visit_status": cursor.status()})

    tools = [
        StructuredTool.from_function(visit_status),
        StructuredTool.from_function(move_func),
        StructuredTool.from_function(get_current),
        StructuredTool.from_function(get_func),
        StructuredTool.from_function(get_lines),
        StructuredTool.from_function(submit_explore_pack),
    ]
    return tools, sink
