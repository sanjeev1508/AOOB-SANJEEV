"""LangChain tools bound per-run to a ``SourceIndex`` (safe for parallel graphs)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from aoob_pipeline.source_index import SourceIndex


def make_explore_tools(source: SourceIndex) -> tuple[list[StructuredTool], dict[str, Any]]:
    """Return (tools, sink). ``sink['pack']`` is set by ``submit_explore_pack``."""
    sink: dict[str, Any] = {"pack": None}

    def get_func(name: str, window: int = 0, near_line: int = 0) -> str:
        """Return source for a C function. Optional window around near_line (0 = full body)."""
        win = window if window and window > 0 else None
        near = near_line if near_line and near_line > 0 else None
        payload = source.get_func(name, window=win, near_line=near)
        snippet = payload.get("snippet") or ""
        if len(snippet) > 24000:
            payload = dict(payload)
            payload["snippet"] = snippet[:24000] + "\n/* ... truncated ... */"
            payload["truncated"] = True
        return json.dumps(payload, ensure_ascii=False)

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
            },
            ensure_ascii=False,
        )

    def submit_explore_pack(facts_json: str, notes_json: str = "[]") -> str:
        """Finish exploration. facts_json: JSON list of {kind,function,line,quote,...}."""
        try:
            facts = json.loads(facts_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"accepted": False, "error": f"facts_json not valid JSON: {exc}"})
        if not isinstance(facts, list):
            return json.dumps({"accepted": False, "error": "facts_json must be a JSON array"})
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
        sink["pack"] = {"facts": cleaned, "notes": [str(n) for n in notes]}
        return json.dumps({"accepted": True, "fact_count": len(cleaned)})

    tools = [
        StructuredTool.from_function(get_func),
        StructuredTool.from_function(get_lines),
        StructuredTool.from_function(submit_explore_pack),
    ]
    return tools, sink
