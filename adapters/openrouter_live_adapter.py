from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.embeddings import Embedder
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from vexic.embeddings import EMBEDDING_DIM
from vexic.longmemeval import (
    LONGMEMEVAL_RECALL_JUDGE_PROMPT,
    LongMemEvalRecallJudgeVerdict,
)
from vexic.models import ContradictionJudgment, FactCandidate


PROVIDER = "openrouter"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter provider preference: route only to model providers that neither
# retain nor train on prompts. Transcript text and stored fact text travel in
# these requests, so data handling is pinned per request instead of inheriting
# the OpenRouter account default.
OPENROUTER_PROVIDER_PREFERENCES: dict[str, Any] = {
    "provider": {"data_collection": "deny"},
}
# NOTE(alpha): single embedding worker; add a pool only if live runs need parallel embeddings.
_EMBED_EXECUTOR = ThreadPoolExecutor(max_workers=1)

EXTRACTION_INSTRUCTIONS = """\
Extract only durable user facts stated in the transcript.
Use the closed category vocabulary exactly: preference, fact, goal, event,
relationship, skill, constraint, context.
Every candidate must include source_message_ids from the [message_id=N] markers.
When the transcript states or clearly implies a temporal reference for when
the fact occurred (a date, month, year, or relative time you can resolve
against context), populate occurred_at with an ISO 8601 string at whatever
precision is actually known: a full date ("2025-03-14"), a year-month
("2025-03"), or a year ("2025"). Never fabricate a day or month you were not
told. Leave occurred_at null when no temporal reference exists. Look
especially hard for a date on category="event" facts, since event facts
should carry an occurred_at whenever the transcript gives any basis for one.
Return an empty list when there are no durable user facts.\
"""

CONTRADICTION_INSTRUCTIONS = """\
Judge whether the new fact contradicts the existing fact.
Return contradicts=false unless both facts cannot be true at the same time.\
"""

SUMMARY_INSTRUCTIONS = """\
Write a concise, factual summary of this transcript span.
Capture concrete decisions, facts, and open threads/action items.
Do not speculate beyond what the transcript states. No preamble or
meta-commentary -- return only the summary body.\
"""

DEFAULT_SUMMARY_MODEL = "deepseek/deepseek-v4-pro"


def _env_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def _require_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key is None or not key.strip():
        raise RuntimeError("OPENROUTER_API_KEY is required for the live retrieval adapter.")
    return key.strip()


def _require_openrouter_model(model: str, env_name: str) -> str:
    model = model.strip()
    if ":" in model or "/" not in model:
        raise RuntimeError(
            f"{env_name} must use an OpenRouter model id like deepseek/deepseek-v4-pro."
        )
    return model


def _reject_passed_secrets(secrets: Mapping[str, str] | None) -> None:
    if secrets:
        raise RuntimeError(
            "openrouter_live_adapter.py reads provider secrets from environment variables only."
        )


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than 0.")
    return parsed


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise RuntimeError(f"{name} must be a finite number greater than 0.")
    return parsed


def _model_name(model_group: str) -> str:
    group_key = _env_key(model_group)
    group_env = f"VEXIC_LIVE_{group_key}_MODEL"
    if group_model := os.environ.get(group_env):
        return _require_openrouter_model(group_model, group_env)
    if default_model := os.environ.get("VEXIC_LIVE_MODEL"):
        return _require_openrouter_model(default_model, "VEXIC_LIVE_MODEL")
    return "deepseek/deepseek-v4-pro"


def _summary_model_name() -> str:
    model = os.environ.get("VEXIC_SUMMARY_MODEL")
    if model:
        return _require_openrouter_model(model, "VEXIC_SUMMARY_MODEL")
    return DEFAULT_SUMMARY_MODEL


def _embedding_model_name() -> str:
    model = os.environ.get("VEXIC_LIVE_EMBEDDING_MODEL")
    if model:
        return _require_openrouter_model(model, "VEXIC_LIVE_EMBEDDING_MODEL")
    return "openai/text-embedding-3-small"


def _model_settings() -> ModelSettings:
    return {
        "max_tokens": _int_env("VEXIC_LIVE_MAX_OUTPUT_TOKENS", 512),
        "timeout": _float_env("VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS", 60.0),
        "extra_body": OPENROUTER_PROVIDER_PREFERENCES,
    }


def _provider() -> OpenAIProvider:
    return OpenAIProvider(
        base_url=OPENROUTER_BASE_URL,
        api_key=_require_openrouter_key(),
    )


def _agent(model_group: str, output_type: Any, instructions: str) -> Agent[None, Any]:
    return Agent(
        OpenAIChatModel(_model_name(model_group), provider=_provider()),
        output_type=output_type,
        instructions=instructions,
        model_settings=_model_settings(),
    )


def _agent_with_model(model_name: str, output_type: Any, instructions: str) -> Agent[None, Any]:
    return Agent(
        OpenAIChatModel(model_name, provider=_provider()),
        output_type=output_type,
        instructions=instructions,
        model_settings=_model_settings(),
    )


def build_extraction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, list[FactCandidate]]:
    _reject_passed_secrets(secrets)
    return _agent(model_group, list[FactCandidate], EXTRACTION_INSTRUCTIONS)


def build_contradiction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, ContradictionJudgment]:
    _reject_passed_secrets(secrets)
    return _agent(model_group, ContradictionJudgment, CONTRADICTION_INSTRUCTIONS)


def build_summary_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, str]:
    # `model_group` is intentionally unused: the summary model is env-driven
    # via `VEXIC_SUMMARY_MODEL` (see `_summary_model_name`), not routed by
    # model group like the other agent builders in this module.
    _reject_passed_secrets(secrets)
    return _agent_with_model(_summary_model_name(), str, SUMMARY_INSTRUCTIONS)


def build_longmemeval_recall_judge_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, LongMemEvalRecallJudgeVerdict]:
    # Recall judging is deterministic grading, not generation: pin temperature 0
    # on top of the shared env-driven settings.
    _reject_passed_secrets(secrets)
    settings = _model_settings()
    settings["temperature"] = 0
    return Agent(
        OpenAIChatModel(_model_name(model_group), provider=_provider()),
        output_type=LongMemEvalRecallJudgeVerdict,
        instructions=LONGMEMEVAL_RECALL_JUDGE_PROMPT,
        model_settings=settings,
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = _embedding_model_name()
    result = _EMBED_EXECUTOR.submit(
        lambda: Embedder(
            OpenAIEmbeddingModel(model, provider=_provider())
        ).embed_documents_sync(
            texts,
            settings={
                "dimensions": EMBEDDING_DIM,
                "extra_body": OPENROUTER_PROVIDER_PREFERENCES,
            },
        )
    ).result()
    embeddings = [list(embedding) for embedding in result.embeddings]
    bad_dimensions = [
        len(embedding)
        for embedding in embeddings
        if len(embedding) != EMBEDDING_DIM
    ][:5]
    if bad_dimensions:
        raise RuntimeError(
            f"{model} returned embedding dimensions {bad_dimensions}; "
            f"expected {EMBEDDING_DIM}."
        )
    return embeddings
