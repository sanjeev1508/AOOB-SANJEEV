"""Build LangChain chat models from per-agent `.env` settings."""

from __future__ import annotations

import math
import os
import sys
from typing import Any

from aoob_pipeline.config import AgentLLMConfig, resolve_agent

BOSCH_HEADER_NAME = "genaiplatform-farm-subscription-key"

# Rough chars-per-token for mixed JSON / C source on Qwen/Gemma tokenizers.
_CHARS_PER_TOKEN = 3.0
# Headroom for the system prompt + the model's own output.
_CTX_HEADROOM_TOKENS = 2048


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def max_num_ctx() -> int:
    return int(os.getenv("AOOB_MAX_NUM_CTX") or 32768)


def estimate_tokens(text: str) -> int:
    return int(math.ceil(len(text or "") / _CHARS_PER_TOKEN))


def ctx_for_prompt(prompt_chars: int, base_ctx: int) -> int:
    """Smallest power-of-two context >= base that fits the prompt + headroom.

    Ollama silently drops the *start* of an over-long prompt (system prompt
    first), which makes the model summarise the tail instead of doing the
    task. Always size the window from the actual prompt.
    """
    needed = int(math.ceil(prompt_chars / _CHARS_PER_TOKEN)) + _CTX_HEADROOM_TOKENS
    ctx = max(base_ctx, 2048)
    while ctx < needed and ctx < max_num_ctx():
        ctx *= 2
    return min(ctx, max_num_ctx())


def prompt_fits(prompt_chars: int, num_ctx: int) -> bool:
    return estimate_tokens("x" * prompt_chars) + _CTX_HEADROOM_TOKENS <= num_ctx


def build_chat_model(
    cfg: AgentLLMConfig,
    *,
    num_ctx: int | None = None,
    temperature: float | None = None,
) -> Any:
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
            temperature=0.2 if temperature is None else temperature,
            max_tokens=4096,
            timeout=cfg.timeout,
        )

    from langchain_ollama import ChatOllama

    ctx = num_ctx or cfg.num_ctx
    temp = 0.1 if temperature is None else temperature
    _log(
        f"[llm] agent={cfg.name} backend={cfg.backend} model={cfg.model} "
        f"base_url={cfg.base_url} num_ctx={ctx} temperature={temp}"
    )
    return ChatOllama(
        model=cfg.model,
        base_url=cfg.base_url,
        temperature=temp,
        num_ctx=ctx,
        num_predict=4096,
        client_kwargs={"timeout": cfg.timeout},
    )


def build_agent_llm(
    agent_name: str,
    root=None,
    *,
    prompt_chars: int | None = None,
    temperature: float | None = None,
) -> Any:
    """Build the agent model; when ``prompt_chars`` is given, size num_ctx to fit."""
    cfg = resolve_agent(agent_name, root=root)
    ctx = None
    if prompt_chars is not None and cfg.backend != "bosch":
        ctx = ctx_for_prompt(prompt_chars, cfg.num_ctx)
        if not prompt_fits(prompt_chars, ctx):
            _log(
                f"[llm] WARNING agent={agent_name}: prompt ~{estimate_tokens('x' * prompt_chars)} "
                f"tokens exceeds AOOB_MAX_NUM_CTX={max_num_ctx()}; Ollama will truncate"
            )
    return build_chat_model(cfg, num_ctx=ctx, temperature=temperature)
