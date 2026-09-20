"""Load per-agent backend / model settings from the project `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Backend = Literal["local", "network", "bosch"]

AGENT_KEYS = (
    "CALL_PATH_EXPLORE",
    "VAR_VALUE_EXPLORE",
    "TP_PROVE",
    "FP_PROVE",
    "FINAL_CLASSIFICATION",
)


@dataclass(frozen=True)
class AgentLLMConfig:
    name: str
    backend: Backend
    model: str
    base_url: str
    api_key: str | None
    api_version: str | None
    timeout: float
    num_ctx: int


@dataclass(frozen=True)
class PipelineConfig:
    project_root: Path
    path_class_cap: int = 15
    final_runs: int = 3
    max_tool_rounds: int = 12
    max_reexplore: int = 1
    ollama_fallback: bool = True
    tp_policy: str = "type_legal"  # type_legal | operating_range
    prover_brief_max_chars: int = 60000
    final_temperatures: tuple[float, ...] = (0.0, 0.3, 0.6)


def project_root_from(here: Path | None = None) -> Path:
    if here is None:
        here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".env").exists() or (candidate / "array_oob_analyzer.py").exists():
            return candidate
    return Path(__file__).resolve().parents[1]


def load_env(root: Path | None = None) -> Path:
    root = root or project_root_from()
    load_dotenv(root / ".env", override=False)
    return root


def _backend(raw: str | None) -> Backend:
    value = (raw or "local").strip().lower()
    if value not in {"local", "network", "bosch"}:
        return "local"
    return value  # type: ignore[return-value]


def resolve_agent(name: str, root: Path | None = None) -> AgentLLMConfig:
    root = load_env(root)
    backend = _backend(os.getenv(f"{name}_AGENT"))
    override = (os.getenv(f"{name}_MODEL") or "").strip()
    timeout = float(os.getenv("OLLAMA_TIMEOUT") or os.getenv("BOSCH_TIMEOUT") or 180)
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX") or 8192)

    if backend == "bosch":
        model = override or (os.getenv("BOSCH_MODEL") or "GPT-5-nano")
        return AgentLLMConfig(
            name=name,
            backend=backend,
            model=model,
            base_url=(os.getenv("BOSCH_BASE_URL") or "").rstrip("/"),
            api_key=os.getenv("MODEL_FARM_API_KEY"),
            api_version=os.getenv("BOSCH_API_VERSION") or "2024-05-01-preview",
            timeout=float(os.getenv("BOSCH_TIMEOUT") or timeout),
            num_ctx=num_ctx,
        )

    if backend == "network":
        model = override or (os.getenv("OLLAMA_NETWORK_MODEL") or os.getenv("OLLAMA_LOCAL_MODEL") or "qwen3:14b")
        return AgentLLMConfig(
            name=name,
            backend=backend,
            model=model,
            base_url=(os.getenv("OLLAMA_NETWORK_BASE_URL") or "http://127.0.0.1:11434").rstrip("/"),
            api_key=None,
            api_version=None,
            timeout=timeout,
            num_ctx=num_ctx,
        )

    model = override or (os.getenv("OLLAMA_LOCAL_MODEL") or "qwen3:14b")
    return AgentLLMConfig(
        name=name,
        backend="local",
        model=model,
        base_url=(os.getenv("OLLAMA_LOCAL_BASE_URL") or "http://127.0.0.1:11434").rstrip("/"),
        api_key=None,
        api_version=None,
        timeout=timeout,
        num_ctx=num_ctx,
    )


def pipeline_config(root: Path | None = None) -> PipelineConfig:
    root = load_env(root)
    return PipelineConfig(
        project_root=root,
        path_class_cap=int(os.getenv("AOOB_PATH_CLASS_CAP") or 15),
        final_runs=max(1, int(os.getenv("AOOB_FINAL_RUNS") or 3)),
        max_tool_rounds=int(os.getenv("AOOB_MAX_TOOL_ROUNDS") or 12),
        max_reexplore=int(os.getenv("AOOB_MAX_REEXPLORE") or 1),
        ollama_fallback=(os.getenv("AOOB_OLLAMA_RUNTIME_FALLBACK") or "1").strip() not in {"0", "false", "no"},
        tp_policy=(os.getenv("AOOB_TP_POLICY") or "type_legal").strip().lower(),
        prover_brief_max_chars=int(os.getenv("AOOB_PROVER_BRIEF_MAX_CHARS") or 40000),
        final_temperatures=_parse_temps(os.getenv("AOOB_FINAL_TEMPERATURES")),
    )


def _parse_temps(raw: str | None) -> tuple[float, ...]:
    out: list[float] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(max(0.0, min(1.5, float(part))))
        except ValueError:
            continue
    return tuple(out) or (0.0, 0.3, 0.6)
