"""Deterministic retrieval eval runner for Vexic.

This wires the repo's LongMemEval-style eval datasets into a scored, repeatable
harness without touching any model-backed host port. For each dataset row it:

1. stands up a fresh in-memory-style local service over a temp SQLite database,
2. ingests the row transcript through ``ingest_source_transcript``,
3. retrieves with ``search_transcript`` for the row question, and
4. scores whether the expected fact (and its key tokens) appears in the
   retrieved transcript hits.

Scoring is deterministic and uses only the transcript-search retrieval path,
which the local service supports with no host ports. The model-backed Tier 2/3
path (``search_long_term`` after dream phases) needs host adapters and is
deliberately out of scope here; this runner never wires a provider.

Retrieval note: the local ``search_transcript`` contract uses all-tokens-must-
match FTS keyword semantics, so a full natural-language question (with
stopwords and inflected verbs) rarely matches a short transcript. This runner
therefore issues one query per content keyword (a deterministic stopword
strip) through the real ``search_transcript`` and unions the hits -- a
recall-oriented retrieval set built from the single-token API. It records the
raw-question hit count alongside the unioned-keyword hit count so the metric
stays honest about what was searched.

Exit code is always 0 (this is an eval, not a gate). The final stdout line is a
machine-readable JSON summary.

Usage (needs the editable install on path, since this imports ``vexic``):
    uv run --with-editable . python -m vexic.run_evals --dataset tests/fixtures/longmemeval_s_smoke.jsonl
    uv run --with-editable . python -m vexic.run_evals --dataset tests/fixtures/longmemeval_s_subset_10.jsonl --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from vexic.contract import (
    IngestSourceTranscriptRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    SearchTranscriptRequest,
    SourceTranscriptMessage,
    TrustBoundary,
)
from vexic.service import LocalMemoryService
from vexic.storage import single_message_adapter


# --- Dataset models -------------------------------------------------------


class EvalTurn(BaseModel):
    """A single transcript turn in role/content object form."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = "user"
    content: str

    @field_validator("content")
    @classmethod
    def _content_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


# A transcript turn may be a bare string (treated as a user turn) or an object.
TranscriptTurn = str | EvalTurn


class EvalRow(BaseModel):
    """One LongMemEval-style row: transcript + question + expected fact."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    row_id: str = Field(alias="id")
    transcript: list[TranscriptTurn] = Field(min_length=1)
    question: str
    expected_fact: str

    @field_validator("row_id", "question", "expected_fact")
    @classmethod
    def _text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


EvalRow.model_rebuild()


# --- Dataset loading ------------------------------------------------------


def load_dataset(path: Path, *, limit: int | None = None) -> list[EvalRow]:
    """Parse a .jsonl dataset into validated EvalRow models."""
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    rows: list[EvalRow] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(EvalRow.model_validate_json(line))
            except ValidationError as exc:
                first = exc.errors()[0]
                location = ".".join(str(part) for part in first["loc"])
                detail = f"{location}: {first['msg']}" if location else first["msg"]
                raise ValueError(
                    f"dataset line {line_number} is invalid: {detail}."
                ) from exc
            if limit is not None and len(rows) >= limit:
                break
    if not rows:
        raise ValueError("dataset must contain at least one row.")
    return rows


# --- Transcript encoding --------------------------------------------------


def _turn_role_and_content(turn: TranscriptTurn) -> tuple[str, str]:
    if isinstance(turn, str):
        return "user", turn
    return turn.role, turn.content


def _message_json(turn: TranscriptTurn) -> str:
    """Encode a turn as a pydantic-ai message, matching the storage adapter."""
    role, content = _turn_role_and_content(turn)
    if role == "assistant":
        message: ModelRequest | ModelResponse = ModelResponse(
            parts=[TextPart(content=content)]
        )
    else:
        message = ModelRequest(parts=[UserPromptPart(content=content)])
    return single_message_adapter.dump_json(message).decode()


# --- Scope construction ---------------------------------------------------


def _scope(tenant_id: str) -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant_id,
        session_id="eval",
        principal=Principal(
            principal_id="vexic-eval-runner",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
    )


# --- Scoring --------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Common English stopwords plus question framing words. Stripping these turns a
# natural-language question into a keyword query that the all-tokens FTS path
# can match. This is a deterministic transform, not a relevance heuristic.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "by", "did", "do",
        "does", "for", "from", "had", "has", "have", "how", "i", "in", "is",
        "it", "its", "me", "my", "of", "on", "or", "que", "she", "so", "tell",
        "that", "the", "their", "them", "then", "there", "these", "they",
        "this", "to", "was", "we", "were", "what", "when", "where", "which",
        "who", "whom", "why", "will", "with", "would", "you", "your",
    }
)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def question_keywords(question: str) -> list[str]:
    """Reduce a question to its content keywords for keyword retrieval.

    Duplicates are dropped while preserving order. If a question is all
    stopwords, fall back to its raw tokens so retrieval still has something to
    search on.
    """
    keywords: list[str] = []
    for tok in _tokens(question):
        if tok not in _STOPWORDS and tok not in keywords:
            keywords.append(tok)
    if keywords:
        return keywords
    fallback: list[str] = []
    for tok in _tokens(question):
        if tok not in fallback:
            fallback.append(tok)
    return fallback


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def score_row(expected_fact: str, hit_bodies: list[str]) -> dict[str, object]:
    """Deterministically score retrieval for one row.

    Two signals over the union of retrieved transcript bodies:

    - ``exact_substring``: the normalized expected fact appears verbatim in a
      retrieved body. This is the strict pass condition.
    - ``token_recall``: fraction of distinct expected-fact tokens present in
      the retrieved bodies. A soft signal for partial retrieval.

    A row "passes" when the exact normalized expected fact is recovered.
    """
    expected_tokens = set(_tokens(expected_fact))
    haystack_normalized = " || ".join(_normalized(body) for body in hit_bodies)
    haystack_tokens = set(_tokens(haystack_normalized))

    exact_hit = _normalized(expected_fact) in haystack_normalized
    if expected_tokens:
        recovered = expected_tokens & haystack_tokens
        token_recall = len(recovered) / len(expected_tokens)
    else:
        token_recall = 1.0

    return {
        "exact_substring": exact_hit,
        "token_recall": round(token_recall, 4),
        "expected_token_count": len(expected_tokens),
        "passed": exact_hit,
    }


# --- Per-row evaluation ---------------------------------------------------


async def evaluate_row(
    row: EvalRow,
    *,
    limit: int,
) -> dict[str, object]:
    """Ingest one row's transcript and score transcript retrieval for it."""
    tenant_id = f"vexic-eval-{uuid.uuid4().hex}"
    scope = _scope(tenant_id)

    with tempfile.TemporaryDirectory(
        prefix="vexic-eval-", ignore_cleanup_errors=True
    ) as temp_dir:
        db_path = Path(temp_dir) / "memory.db"
        service = LocalMemoryService(db_path=str(db_path), tenant_id=tenant_id)
        service.init_schema()

        source_session_id = f"eval-{row.row_id}"
        ingest_messages = [
            SourceTranscriptMessage(
                source_host="vexic-eval",
                source_session_id=source_session_id,
                source_message_id=f"{row.row_id}-{index}",
                message_json=_message_json(turn),
            )
            for index, turn in enumerate(row.transcript)
        ]
        ingest_result = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=scope,
                redaction=RedactionContext(forbidden_values=()),
                messages=ingest_messages,
            )
        )
        inserted = sum(1 for item in ingest_result.items if item.status == "inserted")

        # Honest diagnostic: how the raw natural-language question fares under
        # the all-tokens FTS contract (usually 0 hits on short transcripts).
        raw_result = await service.search_transcript(
            SearchTranscriptRequest(
                scope=scope,
                query=row.question,
                limit=limit,
            )
        )

        # Scored path: union the hits from one query per content keyword,
        # all through the same real service. Dedupe by message_id, keeping a
        # stable order so scoring is deterministic.
        keywords = question_keywords(row.question)
        seen_ids: set[int] = set()
        hit_bodies: list[str] = []
        for keyword in keywords:
            keyword_result = await service.search_transcript(
                SearchTranscriptRequest(
                    scope=scope,
                    query=keyword,
                    limit=limit,
                )
            )
            for hit in keyword_result.hits:
                if hit.message_id not in seen_ids:
                    seen_ids.add(hit.message_id)
                    hit_bodies.append(hit.body)
        score = score_row(row.expected_fact, hit_bodies)

    return {
        "id": row.row_id,
        "question": row.question,
        "keywords": keywords,
        "expected_fact": row.expected_fact,
        "messages_ingested": inserted,
        "raw_question_hits": len(raw_result.hits),
        "hits": len(hit_bodies),
        "score": score,
    }


async def run(rows: list[EvalRow], *, limit: int) -> dict[str, object]:
    results = [await evaluate_row(row, limit=limit) for row in rows]
    total = len(results)
    passed = sum(1 for r in results if r["score"]["passed"])  # type: ignore[index]
    recalls = [float(r["score"]["token_recall"]) for r in results]  # type: ignore[index]
    mean_recall = round(sum(recalls) / total, 4) if total else 0.0
    return {
        "metric": "transcript_retrieval",
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "mean_token_recall": mean_recall,
        "limit": limit,
        "rows": results,
    }


# --- CLI ------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Score Vexic transcript retrieval against a LongMemEval-style "
            "dataset. Deterministic and host-port-free."
        )
    )
    parser.add_argument(
        "--dataset",
        default="tests/fixtures/longmemeval_s_smoke.jsonl",
        help=(
            "Path to the .jsonl eval dataset "
            "(default: tests/fixtures/longmemeval_s_smoke.jsonl)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Evaluate at most this many rows.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of transcript hits retrieved per question (default: 5).",
    )
    return parser


def _print_human_summary(summary: dict[str, object]) -> None:
    print("Vexic transcript-retrieval eval")
    print(f"  metric:            {summary['metric']}")
    print(f"  top-k:             {summary['limit']}")
    print(f"  rows:              {summary['total']}")
    print(f"  passed:            {summary['passed']}")
    print(f"  pass_rate:         {summary['pass_rate']}")
    print(f"  mean_token_recall: {summary['mean_token_recall']}")
    print("  per-row:")
    for row in summary["rows"]:  # type: ignore[union-attr]
        score = row["score"]  # type: ignore[index]
        status = "PASS" if score["passed"] else "FAIL"
        print(
            f"    [{status}] {row['id']}: "  # type: ignore[index]
            f"recall={score['token_recall']} "
            f"hits={row['hits']} "  # type: ignore[index]
            f"ingested={row['messages_ingested']}"  # type: ignore[index]
        )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.top_k < 1:
        print("--top-k must be at least 1.", file=sys.stderr)
        return 0
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1.", file=sys.stderr)
        return 0

    try:
        rows = load_dataset(Path(args.dataset), limit=args.limit)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 0

    summary = asyncio.run(run(rows, limit=args.top_k))
    _print_human_summary(summary)
    # Machine-readable final line.
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
