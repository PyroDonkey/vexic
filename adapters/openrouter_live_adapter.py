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

# An earlier "only durable user facts stated in the transcript" wording read
# as personal-bio facts, so assistant-heavy working sessions -- the common
# shape of agent transcripts -- extracted zero candidates even though the
# user's own requests and corrections established durable facts about their
# projects, stack, and working preferences. The instructions name that shape
# explicitly and narrow the empty-list escape to transcripts that establish
# nothing durable at all.
#
# The subject paragraph is ADR 0039 option A. With no guidance on the field at
# all, the model emitted "User" for nearly every fact, collapsing real entities
# into one bucket; the normalizations shipped alongside that ADR only fold
# case/whitespace variants of a token, so the mega-bucket itself is prompt
# work. Subject stays a plain string -- no contract or schema change.
EXTRACTION_INSTRUCTIONS = """\
Extract durable facts about the user from the transcript.
Durable user facts are stated directly ("I use uv for all my projects") or
clearly established by the user's own requests and corrections: the projects
they are building, the tools, languages, and environments they work in, how
they prefer to work, and preferences their corrections reveal.
Most transcripts are working sessions, not conversations about the user. A
transcript where the user only directs coding work still establishes durable
facts about their projects and working preferences; extract those rather
than returning an empty list.
Do not extract one-off task mechanics -- a specific bug fixed, a command that
was run, code the assistant wrote -- unless they establish something durable
about the user or their projects. Ground every fact in what the user said or
asked for, not in what the assistant did on its own.
Use the closed category vocabulary exactly: preference, fact, goal, event,
relationship, skill, constraint, context.
Set subject to whoever or whatever the fact is actually about. When a fact is
about a specific named entity -- a person, pet, place, organization, product,
or tool -- the subject is that entity's own name ("Rachel", "Luna",
"AutoCAD LT 2013"), not the user.
Reserve the subject "User" for facts that are genuinely about the user
themselves -- their own preferences, goals, skills, and constraints -- and
write it exactly as "User", never a synonym such as "the user".
When a fact is about the user's own work -- their projects, tools, employer,
or workflow -- and the transcript gives no proper name for it, keep the
subject "User" rather than inventing a descriptive label such as "the user's
project" or "their CAD workflow".
Subject is a key, not a substitute for the statement: keep fact_text
self-contained and name the entity in it as well.
Every candidate must include source_message_ids from the [message_id=N] markers.
Because every fact is grounded in the user, source_message_ids must include
at least one User message -- the request or correction that establishes the
fact. Cite the Assistant message that carries the detail alongside it, never
alone.
Each transcript line's marker may carry observed=YYYY-MM-DD Day -- the date
that message was recorded. Observed time is recording time, never event time:
never copy an observed date into occurred_at by itself.
Populate occurred_at only from temporal references in the transcript text:
- An absolute date stated in the text: copy it at exactly its stated
  precision -- "2025-03-14" for a full date, "2025-03" for a month, "2025"
  for a year. If the text states a month and day but no year, use the
  observed date's year only when tense and context make the year
  unambiguous; otherwise leave occurred_at null. Never invent a year.
- A relative reference ("last Sunday", "three weekends ago", "back in
  March"): resolve it against the observed date of the message that says it,
  and only when the resolution is unambiguous. Output only the precision the
  resolution supports: "last Sunday" against a known observed date gives a
  full date; "a few months ago" gives at most a year-month; "years ago"
  resolves to nothing -- leave it null.
Never fabricate any component: no invented days, months, or years, and no
defaulting missing components to 01. When in doubt, less precision or null.
Leave occurred_at null when no temporal reference exists. Look especially
hard for a date on category="event" facts.
Return an empty list only when the transcript establishes no durable user
facts at all, not merely because the session is task-focused.\
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

# Per-agent output-token caps. The default model is a reasoning model whose
# thinking tokens count against max_tokens, so a cap sized to the visible
# output starves the agent into finish_reason=length before it emits anything:
# 512 killed extraction, and the same 512 later killed Deep's contradiction
# judgment even though that judgment is one boolean. Size every cap for the
# reasoning ahead of the output, not the output. max_tokens is a ceiling, not
# a spend, so headroom is free. VEXIC_LIVE_MAX_OUTPUT_TOKENS overrides all.
EXTRACTION_MAX_OUTPUT_TOKENS = 8192  # reasoning + structured candidate list
SUMMARY_MAX_OUTPUT_TOKENS = 8192  # reasoning + prose summary
CONTRADICTION_MAX_OUTPUT_TOKENS = 8192  # reasoning + single boolean judgment


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


def _judge_model_name(model_group: str) -> str:
    # Recall verdicts must never be graded by the implicit deepseek default:
    # diagnostics label the run with the judge model group, so a silent
    # fallback would attribute scores to the wrong judge. Require an explicit
    # model instead of falling through like `_model_name`.
    group_env = f"VEXIC_LIVE_{_env_key(model_group)}_MODEL"
    if group_model := os.environ.get(group_env):
        return _require_openrouter_model(group_model, group_env)
    if default_model := os.environ.get("VEXIC_LIVE_MODEL"):
        return _require_openrouter_model(default_model, "VEXIC_LIVE_MODEL")
    raise RuntimeError(
        f"Recall judging requires an explicit model: set {group_env} or VEXIC_LIVE_MODEL."
    )


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


def _model_settings(default_max_tokens: int | None) -> ModelSettings:
    # `None` omits the cap entirely; every capped caller states its own budget,
    # so no agent can silently inherit another agent's ceiling.
    settings: ModelSettings = {
        "timeout": _float_env("VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS", 60.0),
        "extra_body": OPENROUTER_PROVIDER_PREFERENCES,
    }
    if default_max_tokens is not None:
        settings["max_tokens"] = _int_env(
            "VEXIC_LIVE_MAX_OUTPUT_TOKENS", default_max_tokens
        )
    return settings


def _provider() -> OpenAIProvider:
    return OpenAIProvider(
        base_url=OPENROUTER_BASE_URL,
        api_key=_require_openrouter_key(),
    )


def _agent(
    model_group: str,
    output_type: Any,
    instructions: str,
    *,
    default_max_tokens: int,
) -> Agent[None, Any]:
    return Agent(
        OpenAIChatModel(_model_name(model_group), provider=_provider()),
        output_type=output_type,
        instructions=instructions,
        model_settings=_model_settings(default_max_tokens),
    )


def _agent_with_model(
    model_name: str,
    output_type: Any,
    instructions: str,
    *,
    default_max_tokens: int,
) -> Agent[None, Any]:
    return Agent(
        OpenAIChatModel(model_name, provider=_provider()),
        output_type=output_type,
        instructions=instructions,
        model_settings=_model_settings(default_max_tokens),
    )


def build_extraction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, list[FactCandidate]]:
    _reject_passed_secrets(secrets)
    return _agent(
        model_group,
        list[FactCandidate],
        EXTRACTION_INSTRUCTIONS,
        default_max_tokens=EXTRACTION_MAX_OUTPUT_TOKENS,
    )


def build_contradiction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, ContradictionJudgment]:
    _reject_passed_secrets(secrets)
    return _agent(
        model_group,
        ContradictionJudgment,
        CONTRADICTION_INSTRUCTIONS,
        default_max_tokens=CONTRADICTION_MAX_OUTPUT_TOKENS,
    )


def build_summary_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, str]:
    # `model_group` is intentionally unused: the summary model is env-driven
    # via `VEXIC_SUMMARY_MODEL` (see `_summary_model_name`), not routed by
    # model group like the other agent builders in this module.
    _reject_passed_secrets(secrets)
    return _agent_with_model(
        _summary_model_name(),
        str,
        SUMMARY_INSTRUCTIONS,
        default_max_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
    )


def build_longmemeval_recall_judge_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Agent[None, LongMemEvalRecallJudgeVerdict]:
    # Recall judging is deterministic grading, not generation: pin temperature 0
    # on top of the shared env-driven settings. The output cap is dropped
    # entirely so a long structured verdict reason cannot truncate into a
    # judge error.
    _reject_passed_secrets(secrets)
    settings = _model_settings(None)
    settings["temperature"] = 0
    return Agent(
        OpenAIChatModel(_judge_model_name(model_group), provider=_provider()),
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
    # Count before dimensions: callers zip embeddings against their inputs with
    # `strict=True`, so a short provider response would otherwise surface far
    # downstream as a bare ValueError -- the same exception class libSQL raises
    # for storage faults, which makes the two indistinguishable in an incident.
    if len(embeddings) != len(texts):
        raise RuntimeError(
            f"{model} returned {len(embeddings)} embeddings for {len(texts)} "
            "inputs; expected exactly one embedding per input text."
        )
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
