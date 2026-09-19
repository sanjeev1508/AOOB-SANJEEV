"""Build LangChain chat models from per-agent `.env` settings."""

from __future__ import annotations

import sys
from typing import Any

from aoob_pipeline.config import AgentLLMConfig, resolve_agent

BOSCH_HEADER_NAME = "genaiplatform-farm-subscription-key"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_chat_model(cfg: AgentLLMConfig) -> Any:
    """Return a LangChain chat model for one agent backend."""
    if cfg.backend == "bosch":
        from langchain_openai import ChatOpenAI

        if not cfg.api_key:
            raise RuntimeError("Set MODEL_FARM_API_KEY for Bosch backend.")
        headers = {BOSCH_HEADER_NAME: cfg.api_key, "api-key": cfg.api_key}
        default_query = {"api-version": cfg.api_version} if cfg.api_version else None
        _log(f"[llm] agent={cfg.name} backend=bosch model={cfg.model}")
        return ChatOpenAI(
            model=cfg.model,
            api_key="unused-auth-is-via-header",
            base_url=cfg.base_url,
            default_headers=headers,
            default_query=default_query,
            temperature=0.2,
            max_tokens=4096,
            timeout=cfg.timeout,
        )

    from langchain_ollama import ChatOllama

    _log(
        f"[llm] agent={cfg.name} backend={cfg.backend} model={cfg.model} "
        f"base_url={cfg.base_url} num_ctx={cfg.num_ctx}"
    )
    return ChatOllama(
        model=cfg.model,
        base_url=cfg.base_url,
        temperature=0.1,
        num_ctx=cfg.num_ctx,
        num_predict=4096,
        client_kwargs={"timeout": cfg.timeout},
    )


def build_agent_llm(agent_name: str, root=None) -> Any:
    return build_chat_model(resolve_agent(agent_name, root=root))
