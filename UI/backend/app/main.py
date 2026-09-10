"""FastAPI service for browsing Astree alarm and variable-analysis data."""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ALARMS_PATH = PROJECT_ROOT / "Full_alarms.csv"
DEFAULT_VARIABLE_INFO_PATH = PROJECT_ROOT / "array_oob_variable_info.json"
DEFAULT_SOURCE_PATH = PROJECT_ROOT / "input.c"


def _setting_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default


def _canonical_order(value: str) -> str:
    """Compare order IDs consistently with Astree's thousands separators."""
    return value.strip().replace(",", "")


def _read_alarm_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        first = stream.readline()
        # The export starts with the Excel-style separator declaration ``sep=;``.
        if not first.lower().startswith("sep="):
            stream.seek(0)
        reader = csv.DictReader(stream, delimiter=";")
        return [{key.strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def _read_variable_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    if not isinstance(payload, list):
        raise ValueError("variable info JSON must contain an array")
    return [row for row in payload if isinstance(row, dict)]


def _read_function_snippets(path: Path) -> dict[str, dict[str, Any]]:
    """Index function bodies so graph-node clicks can show real source text."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    signature = re.compile(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(\{)?")
    snippets: dict[str, dict[str, Any]] = {}
    for start, line in enumerate(lines):
        match = signature.search(line)
        if not match:
            continue
        if not match.group(2):
            if start + 1 >= len(lines) or not lines[start + 1].strip().startswith("{"):
                continue
        depth = line.count("{") - line.count("}")
        if depth == 0:
            depth = lines[start + 1].count("{") - lines[start + 1].count("}")
            end = start + 1
        else:
            end = start
        while depth > 0 and end + 1 < len(lines):
            end += 1
            depth += lines[end].count("{") - lines[end].count("}")
        name = match.group(1)
        snippets[name] = {
            "start_line": start + 1,
            "end_line": end + 1,
            "text": "\n".join(lines[start:end + 1]),
        }
    return snippets


class AlarmStore:
    """Load the two source exports once and expose merged alarm records."""

    def __init__(self, alarms_path: Path, variable_info_path: Path, source_path: Path | None = None) -> None:
        self.alarms_path = alarms_path
        self.variable_info_path = variable_info_path
        csv_rows = _read_alarm_rows(alarms_path)
        variable_rows = _read_variable_rows(variable_info_path)
        snippets = _read_function_snippets(source_path) if source_path and source_path.exists() else {}
        by_location = {
            str(row.get("location", "")).strip(): row for row in variable_rows
        }
        self.records: list[dict[str, Any]] = []
        self.by_order: dict[str, dict[str, Any]] = {}

        for alarm in csv_rows:
            variable = by_location.get(alarm.get("Location", ""))
            if variable is None:
                variable = {}

            order_id = alarm.get("Order", "")
            record = {
                "order_id": order_id,
                "order_key": _canonical_order(order_id),
                "type": alarm.get("Type", ""),
                "category": alarm.get("Category", ""),
                "location": alarm.get("Location", ""),
                "classification": alarm.get("Classification", ""),
                "comment": alarm.get("Comment", ""),
                "message": alarm.get("Message", ""),
                "group_id": variable.get("group_id"),
                "variable": variable.get("variable"),
                "path_count": variable.get("no_of_paths", len(variable.get("paths", []))),
                "function_names": self._function_names(variable.get("paths", [])),
                "variable_info": variable.get("variable_info"),
                "variable_infos": variable.get("variable_infos", []),
                "symbol_infos": variable.get("symbol_infos", []),
                "paths": variable.get("paths", []),
                "function_snippets": {
                    name.split("|", 1)[0]: snippets.get(name.split("|", 1)[0])
                    for path in variable.get("paths", [])
                    for name in path.get("function_sequence", [])
                    if snippets.get(name.split("|", 1)[0])
                },
            }
            record["alarm"] = {
                "order": alarm.get("Order", ""),
                "type": alarm.get("Type", ""),
                "category": alarm.get("Category", ""),
                "location": alarm.get("Location", ""),
                "message": alarm.get("Message", ""),
            }
            record["group"] = variable or None
            self.records.append(record)
            self.by_order[record["order_key"]] = record

    @staticmethod
    def _function_names(paths: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for path in paths:
            for entry in path.get("function_sequence", []):
                if entry not in names:
                    names.append(entry)
        return names

    def summaries(self) -> list[dict[str, Any]]:
        return [
            {
                key: record[key]
                for key in (
                    "order_id",
                    "type",
                    "category",
                    "location",
                    "classification",
                    "comment",
                    "message",
                    "group_id",
                    "variable",
                    "path_count",
                    "function_names",
                )
            }
            for record in self.records
        ]

    def get(self, order_id: str) -> dict[str, Any] | None:
        record = self.by_order.get(_canonical_order(order_id))
        if record is None or record["group"] is None:
            return None
        return record


def _build_store() -> AlarmStore:
    return AlarmStore(
        _setting_path("ASTREE_ALARMS_PATH", DEFAULT_ALARMS_PATH),
        _setting_path("ASTREE_VARIABLE_INFO_PATH", DEFAULT_VARIABLE_INFO_PATH),
        _setting_path("ASTREE_SOURCE_PATH", DEFAULT_SOURCE_PATH),
    )


app = FastAPI(title="Astree Alarm Explorer", version="1.0.0")
origins = [
    origin.strip()
    for origin in os.getenv(
        "ASTREE_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_store: AlarmStore | None = None


def get_store() -> AlarmStore:
    global _store
    if _store is None:
        _store = _build_store()
    return _store


@app.get("/api/alarms")
def list_alarms() -> dict[str, Any]:
    store = get_store()
    return {"items": store.summaries(), "total": len(store.records)}


@app.get("/api/alarm/{order_id}")
def get_alarm(order_id: str) -> dict[str, Any]:
    record = get_store().get(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Alarm {order_id} was not found")
    return {
        "alarm": record["alarm"],
        "group": record["group"],
        # Keep the flattened fields for the current frontend and older clients.
        **{key: value for key, value in record.items() if key not in {"alarm", "group"}},
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
