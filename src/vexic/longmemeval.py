"""Native LongMemEval harness for Vexic memory evaluation.

This module intentionally ingests benchmark conversations through the normal
Tier 1 transcript surface so the benchmark measures Vexic memory behavior,
not a parallel fixture format.

Rehomed from the Coalescent source host (COA-342). Model-backed stages
(extraction, contradiction, recall judging) flow through host-supplied agent
factories per the host-port boundary; this module contains no provider wiring.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from vexic.deep import compute_score, run_deep_phase
from vexic.formatting import UNVERIFIED_NOTES_PREAMBLE, format_candidate_note
from vexic.pipeline import LIGHT_PHASE_BATCH_SIZE, run_light_phase
from vexic.ports import AgentFactory, EmbedTexts
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.rem import run_rem_phase
from vexic.storage import TranscriptHit, init_db, save_messages, search_messages
from vexic.storage.transcript import get_watermark
from vexic.subagents.retrieval import (
    retrieve_candidate_fallback,
    retrieve_long_term_facts,
)

LongMemEvalSplit = Literal["oracle", "s"]
AnswerMode = Literal["retrieval-debug", "tier2-debug", "tier3-debug", "judged-recall"]
LongMemEvalSelection = Literal["first", "stratified"]
LongMemEvalRecallJudgeVerdictValue = Literal["supported", "not_supported", "partial"]
LongMemEvalDreamShape = Literal[
    "skipped",
    "single-shot",
    "single-shot-light-rem",
    "incremental-per-session",
    "incremental-batched-sessions",
]

LONGMEMEVAL_RECALL_JUDGE_PROMPT_VERSION = "longmemeval-recall-judge-v1"
LONGMEMEVAL_RECALL_JUDGE_PROMPT = """\
You judge whether retrieved Vexic long-term memory facts contain or support
the benchmark gold answer.

Grade memory only. Do not answer from outside knowledge, hidden transcript
content, the current date, or assumptions. The gold answer is supplied only for
post-retrieval diagnostics; the retrieval query has already happened.

Return:
- supported: retrieved facts state the gold answer, paraphrase it, reformat it,
  or provide all inputs needed to derive it.
- partial: retrieved facts contain related evidence but not enough to fully
  support or derive the gold answer.
- not_supported: retrieved facts are empty, unrelated, contradictory, or missing
  required derivation inputs.
- confidence: a number from 0 to 1 for how clearly the retrieved facts support
  your verdict.

For temporal questions, mark supported only when the retrieved facts include the
actual temporal inputs needed to compute the answer. Do not resolve relative
dates such as "today", "last week", or "Monday" from your own context.
When unsure, prefer partial or not_supported and explain the missing evidence.\
"""

_LABEL_KEYS = {
    "answer",
    "answer_session_ids",
    "has_answer",
    "autoeval_label",
    "eval_label",
    "label",
}
_DEBUG_QUERY_STOPWORDS = {
    "a",
    "an",
    "did",
    "is",
    "mentioned",
    "the",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
}


@dataclass(frozen=True)
class LongMemEvalTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LongMemEvalSession:
    session_id: str
    timestamp: str
    turns: tuple[LongMemEvalTurn, ...]


@dataclass(frozen=True)
class LongMemEvalInstance:
    question_id: str
    question_type: str
    question: str
    question_date: str
    sessions: tuple[LongMemEvalSession, ...]
    answer: Any = None


def _strip_eval_labels(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_eval_labels(item)
            for key, item in value.items()
            if key not in _LABEL_KEYS
        }
    if isinstance(value, list):
        return [_strip_eval_labels(item) for item in value]
    return value


def _required_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"LongMemEval instance requires non-empty {key!r}.")
    return value


def _normalize_timestamp(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("LongMemEval session timestamp must be non-empty.")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        cleaned_text = re.sub(r"\s+\([A-Za-z]{3}\)", "", text)
        parsed = datetime.strptime(cleaned_text, "%Y/%m/%d %H:%M")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _timestamp_datetime(value: str) -> datetime:
    return datetime.fromisoformat(_normalize_timestamp(value))


def parse_longmemeval_instance(
    raw: dict[str, Any],
    *,
    max_sessions: int | None = None,
) -> LongMemEvalInstance:
    """Validate and sanitize one LongMemEval instance."""

    question_id = _required_str(raw, "question_id")
    question_type = _required_str(raw, "question_type")
    question = _required_str(raw, "question")
    question_date = _required_str(raw, "question_date")
    answer = raw.get("answer")
    sanitized = _strip_eval_labels(raw)

    session_ids = sanitized.get("haystack_session_ids")
    session_dates = sanitized.get("haystack_dates")
    sessions = sanitized.get("haystack_sessions")
    if (
        not isinstance(session_ids, list)
        or not isinstance(session_dates, list)
        or not isinstance(sessions, list)
    ):
        raise ValueError(
            "LongMemEval instance requires haystack_session_ids, haystack_dates, and haystack_sessions lists."
        )
    if not (len(session_ids) == len(session_dates) == len(sessions)):
        raise ValueError("LongMemEval haystack session fields must have matching lengths.")

    limit = len(sessions) if max_sessions is None else min(max_sessions, len(sessions))
    parsed_sessions: list[LongMemEvalSession] = []
    for index in range(limit):
        session_id = session_ids[index]
        timestamp = session_dates[index]
        raw_turns = sessions[index]
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("LongMemEval haystack_session_ids must contain non-empty strings.")
        if not isinstance(timestamp, str):
            raise ValueError("LongMemEval haystack_dates must contain strings.")
        if not isinstance(raw_turns, list):
            raise ValueError("LongMemEval haystack_sessions entries must be turn lists.")

        turns: list[LongMemEvalTurn] = []
        for turn in raw_turns:
            if not isinstance(turn, dict):
                raise ValueError("LongMemEval turns must be objects.")
            role = turn.get("role")
            content = turn.get("content")
            if role not in ("user", "assistant"):
                raise ValueError("LongMemEval turn role must be 'user' or 'assistant'.")
            if not isinstance(content, str):
                raise ValueError("LongMemEval turn content must be a string.")
            turns.append(LongMemEvalTurn(role=role, content=content))
        parsed_sessions.append(
            LongMemEvalSession(
                session_id=session_id,
                timestamp=_normalize_timestamp(timestamp),
                turns=tuple(turns),
            )
        )

    return LongMemEvalInstance(
        question_id=question_id,
        question_type=question_type,
        question=question,
        question_date=question_date,
        sessions=tuple(parsed_sessions),
        answer=answer,
    )


def _session_message(turn: LongMemEvalTurn, timestamp: str) -> ModelMessage:
    when = _timestamp_datetime(timestamp)
    if turn.role == "user":
        return ModelRequest(
            parts=[UserPromptPart(content=turn.content, timestamp=when)],
            timestamp=when,
        )
    return ModelResponse(
        parts=[TextPart(content=turn.content)],
        timestamp=when,
    )


def _sorted_sessions(instance: LongMemEvalInstance) -> list[LongMemEvalSession]:
    return sorted(
        instance.sessions,
        key=lambda item: _timestamp_datetime(item.timestamp),
    )


def _save_instance_session(
    db_path: str,
    instance: LongMemEvalInstance,
    session: LongMemEvalSession,
    *,
    forbidden_secret_values: list[str] | tuple[str, ...] = (),
) -> None:
    messages = [_session_message(turn, session.timestamp) for turn in session.turns]
    save_messages(
        db_path,
        messages,
        session_id=f"longmemeval:{instance.question_id}:{session.session_id}",
        timestamp=session.timestamp,
        forbidden_secret_values=forbidden_secret_values,
    )


def ingest_instance(
    db_path: str,
    instance: LongMemEvalInstance,
    *,
    forbidden_secret_values: list[str] | tuple[str, ...] = (),
) -> None:
    """Append one sanitized LongMemEval instance into an isolated memory DB."""

    init_db(db_path)
    for session in _sorted_sessions(instance):
        _save_instance_session(
            db_path,
            instance,
            session,
            forbidden_secret_values=forbidden_secret_values,
        )


@dataclass(frozen=True)
class LongMemEvalRunPaths:
    run_dir: Path
    predictions_path: Path
    diagnostics_path: Path


@dataclass(frozen=True)
class LongMemEvalRunSummary:
    paths: LongMemEvalRunPaths
    questions_started: int
    questions_completed: int
    questions_failed: int
    judged_recall_supported: int | None = None
    judged_recall_total: int | None = None
    judged_recall_by_question_type: dict[str, dict[str, int]] | None = None


@dataclass(frozen=True)
class LongMemEvalDreamResult:
    status: Literal["ok", "incomplete", "skipped"]
    light_cycles: int
    rem_ran: bool
    deep_ran: bool
    final_watermark: int
    consolidation_count: int = 0
    candidate_scoring_time: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LongMemEvalAnswerResult:
    hypothesis: str
    candidate_fallback_used: bool = False
    retrieved_long_term_fact_count: int = 0
    retrieved_long_term_fact_texts: tuple[str, ...] = ()
    retrieved_candidate_note_texts: tuple[str, ...] = ()


class LongMemEvalRecallJudgeVerdict(BaseModel):
    verdict: LongMemEvalRecallJudgeVerdictValue
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class LongMemEvalRecallJudgeInput:
    question: str
    gold_answer: Any
    retrieved_fact_texts: tuple[str, ...]


class LongMemEvalRecallJudge(Protocol):
    async def __call__(
        self,
        judge_input: LongMemEvalRecallJudgeInput,
    ) -> LongMemEvalRecallJudgeVerdict:
        ...


def _safe_path_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return safe or "question"


def _question_path_component(question_id: str) -> str:
    digest = hashlib.sha1(
        question_id.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:8]
    return f"{_safe_path_component(question_id)}-{digest}"


def create_run_paths(output_dir: Path, *, run_id: str | None = None) -> LongMemEvalRunPaths:
    """Create a fresh run directory under an eval output root."""

    resolved_run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_dir / _safe_path_component(resolved_run_id)
    if run_dir.exists() and run_id is None:
        suffix = datetime.now(timezone.utc).strftime("%f")
        run_dir = output_dir / f"{_safe_path_component(resolved_run_id)}-{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return LongMemEvalRunPaths(
        run_dir=run_dir,
        predictions_path=run_dir / "predictions.jsonl",
        diagnostics_path=run_dir / "diagnostics.jsonl",
    )


def question_db_path(run_dir: Path, question_id: str) -> Path:
    """Return the isolated memory DB path for one LongMemEval question."""

    question_dir = run_dir / _question_path_component(question_id)
    try:
        question_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(
            "LongMemEval question directory already exists for "
            f"question_id {question_id!r}: {question_dir}"
        ) from exc
    return question_dir / "memory.db"


def _fallback_question_id(raw: Mapping[str, Any], row_number: int) -> str:
    value = raw.get("question_id")
    question_id = value if isinstance(value, str) and value else "<unknown>"
    return f"row-{row_number}:{question_id}"
