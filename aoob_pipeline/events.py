"""Thread-safe pipeline event stream for UI transparency."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


EmitFn = Callable[[dict[str, Any]], None]


class EventBus:
    """Append-only event log shared with the classify job + optional JSONL file."""

    def __init__(self, *, jsonl_path: Path | None = None, on_event: EmitFn | None = None):
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._focus: dict[str, Any] = {}
        self._jsonl = jsonl_path
        self._on_event = on_event
        if self._jsonl:
            self._jsonl.parent.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        with self._lock:
            return list(self._events), dict(self._focus)

    def emit(
        self,
        kind: str,
        *,
        agent: str | None = None,
        stage: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        input_preview: str | None = None,
        output_preview: str | None = None,
        tool: str | None = None,
        functions: list[str] | None = None,
        edges: list[dict[str, str]] | None = None,
        activity: str | None = None,
        cursor: dict[str, Any] | None = None,
        call_path: dict[str, Any] | None = None,
        var_paths: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "ts": time.time(),
            "kind": kind,
            "agent": agent,
            "stage": stage,
            "title": title or kind,
            "detail": detail,
            "input_preview": _clip(input_preview),
            "output_preview": _clip(output_preview),
            "tool": tool,
            "functions": functions or [],
            "edges": edges or [],
            "activity": activity,
        }
        if cursor is not None:
            event["cursor"] = cursor
        if call_path is not None:
            event["call_path"] = call_path
        if var_paths is not None:
            event["var_paths"] = var_paths
        if extra:
            event["extra"] = extra
        with self._lock:
            self._events.append(event)
            touch = (
                functions is not None
                or edges is not None
                or activity is not None
                or agent is not None
                or cursor is not None
                or call_path is not None
                or var_paths is not None
            )
            if touch:
                if agent is not None:
                    self._focus["agent"] = agent
                if activity is not None:
                    self._focus["activity"] = activity
                if functions is not None:
                    self._focus["functions"] = list(functions)
                if edges is not None:
                    self._focus["edges"] = list(edges)
                if call_path is not None:
                    self._focus["call_path"] = dict(call_path)
                if var_paths is not None:
                    self._focus["var_paths"] = list(var_paths)
                if cursor is not None and agent:
                    cursors = dict(self._focus.get("cursors") or {})
                    cursors[agent] = dict(cursor)
                    self._focus["cursors"] = cursors
                self._focus["stage"] = stage or self._focus.get("stage")
                self._focus["updated_at"] = event["ts"]
            focus = dict(self._focus)
            if self._jsonl:
                with self._jsonl.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self._on_event:
            try:
                self._on_event({"event": event, "focus": focus})
            except Exception:  # noqa: BLE001
                pass
        return event


def _clip(text: str | None, limit: int = 8000) -> str | None:
    if text is None:
        return None
    raw = str(text)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + f"\n… [{len(raw) - limit} more chars]"


def preview_json(payload: Any, limit: int = 2400) -> str:
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except TypeError:
        text = str(payload)
    return _clip(text, limit) or ""
