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
from vexic.ports import AgentFactory, EmbedTexts, missing_host_port
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


async def drain_light_then_consolidate(
    db_path: str,
    model_group: str,
    *,
    message_count: int,
    secrets: Mapping[str, str] | None = None,
    max_light_cycles: int | None = None,
    deep_top_n: int = 15,
    extraction_agent_factory: AgentFactory | None = None,
    embed: EmbedTexts | None = None,
    contradiction_agent_factory: AgentFactory | None = None,
) -> LongMemEvalDreamResult:
    """Drain Light to a watermark fixpoint, then run REM and Deep once."""

    cycle_cap = max_light_cycles or ceil(message_count / LIGHT_PHASE_BATCH_SIZE) + 1
    previous_watermark = get_watermark(db_path, agent_id=None)
    current_watermark = previous_watermark
    cycles = 0
    for _ in range(cycle_cap):
        await run_light_phase(
            db_path,
            model_group,
            secrets=secrets,
            extraction_agent_factory=extraction_agent_factory,
            embed=embed,
        )
        cycles += 1
        current_watermark = get_watermark(db_path, agent_id=None)
        if current_watermark == previous_watermark:
            await run_rem_phase(db_path)
            candidate_scoring_time = datetime.now(timezone.utc)
            await run_deep_phase(
                db_path,
                model_group,
                secrets=secrets,
                top_n=deep_top_n,
                now=candidate_scoring_time,
                contradiction_agent_factory=contradiction_agent_factory,
            )
            return LongMemEvalDreamResult(
                status="ok",
                light_cycles=cycles,
                rem_ran=True,
                deep_ran=True,
                final_watermark=current_watermark,
                consolidation_count=1,
                candidate_scoring_time=candidate_scoring_time.isoformat(),
            )
        previous_watermark = current_watermark

    return LongMemEvalDreamResult(
        status="incomplete",
        light_cycles=cycles,
        rem_ran=False,
        deep_ran=False,
        final_watermark=current_watermark,
        consolidation_count=1,
        error="Light phase did not reach a stable watermark before max_light_cycles.",
    )


async def _ingest_then_consolidate_incrementally(
    db_path: str,
    instance: LongMemEvalInstance,
    model_group: str,
    *,
    secrets: Mapping[str, str] | None = None,
    forbidden_secret_values: list[str] | tuple[str, ...] = (),
    max_light_cycles: int | None = None,
    deep_top_n: int = 15,
    dream_session_batch_size: int = 1,
    extraction_agent_factory: AgentFactory | None = None,
    embed: EmbedTexts | None = None,
    contradiction_agent_factory: AgentFactory | None = None,
) -> LongMemEvalDreamResult:
    """Ingest sessions chronologically and run one consolidation cycle per batch."""

    if dream_session_batch_size < 1:
        raise ValueError("dream_session_batch_size must be at least 1.")
    init_db(db_path)
    light_cycles = 0
    rem_ran = False
    deep_ran = False
    final_watermark = get_watermark(db_path, agent_id=None)
    consolidation_count = 0
    candidate_scoring_time: str | None = None
    sorted_sessions = _sorted_sessions(instance)
    for start_index in range(0, len(sorted_sessions), dream_session_batch_size):
        batch = sorted_sessions[start_index : start_index + dream_session_batch_size]
        batch_message_count = 0
        for session in batch:
            _save_instance_session(
                db_path,
                instance,
                session,
                forbidden_secret_values=forbidden_secret_values,
            )
            batch_message_count += len(session.turns)
        dream = await drain_light_then_consolidate(
            db_path,
            model_group,
            message_count=batch_message_count,
            secrets=secrets,
            max_light_cycles=max_light_cycles,
            deep_top_n=deep_top_n,
            extraction_agent_factory=extraction_agent_factory,
            embed=embed,
            contradiction_agent_factory=contradiction_agent_factory,
        )
        consolidation_count += 1
        light_cycles += dream.light_cycles
        rem_ran = rem_ran or dream.rem_ran
        deep_ran = deep_ran or dream.deep_ran
        final_watermark = dream.final_watermark
        dream_scoring_time = getattr(dream, "candidate_scoring_time", None)
        if dream_scoring_time is not None:
            candidate_scoring_time = dream_scoring_time
        if dream.status != "ok":
            return LongMemEvalDreamResult(
                status="incomplete",
                light_cycles=light_cycles,
                rem_ran=rem_ran,
                deep_ran=deep_ran,
                final_watermark=final_watermark,
                consolidation_count=consolidation_count,
                candidate_scoring_time=candidate_scoring_time,
                error=dream.error,
            )

    return LongMemEvalDreamResult(
        status="ok",
        light_cycles=light_cycles,
        rem_ran=rem_ran,
        deep_ran=deep_ran,
        final_watermark=final_watermark,
        consolidation_count=consolidation_count,
        candidate_scoring_time=candidate_scoring_time,
    )


async def drain_light_then_rem(
    db_path: str,
    model_group: str,
    *,
    message_count: int,
    secrets: Mapping[str, str] | None = None,
    max_light_cycles: int | None = None,
    extraction_agent_factory: AgentFactory | None = None,
    embed: EmbedTexts | None = None,
) -> LongMemEvalDreamResult:
    """Drain Light to a watermark fixpoint, then run REM without Deep promotion."""

    cycle_cap = max_light_cycles or ceil(message_count / LIGHT_PHASE_BATCH_SIZE) + 1
    previous_watermark = get_watermark(db_path, agent_id=None)
    current_watermark = previous_watermark
    cycles = 0
    for _ in range(cycle_cap):
        await run_light_phase(
            db_path,
            model_group,
            secrets=secrets,
            extraction_agent_factory=extraction_agent_factory,
            embed=embed,
        )
        cycles += 1
        current_watermark = get_watermark(db_path, agent_id=None)
        if current_watermark == previous_watermark:
            await run_rem_phase(db_path)
            candidate_scoring_time = datetime.now(timezone.utc).isoformat()
            return LongMemEvalDreamResult(
                status="ok",
                light_cycles=cycles,
                rem_ran=True,
                deep_ran=False,
                final_watermark=current_watermark,
                consolidation_count=1,
                candidate_scoring_time=candidate_scoring_time,
            )
        previous_watermark = current_watermark

    return LongMemEvalDreamResult(
        status="incomplete",
        light_cycles=cycles,
        rem_ran=False,
        deep_ran=False,
        final_watermark=current_watermark,
        consolidation_count=1,
        error="Light phase did not reach a stable watermark before max_light_cycles.",
    )


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("LongMemEval dataset file must contain a JSON array.")
    if not all(isinstance(item, dict) for item in raw):
        raise ValueError("LongMemEval dataset entries must be JSON objects.")
    return raw


def _select_instances(
    raw_instances: Sequence[dict[str, Any]],
    *,
    limit: int,
    selection: LongMemEvalSelection,
) -> list[dict[str, Any]]:
    if selection == "first":
        return list(raw_instances[:limit])
    if selection != "stratified":
        raise ValueError(f"Unsupported LongMemEval selection: {selection}")

    groups: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_instances:
        question_type = raw.get("question_type")
        group_name = question_type if isinstance(question_type, str) else "<unknown>"
        groups.setdefault(group_name, []).append(raw)

    selected: list[dict[str, Any]] = []
    group_names = list(groups)
    index = 0
    while len(selected) < limit:
        made_progress = False
        for group_name in group_names:
            group = groups[group_name]
            if index < len(group):
                selected.append(group[index])
                made_progress = True
                if len(selected) == limit:
                    break
        if not made_progress:
            break
        index += 1
    return selected


def _filter_instances_by_question_id(
    raw_instances: Sequence[dict[str, Any]],
    question_ids: Sequence[str],
) -> list[dict[str, Any]]:
    if not question_ids:
        return list(raw_instances)
    allowed_ids = set(question_ids)
    return [
        raw
        for raw in raw_instances
        if isinstance(raw.get("question_id"), str)
        and raw["question_id"] in allowed_ids
    ]


def _completed_question_ids_from_run(run_dir: Path) -> set[str]:
    diagnostics_path = run_dir / "diagnostics.jsonl"
    if not diagnostics_path.exists():
        raise ValueError(f"LongMemEval resume run has no diagnostics file: {diagnostics_path}")

    completed_ids: set[str] = set()
    with diagnostics_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "LongMemEval resume diagnostics contains invalid JSON "
                    f"on line {line_number}: {diagnostics_path}"
                ) from exc
            question_id = payload.get("question_id")
            status = payload.get("status")
            if isinstance(question_id, str) and status == "ok":
                completed_ids.add(question_id)
    return completed_ids


def _filter_completed_instances(
    raw_instances: Sequence[dict[str, Any]],
    completed_question_ids: set[str],
) -> list[dict[str, Any]]:
    if not completed_question_ids:
        return list(raw_instances)
    return [
        raw
        for raw in raw_instances
        if not (
            isinstance(raw.get("question_id"), str)
            and raw["question_id"] in completed_question_ids
        )
    ]


def _artifact_line(payload: dict[str, Any], forbidden_secret_values: Sequence[str]) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert_no_forbidden_secret_values(forbidden_secret_values, text)
    return text


def _append_jsonl(path: Path, payload: dict[str, Any], forbidden_secret_values: Sequence[str]) -> None:
    line = _artifact_line(payload, forbidden_secret_values)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def _judge_model_id_from_agent(agent: Any) -> str | None:
    model = getattr(agent, "model", None)
    model_name = getattr(model, "model_name", None)
    return model_name if isinstance(model_name, str) and model_name else None


def _render_recall_judge_input(judge_input: LongMemEvalRecallJudgeInput) -> str:
    facts = "\n".join(
        fact_text
        if fact_text.startswith("[unverified note]")
        else f"[fact {index}] {fact_text}"
        for index, fact_text in enumerate(judge_input.retrieved_fact_texts, start=1)
    )
    if not facts:
        facts = "None"
    return (
        "Question:\n"
        f"{judge_input.question}\n\n"
        "Gold answer:\n"
        f"{json.dumps(judge_input.gold_answer, ensure_ascii=False, default=str)}\n\n"
        "Retrieved facts:\n"
        f"{facts}"
    )


async def score_longmemeval_recall(
    judge_input: LongMemEvalRecallJudgeInput,
    *,
    judge_model_group: str,
    agent: Any = None,
    judge_agent_factory: AgentFactory | None = None,
    secrets: Mapping[str, str] | None = None,
    forbidden_secret_values: Sequence[str] = (),
) -> LongMemEvalRecallJudgeVerdict:
    rendered_input = _render_recall_judge_input(judge_input)
    assert_no_forbidden_secret_values(
        forbidden_secret_values,
        LONGMEMEVAL_RECALL_JUDGE_PROMPT,
        rendered_input,
    )
    resolved_agent = agent
    if resolved_agent is None:
        if judge_agent_factory is None:
            raise missing_host_port(
                "judge_agent_factory",
                "Supply a recall-judge agent factory (see "
                "adapters/openrouter_live_adapter.py) or pass judge_scorer.",
            )
        resolved_agent = judge_agent_factory(judge_model_group, secrets=secrets)
    result = await resolved_agent.run(rendered_input)
    assert_no_forbidden_secret_values(
        forbidden_secret_values,
        result.output.model_dump_json(),
    )
    return result.output


def _retrieval_debug_hypothesis(db_path: str, instance: LongMemEvalInstance) -> tuple[str, bool]:
    rendered_hits: list[str] = []
    query_tokens = re.findall(r"[\w]+", instance.question, flags=re.UNICODE)
    compact_query = " ".join(
        token for token in query_tokens if token.lower() not in _DEBUG_QUERY_STOPWORDS
    )
    queries = [instance.question]
    if compact_query and compact_query != instance.question:
        queries.append(compact_query)
    for session in instance.sessions:
        hits = []
        for query in queries:
            hits = search_messages(
                db_path,
                query,
                session_id=f"longmemeval:{instance.question_id}:{session.session_id}",
            )
            if hits:
                break
        rendered_hits.extend(_format_transcript_hit(hit) for hit in hits)
    if not rendered_hits:
        return "No relevant memories found.", False
    return "\n---\n".join(rendered_hits[:5]), False


def _format_transcript_hit(hit: TranscriptHit) -> str:
    if hit.timestamp is None:
        return f"[message {hit.message_id}] {hit.body}"
    return f"[message {hit.message_id} @ {hit.timestamp}] {hit.body}"


def _format_long_term_fact(fact: Any) -> str:
    sources = ", ".join(str(message_id) for message_id in fact.source_message_ids)
    return (
        f"[fact {fact.fact_id}] {fact.fact_text}\n"
        f"(category: {fact.category}, confidence: {fact.confidence:.2f}, "
        f"source messages: {sources})"
    )


@dataclass(frozen=True)
class _AnswerDiagnostics:
    answer_matchable: bool
    answer_match_skipped_reason: str | None
    answer_found_in_tier1: bool
    answer_extracted_to_tier2: bool
    answer_candidate_id: int | None
    answer_candidate_rank: int | None
    answer_promoted_to_tier3: bool
    answer_retrieved_from_tier3: bool


@dataclass(frozen=True)
class _DiagnosticCandidate:
    candidate_id: int
    fact_text: str
    importance: int
    hit_count: int
    last_seen_at: datetime
    rem_boost: float
    promoted: bool
    promoted_fact_id: int | None


def _answer_variants(answer: Any) -> tuple[str, ...]:
    if isinstance(answer, str):
        return (answer,)
    if isinstance(answer, Sequence) and not isinstance(answer, (bytes, bytearray)):
        return tuple(item for item in answer if isinstance(item, str))
    return ()


def _normalized_tokens(value: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in re.findall(r"[A-Za-z0-9]+", value))


def _matchable_answer_tokens(answer: Any) -> tuple[tuple[str, ...], ...]:
    variants: list[tuple[str, ...]] = []
    for value in _answer_variants(answer):
        tokens = _normalized_tokens(value)
        if not tokens:
            continue
        compact = "".join(tokens)
        if compact in {"yes", "no", "true", "false", "none", "unknown", "na"}:
            continue
        if len(compact) < 4:
            continue
        variants.append(tokens)
    return tuple(dict.fromkeys(variants))


def _contains_answer_tokens(text: str, answer_tokens: Sequence[tuple[str, ...]]) -> bool:
    tokens = _normalized_tokens(text)
    if not tokens:
        return False
    for variant in answer_tokens:
        if len(variant) > len(tokens):
            continue
        for index in range(0, len(tokens) - len(variant) + 1):
            if tuple(tokens[index : index + len(variant)]) == tuple(variant):
                return True
    return False


def _messages_fts_bodies(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            rows = conn.execute("SELECT body FROM messages_fts").fetchall()
        except sqlite3.OperationalError:
            return []
    return [str(row[0]) for row in rows]


def _load_diagnostic_candidates(db_path: Path) -> list[_DiagnosticCandidate]:
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            rows = conn.execute(
                """
                SELECT id, fact_text, importance, hit_count,
                       COALESCE(last_seen_at, created_at) AS last_seen_at,
                       created_at, rem_boost, promoted, promoted_fact_id
                FROM memory_candidates
                WHERE retired = 0
                    AND stale = 0
                    AND needs_review = 0
                ORDER BY id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [
        _DiagnosticCandidate(
            candidate_id=int(row[0]),
            fact_text=str(row[1]),
            importance=int(row[2]),
            hit_count=int(row[3]),
            last_seen_at=_safe_diagnostic_timestamp(row[4], row[5]),
            rem_boost=float(row[6]),
            promoted=bool(row[7]),
            promoted_fact_id=None if row[8] is None else int(row[8]),
        )
        for row in rows
    ]


def _safe_diagnostic_timestamp(*values: object) -> datetime:
    for value in values:
        if value is None:
            continue
        try:
            return _timestamp_datetime(str(value))
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _rank_diagnostic_candidates(
    candidates: Sequence[_DiagnosticCandidate],
    *,
    scoring_time: datetime,
) -> dict[int, int]:
    if not candidates:
        return {}
    max_hit_count = max(candidate.hit_count for candidate in candidates)
    scored = [
        (
            compute_score(
                importance=candidate.importance,
                hit_count=candidate.hit_count,
                days_since_last_seen=(
                    scoring_time - candidate.last_seen_at
                ).total_seconds()
                / 86400,
                max_hit_count=max_hit_count,
                rem_boost=candidate.rem_boost,
            ),
            candidate.candidate_id,
        )
        for candidate in candidates
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return {candidate_id: index for index, (_, candidate_id) in enumerate(scored, start=1)}


def _rank_bucket(rank: int | None) -> str | None:
    if rank is None:
        return None
    if rank <= 3:
        return "top_3"
    if rank <= 10:
        return "top_10"
    if rank <= 20:
        return "top_20"
    if rank <= 50:
        return "top_50"
    if rank <= 100:
        return "top_100"
    return "over_100"


def _promoted_answer_fact_exists(
    db_path: Path,
    *,
    candidate_id: int | None,
    answer_tokens: Sequence[tuple[str, ...]],
) -> bool:
    if not db_path.exists():
        return False
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            rows = conn.execute(
                """
                SELECT fact_text, promoted_from_candidate_id
                FROM long_term_memory
                WHERE retired = 0
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return False
    for fact_text, promoted_from_candidate_id in rows:
        if candidate_id is not None and int(promoted_from_candidate_id) == candidate_id:
            return True
        if _contains_answer_tokens(str(fact_text), answer_tokens):
            return True
    return False


def _answer_diagnostics(
    *,
    db_path: Path | None,
    instance: LongMemEvalInstance | None,
    retrieved_long_term_fact_texts: Sequence[str],
    candidate_scoring_time: str | None,
) -> _AnswerDiagnostics:
    answer_tokens = _matchable_answer_tokens(None if instance is None else instance.answer)
    if not answer_tokens:
        return _AnswerDiagnostics(
            answer_matchable=False,
            answer_match_skipped_reason="empty-or-too-short-answer",
            answer_found_in_tier1=False,
            answer_extracted_to_tier2=False,
            answer_candidate_id=None,
            answer_candidate_rank=None,
            answer_promoted_to_tier3=False,
            answer_retrieved_from_tier3=False,
        )
    if db_path is None:
        return _AnswerDiagnostics(
            answer_matchable=True,
            answer_match_skipped_reason=None,
            answer_found_in_tier1=False,
            answer_extracted_to_tier2=False,
            answer_candidate_id=None,
            answer_candidate_rank=None,
            answer_promoted_to_tier3=False,
            answer_retrieved_from_tier3=False,
        )

    candidates = _load_diagnostic_candidates(db_path)
    ranks = _rank_diagnostic_candidates(
        candidates,
        scoring_time=(
            datetime.fromisoformat(candidate_scoring_time)
            if candidate_scoring_time is not None
            else datetime.now(timezone.utc)
        ),
    )
    answer_candidate = next(
        (
            candidate
            for candidate in candidates
            if _contains_answer_tokens(candidate.fact_text, answer_tokens)
        ),
        None,
    )
    answer_candidate_id = (
        None if answer_candidate is None else answer_candidate.candidate_id
    )
    answer_retrieved = any(
        _contains_answer_tokens(fact_text, answer_tokens)
        for fact_text in retrieved_long_term_fact_texts
    )
    return _AnswerDiagnostics(
        answer_matchable=True,
        answer_match_skipped_reason=None,
        answer_found_in_tier1=any(
            _contains_answer_tokens(body, answer_tokens)
            for body in _messages_fts_bodies(db_path)
        ),
        answer_extracted_to_tier2=answer_candidate is not None,
        answer_candidate_id=answer_candidate_id,
        answer_candidate_rank=None
        if answer_candidate_id is None
        else ranks.get(answer_candidate_id),
        answer_promoted_to_tier3=(
            False
            if answer_candidate is None
            else answer_candidate.promoted
            or answer_candidate.promoted_fact_id is not None
            or _promoted_answer_fact_exists(
                db_path,
                candidate_id=answer_candidate_id,
                answer_tokens=answer_tokens,
            )
        ),
        answer_retrieved_from_tier3=answer_retrieved,
    )


def _diagnostics_error_result(instance: LongMemEvalInstance | None) -> _AnswerDiagnostics:
    return _AnswerDiagnostics(
        answer_matchable=bool(
            _matchable_answer_tokens(None if instance is None else instance.answer)
        ),
        answer_match_skipped_reason="diagnostics-error",
        answer_found_in_tier1=False,
        answer_extracted_to_tier2=False,
        answer_candidate_id=None,
        answer_candidate_rank=None,
        answer_promoted_to_tier3=False,
        answer_retrieved_from_tier3=False,
    )


async def _tier3_debug_hypothesis(
    db_path: str,
    instance: LongMemEvalInstance,
    *,
    model_group: str,
    secrets: Mapping[str, str] | None,
    include_candidate_fallback: bool = False,
    embed: EmbedTexts | None = None,
) -> LongMemEvalAnswerResult:
    session_id = f"longmemeval:{instance.question_id}:answer"
    facts = await retrieve_long_term_facts(
        db_path,
        instance.question,
        session_id=session_id,
        model_group=model_group,
        secrets=secrets,
        **({} if embed is None else {"embed": embed}),
    )
    if facts:
        return LongMemEvalAnswerResult(
            "\n---\n".join(_format_long_term_fact(fact) for fact in facts),
            retrieved_long_term_fact_count=len(facts),
            retrieved_long_term_fact_texts=tuple(fact.fact_text for fact in facts),
        )
    if not include_candidate_fallback:
        return LongMemEvalAnswerResult("No long-term memories found.")

    notes = await retrieve_candidate_fallback(
        db_path,
        instance.question,
        session_id=session_id,
        secrets=secrets,
        **({} if embed is None else {"embed": embed}),
    )
    if not notes:
        return LongMemEvalAnswerResult("No long-term memories found.")

    rendered_notes = tuple(format_candidate_note(note) for note in notes)
    return LongMemEvalAnswerResult(
        f"{UNVERIFIED_NOTES_PREAMBLE}\n\n" + "\n---\n".join(rendered_notes),
        candidate_fallback_used=True,
        retrieved_candidate_note_texts=rendered_notes,
    )


def _count_rows_if_present(db_path: Path | None, table_name: str) -> int:
    if db_path is None or not db_path.exists():
        return 0
    if table_name not in {"dream_runs", "memory_candidates", "long_term_memory"}:
        raise ValueError(f"Unsupported diagnostic table: {table_name}")
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
        except sqlite3.OperationalError:
            return 0
    return int(row[0]) if row is not None else 0


async def run_longmemeval_subset(
    dataset_path: Path,
    *,
    split: LongMemEvalSplit,
    output_dir: Path,
    limit: int,
    model_group: str,
    max_sessions: int | None = None,
    dream_session_batch_size: int = 1,
    answer_mode: AnswerMode = "retrieval-debug",
    judge_model_group: str = "claude",
    judge_scorer: LongMemEvalRecallJudge | None = None,
    judge_agent_factory: AgentFactory | None = None,
    secrets: Mapping[str, str] | None = None,
    forbidden_secret_values: Sequence[str] = (),
    deep_top_n: int = 15,
    max_light_cycles: int | None = None,
    skip_dream: bool = False,
    selection: LongMemEvalSelection = "first",
    question_ids: Sequence[str] = (),
    resume_from_run: Path | None = None,
    extraction_agent_factory: AgentFactory | None = None,
    contradiction_agent_factory: AgentFactory | None = None,
    embed: EmbedTexts | None = None,
) -> LongMemEvalRunSummary:
    """Run a capped LongMemEval subset and write prediction/diagnostic JSONL."""

    if limit < 1:
        raise ValueError("limit must be at least 1.")
    if dream_session_batch_size < 1:
        raise ValueError("dream_session_batch_size must be at least 1.")
    if skip_dream and answer_mode != "retrieval-debug":
        raise ValueError("--skip-dream is only supported for retrieval-debug answer mode.")
    if answer_mode not in (
        "retrieval-debug",
        "tier2-debug",
        "tier3-debug",
        "judged-recall",
    ):
        raise ValueError(f"Unsupported LongMemEval answer mode: {answer_mode}")

    loaded_secret_values = tuple((secrets or {}).values())
    guarded_secret_values = tuple(
        dict.fromkeys((*forbidden_secret_values, *loaded_secret_values))
    )
    run_paths = create_run_paths(output_dir)
    raw_instances = _select_instances(
        _load_dataset(dataset_path),
        limit=limit,
        selection=selection,
    )
    raw_instances = _filter_instances_by_question_id(raw_instances, question_ids)
    if resume_from_run is not None:
        raw_instances = _filter_completed_instances(
            raw_instances,
            _completed_question_ids_from_run(resume_from_run),
        )
    completed = 0
    failed = 0
    judged_recall_supported = 0
    judged_recall_total = 0
    judged_recall_by_question_type: dict[str, dict[str, int]] = {}
    recall_judge_agent: Any = None
    for row_number, raw in enumerate(raw_instances, start=1):
        started = time.perf_counter()
        artifact_question_id = _fallback_question_id(raw, row_number)
        status = "ok"
        error: str | None = None
        dream = LongMemEvalDreamResult(
            status="incomplete",
            light_cycles=0,
            rem_ran=False,
            deep_ran=False,
            final_watermark=0,
            error="not started",
        )
        hypothesis = ""
        candidate_fallback_used = False
        retrieved_long_term_fact_count = 0
        retrieved_long_term_fact_texts: tuple[str, ...] = ()
        retrieved_candidate_note_texts: tuple[str, ...] = ()
        retrieved_candidate_note_count = 0
        judge_result: LongMemEvalRecallJudgeVerdict | None = None
        judge_model_id: str | None = None
        judge_error: str | None = None
        db_path: Path | None = None
        instance: LongMemEvalInstance | None = None
        question_type: str | None = None
        dream_shape: LongMemEvalDreamShape = "single-shot"
        session_count = 0
        message_count = 0
        try:
            instance = parse_longmemeval_instance(raw, max_sessions=max_sessions)
            artifact_question_id = instance.question_id
            question_type = instance.question_type
            session_count = len(instance.sessions)
            message_count = sum(len(session.turns) for session in instance.sessions)
            db_path = question_db_path(run_paths.run_dir, instance.question_id)
            if answer_mode in ("tier3-debug", "judged-recall") and not skip_dream:
                dream_shape = (
                    "incremental-per-session"
                    if dream_session_batch_size == 1
                    else "incremental-batched-sessions"
                )
                dream = await _ingest_then_consolidate_incrementally(
                    str(db_path),
                    instance,
                    model_group,
                    secrets=secrets,
                    forbidden_secret_values=guarded_secret_values,
                    max_light_cycles=max_light_cycles,
                    deep_top_n=deep_top_n,
                    dream_session_batch_size=dream_session_batch_size,
                    extraction_agent_factory=extraction_agent_factory,
                    embed=embed,
                    contradiction_agent_factory=contradiction_agent_factory,
                )
            else:
                ingest_instance(
                    str(db_path),
                    instance,
                    forbidden_secret_values=guarded_secret_values,
                )
            if skip_dream:
                dream_shape = "skipped"
                dream = LongMemEvalDreamResult(
                    status="skipped",
                    light_cycles=0,
                    rem_ran=False,
                    deep_ran=False,
                    final_watermark=get_watermark(str(db_path), agent_id=None),
                    consolidation_count=0,
                    error=None,
                )
            elif answer_mode == "tier2-debug":
                dream_shape = "single-shot-light-rem"
                dream = await drain_light_then_rem(
                    str(db_path),
                    model_group,
                    message_count=message_count,
                    max_light_cycles=max_light_cycles,
                    secrets=secrets,
                    extraction_agent_factory=extraction_agent_factory,
                    embed=embed,
                )
            elif dream_shape == "single-shot":
                dream = await drain_light_then_consolidate(
                    str(db_path),
                    model_group,
                    message_count=message_count,
                    deep_top_n=deep_top_n,
                    max_light_cycles=max_light_cycles,
                    secrets=secrets,
                    extraction_agent_factory=extraction_agent_factory,
                    embed=embed,
                    contradiction_agent_factory=contradiction_agent_factory,
                )
            if dream.status == "incomplete":
                status = "incomplete"
                error = dream.error
            if answer_mode == "retrieval-debug":
                hypothesis, candidate_fallback_used = _retrieval_debug_hypothesis(
                    str(db_path),
                    instance,
                )
            elif answer_mode == "tier2-debug":
                if dream.status == "ok":
                    hypothesis = (
                        "Tier 2 candidate diagnostics written to diagnostics.jsonl."
                    )
                elif dream.status == "incomplete":
                    hypothesis = f"Tier 2 diagnostics incomplete: {dream.error}"
            elif answer_mode in ("tier3-debug", "judged-recall"):
                if dream.status != "ok":
                    hypothesis = (
                        "Tier 3 diagnostics incomplete: "
                        f"{dream.error or f'dream status {dream.status}'}"
                    )
                else:
                    answer = await _tier3_debug_hypothesis(
                        str(db_path),
                        instance,
                        model_group=model_group,
                        secrets=secrets,
                        include_candidate_fallback=answer_mode == "judged-recall",
                        embed=embed,
                    )
                    hypothesis = answer.hypothesis
                    candidate_fallback_used = answer.candidate_fallback_used
                    retrieved_long_term_fact_count = (
                        answer.retrieved_long_term_fact_count
                    )
                    retrieved_long_term_fact_texts = (
                        answer.retrieved_long_term_fact_texts
                    )
                    retrieved_candidate_note_texts = answer.retrieved_candidate_note_texts
                    retrieved_candidate_note_count = len(retrieved_candidate_note_texts)
                    if answer_mode == "judged-recall":
                        retrieved_judge_texts = (
                            retrieved_long_term_fact_texts
                            or retrieved_candidate_note_texts
                        )
                        assert_no_forbidden_secret_values(
                            guarded_secret_values,
                            *retrieved_judge_texts,
                        )
                        judge_input = LongMemEvalRecallJudgeInput(
                            question=instance.question,
                            gold_answer=instance.answer,
                            retrieved_fact_texts=retrieved_judge_texts,
                        )
                        try:
                            if judge_scorer is None:
                                if recall_judge_agent is None:
                                    if judge_agent_factory is None:
                                        raise missing_host_port(
                                            "judge_agent_factory",
                                            "Supply a recall-judge agent factory "
                                            "(see adapters/openrouter_live_adapter.py) "
                                            "or pass judge_scorer.",
                                        )
                                    recall_judge_agent = judge_agent_factory(
                                        judge_model_group,
                                        secrets=secrets,
                                    )
                                judge_model_id = _judge_model_id_from_agent(
                                    recall_judge_agent
                                )
                                judge_result = await score_longmemeval_recall(
                                    judge_input,
                                    judge_model_group=judge_model_group,
                                    agent=recall_judge_agent,
                                    secrets=secrets,
                                    forbidden_secret_values=guarded_secret_values,
                                )
                            else:
                                rendered_input = _render_recall_judge_input(judge_input)
                                assert_no_forbidden_secret_values(
                                    guarded_secret_values,
                                    LONGMEMEVAL_RECALL_JUDGE_PROMPT,
                                    rendered_input,
                                )
                                judge_result = await judge_scorer(judge_input)
                                assert_no_forbidden_secret_values(
                                    guarded_secret_values,
                                    judge_result.model_dump_json(),
                                )
                        except Exception as exc:
                            judge_error = str(exc)
                            raise
            else:
                raise ValueError(f"Unsupported LongMemEval answer mode: {answer_mode}")
        except Exception as exc:
            if "forbidden secret" in str(exc):
                raise
            failed += 1
            status = "error"
            error = str(exc)
            hypothesis = ""
        else:
            if status == "ok":
                completed += 1

        try:
            answer_diagnostics = _answer_diagnostics(
                db_path=db_path,
                instance=instance,
                retrieved_long_term_fact_texts=retrieved_long_term_fact_texts,
                candidate_scoring_time=getattr(dream, "candidate_scoring_time", None),
            )
        except Exception as exc:
            if "forbidden secret" in str(exc):
                raise
            answer_diagnostics = _diagnostics_error_result(instance)

        if answer_mode == "judged-recall" and (
            judge_result is None or status != "ok"
        ):
            judged_recall_pass = False
        elif judge_result is None:
            judged_recall_pass = None
        else:
            judged_recall_pass = judge_result.verdict == "supported"
        if answer_mode == "judged-recall":
            judged_recall_total += 1
            if judged_recall_pass:
                judged_recall_supported += 1
            bucket_name = question_type or "<unknown>"
            bucket = judged_recall_by_question_type.setdefault(
                bucket_name,
                {"supported": 0, "total": 0},
            )
            bucket["total"] += 1
            if judged_recall_pass:
                bucket["supported"] += 1

        _append_jsonl(
            run_paths.predictions_path,
            {
                "question_id": artifact_question_id,
                "hypothesis": hypothesis,
            },
            guarded_secret_values,
        )
        _append_jsonl(
            run_paths.diagnostics_path,
            {
                "question_id": artifact_question_id,
                "question_type": question_type,
                "split": split,
                "status": status,
                "error": error,
                "answer_mode": answer_mode,
                "session_count": session_count,
                "message_count": message_count,
                "dream_skipped": dream.status == "skipped",
                "dream_skip_reason": "retrieval-debug-speedup"
                if dream.status == "skipped"
                else None,
                "dream_shape": dream_shape,
                "dream_session_batch_size": dream_session_batch_size,
                "dream_consolidation_count": getattr(
                    dream,
                    "consolidation_count",
                    0,
                ),
                "production_fidelity": dream_shape == "incremental-per-session",
                "light_cycles": dream.light_cycles,
                "rem_ran": dream.rem_ran,
                "deep_ran": dream.deep_ran,
                "deep_top_n": deep_top_n,
                "final_watermark": dream.final_watermark,
                "candidate_scoring_time": getattr(
                    dream,
                    "candidate_scoring_time",
                    None,
                ),
                "candidate_fallback_used": candidate_fallback_used,
                "retrieved_long_term_fact_count": retrieved_long_term_fact_count,
                "retrieved_candidate_note_count": retrieved_candidate_note_count,
                "judge_verdict": None if judge_result is None else judge_result.verdict,
                "judge_reason": None if judge_result is None else judge_result.reason,
                "judge_confidence": (
                    None if judge_result is None else judge_result.confidence
                ),
                "judge_error": judge_error,
                "judge_model_group": (
                    judge_model_group if answer_mode == "judged-recall" else None
                ),
                "judge_model_id": judge_model_id,
                "judge_prompt_version": (
                    LONGMEMEVAL_RECALL_JUDGE_PROMPT_VERSION
                    if answer_mode == "judged-recall"
                    else None
                ),
                "judged_recall_pass": judged_recall_pass,
                "answer_matchable": answer_diagnostics.answer_matchable,
                "answer_match_skipped_reason": (
                    answer_diagnostics.answer_match_skipped_reason
                ),
                "answer_found_in_tier1": answer_diagnostics.answer_found_in_tier1,
                "answer_extracted_to_tier2": (
                    answer_diagnostics.answer_extracted_to_tier2
                ),
                "answer_candidate_id": answer_diagnostics.answer_candidate_id,
                "answer_candidate_rank": answer_diagnostics.answer_candidate_rank,
                "answer_candidate_rank_bucket": _rank_bucket(
                    answer_diagnostics.answer_candidate_rank
                ),
                "answer_promoted_to_tier3": (
                    answer_diagnostics.answer_promoted_to_tier3
                ),
                "answer_retrieved_from_tier3": (
                    answer_diagnostics.answer_retrieved_from_tier3
                ),
                "memory_candidate_count": _count_rows_if_present(
                    db_path,
                    "memory_candidates",
                ),
                "long_term_fact_count": _count_rows_if_present(
                    db_path,
                    "long_term_memory",
                ),
                "dream_run_count": _count_rows_if_present(db_path, "dream_runs"),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            guarded_secret_values,
        )

    return LongMemEvalRunSummary(
        paths=run_paths,
        questions_started=len(raw_instances),
        questions_completed=completed,
        questions_failed=failed,
        judged_recall_supported=(
            judged_recall_supported if answer_mode == "judged-recall" else None
        ),
        judged_recall_total=(
            judged_recall_total if answer_mode == "judged-recall" else None
        ),
        judged_recall_by_question_type=(
            judged_recall_by_question_type if answer_mode == "judged-recall" else None
        ),
    )
