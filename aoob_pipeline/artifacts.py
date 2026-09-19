"""Persist per-alarm run artifacts under ``PVERs/<id>/agent_runs/<order>/``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def run_dir(pver_dir: Path, order: str, *, create: bool = True) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_order = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(order))
    path = pver_dir / "agent_runs" / safe_order / stamp
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def latest_run_dir(pver_dir: Path, order: str) -> Path | None:
    safe_order = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(order))
    base = pver_dir / "agent_runs" / safe_order
    if not base.is_dir():
        return None
    runs = sorted([p for p in base.iterdir() if p.is_dir()], reverse=True)
    return runs[0] if runs else None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
