from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.embeddings import Embedder
from pydantic_ai.settings import ModelSettings

from vexic.embeddings import EMBEDDING_DIM
from vexic.models import ContradictionJudgment, FactCandidate, RemBoostPlan


PROVIDER = "openai"

EXTRACTION_INSTRUCTIONS = """\
Extract only durable user facts stated in the transcript.
Use the closed category vocabulary exactly: preference, fact, goal, event,
relationship, skill, constraint, context.
Every candidate must include source_message_ids from the [message_id=N] markers.
Return an empty list when there are no durable user facts.\
"""

REM_INSTRUCTIONS = """\
Assign reinforcement boosts only for candidate_id values present in the prompt.
Use 0 for isolated or weak candidates and higher values for mutually reinforcing
or important candidates. Do not invent, rewrite, promote, or retire facts.\
"""

CONTRADICTION_INSTRUCTIONS = """\
Judge whether the new fact contradicts the existing fact.
Return contradicts=false unless both facts cannot be true at the same time.\
"""


def _env_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def _require_openai_key() -> None:
    key = os.environ.get("OPENAI_API_KEY")
    if key is None or not key.strip():
        raise RuntimeError("OPENAI_API_KEY is required for the live retrieval adapter.")


def _require_openai_model(model: str, env_name: str) -> str:
    if not model.startswith("openai:"):
        raise RuntimeError(f"{env_name} must use an openai: model for this adapter.")
    return model


def _reject_passed_secrets(secrets: Mapping[str, str] | None) -> None:
    if secrets:
        raise RuntimeError(
            "openai_live_adapter.py reads provider secrets from environment variables only."
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
        return _require_openai_model(group_model, group_env)
    if default_model := os.environ.get("VEXIC_LIVE_MODEL"):
        return _require_openai_model(default_model, "VEXIC_LIVE_MODEL")
    return "openai:gpt-4o-mini"


def _embedding_model_name() -> str:
    model = os.environ.get("VEXIC_LIVE_EMBEDDING_MODEL")
    if model:
        return _require_openai_model(model, "VEXIC_LIVE_EMBEDDING_MODEL")
    return "openai:text-embedding-3-small"


def _model_settings() -> ModelSettings:
    return {
        "max_tokens": _int_env("VEXIC_LIVE_MAX_OUTPUT_TOKENS", 512),
        "timeout": _float_env("VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS", 60.0),
    }


def _agent(model_group: str, output_type: Any, instructions: str) -> Agent[None, Any]:
    _require_openai_key()
    return Agent(
        _model_name(model_group),
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


def build_rem_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, RemBoostPlan]:
    _reject_passed_secrets(secrets)
    return _agent(model_group, RemBoostPlan, REM_INSTRUCTIONS)


def build_contradiction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, ContradictionJudgment]:
    _reject_passed_secrets(secrets)
    return _agent(model_group, ContradictionJudgment, CONTRADICTION_INSTRUCTIONS)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    _require_openai_key()
    model = _embedding_model_name()
    result = Embedder(model).embed_documents_sync(
        texts,
        settings={"dimensions": EMBEDDING_DIM},
    )
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
