import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from vexic.embeddings import EMBEDDING_DIM
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.models import FactCandidate
from vexic.storage.schema import (
    DreamStatus,
    _embedding_blob_to_list,
    _ensure_vector_memory_schema,
    _fts_match_query,
    _normalize_embedding,
    _serialize_float32,
    init_db,
    init_vector_memory,
)
from vexic.storage.connection import connect
from vexic.storage.errors import is_operational_error
from vexic.storage.vectors import select_vector_backend

# Tier 2 candidate-fallback retrieval from the hosted MCP design: the eligibility
# predicate shared with Deep/REM — active, unpromoted candidates only. Kept as
# one constant so the fallback retrievers cannot drift from load_*_candidates.
_ACTIVE_CANDIDATE_PREDICATE = (
    "c.promoted = 0 AND c.retired = 0 AND c.stale = 0 AND c.needs_review = 0"
)

# Tier 2 — short-term reinforcement staging. Owns vector dedup (insert / merge /
# review), the dedup-event ledger, and the eligibility queries the Deep phase
# reads. The conn-scoped promotion helpers at the bottom (read / claim / link)
# are reused by the promotion module so the cross-tier transaction stays in one
# connection; they are the only Tier-2 surface promotion touches.

DedupDecision = Literal["insert", "merge", "review"]
DEDUP_NO_MATCH_THRESHOLD = 0.75
DEDUP_MERGE_THRESHOLD = 0.85
DEDUP_NEIGHBOR_COUNT = 10


@dataclass(frozen=True)
class DedupMatch:
    candidate_id: int
    similarity: float


@dataclass(frozen=True)
class DedupStats:
    inserted: int = 0
    merged: int = 0
    review: int = 0


@dataclass(frozen=True)
class PromotionCandidate:
    # An eligible Tier 2 candidate (unpromoted, non-retired, non-stale,
    # needs_review=0) loaded for Deep-phase scoring and possible promotion.
    candidate_id: int
    fact_text: str
    subject: str
    category: str
    confidence: float
    importance: int
    hit_count: int
    last_seen_at: datetime
    rem_boost: float
    embedding: list[float]
    occurred_at: str | None = None


@dataclass(frozen=True)
class CandidateNote:
    # A Tier 2 candidate surfaced by candidate-fallback retrieval,
    # carrying only what the unverified-note surface shows: text, category, and
    # glass-box provenance. The candidate's LLM confidence is deliberately left
    # out — it is misleadingly high for unvetted material, replaced by words in
    # the presentation layer.
    candidate_id: int
    fact_text: str
    category: str
    source_message_ids: list[int]
    created_at: str


@dataclass(frozen=True)
class RemCandidate:
    # Minimal REM input: the id and the stored embedding the centrality
    # heuristic scores against -- None when the vector is missing (e.g. an
    # interrupted Light repair), which scores 0.0 and resets any stale boost.
    # No text, provenance, counters, or lifecycle flags.
    candidate_id: int
    embedding: list[float] | None = None


@dataclass(frozen=True)
class RemStats:
    boosted: int = 0


def _nearest_candidate(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    embedding: list[float],
    *,
    agent_id: str | None,
) -> DedupMatch | None:
    # Dedup must find the nearest neighbor AMONG the merge-eligible set (same
    # subject + category + agent, still live in staging), not the nearest global
    # vector that happens to also be eligible. A KNN-then-filter query applies
    # its `k`/MATCH limit before the eligibility JOIN, so an eligible duplicate
    # ranked outside the global top-k is invisible and gets wrongly inserted as
    # a fresh candidate — a staging duplicate, violating "reinforce/merge, don't
    # duplicate". The eligible set here is small by construction (same subject
    # AND category AND agent), so we filter first in plain SQL, then rank in
    # Python. Both stored and incoming embeddings are normalized, so the dot
    # product IS cosine similarity and equals what both vector backends'
    # .similarity() would return (sqlite-vec: 1 - L2^2/2; libSQL: 1 - cos_dist)
    # — this keeps backend parity without touching the vector index and does not
    # weaken the dedup thresholds.
    rows = conn.execute(
        """
        SELECT e.candidate_id, e.embedding
        FROM memory_candidate_embeddings AS e
        JOIN memory_candidates AS c
            ON c.id = e.candidate_id
        WHERE c.subject = ?
            AND c.category = ?
            AND c.promoted = 0
            AND c.retired = 0
            AND c.stale = 0
            AND c.needs_review = 0
            AND c.agent_id IS ?
        """,
        (
            candidate.subject,
            candidate.category,
            agent_id,
        ),
    ).fetchall()

    if not rows:
        return None

    best_id: int | None = None
    best_similarity = -2.0
    for row in rows:
        stored = _embedding_blob_to_list(row[1])
        similarity = sum(
            incoming_value * stored_value
            for incoming_value, stored_value in zip(embedding, stored, strict=True)
        )
        if similarity > best_similarity:
            best_similarity = similarity
            best_id = int(row[0])

    if best_id is None:
        return None
    return DedupMatch(
        candidate_id=best_id,
        similarity=max(-1.0, min(1.0, best_similarity)),
    )


def _load_source_message_ids(value: str) -> list[int]:
    raw_ids = json.loads(value)
    return [int(message_id) for message_id in raw_ids]


def _guard_candidate_texts(
    forbidden_secret_values: Iterable[str],
    candidates: Iterable[FactCandidate],
) -> None:
    texts: list[str] = []
    for candidate in candidates:
        texts.extend([
            candidate.fact_text,
            candidate.subject,
            candidate.category,
        ])
    assert_no_forbidden_secret_values(forbidden_secret_values, *texts)


def _merge_source_message_ids(existing_json: str, new_ids: list[int]) -> str:
    merged = sorted(set(_load_source_message_ids(existing_json)) | set(new_ids))
    return json.dumps(merged)


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    embedding: list[float],
    *,
    agent_id: str | None,
    needs_review: bool,
    review_neighbor_id: int | None,
    best_similarity: float | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO memory_candidates
            (fact_text, subject, category, importance, confidence,
             source_message_ids, agent_id, editable, needs_review, review_neighbor_id,
             best_similarity, occurred_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            candidate.fact_text,
            candidate.subject,
            candidate.category,
            candidate.importance,
            candidate.confidence,
            json.dumps(sorted(set(candidate.source_message_ids))),
            agent_id,
            candidate.editable,
            needs_review,
            review_neighbor_id,
            best_similarity,
            candidate.occurred_at,
        ),
    )
    candidate_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO memory_candidate_embeddings (candidate_id, embedding)
        VALUES (?, ?)
        """,
        (candidate_id, _serialize_float32(embedding)),
    )
    return candidate_id


def _replace_candidate_embedding(
    conn: sqlite3.Connection,
    candidate_id: int,
    embedding: list[float],
) -> None:
    conn.execute(
        "DELETE FROM memory_candidate_embeddings WHERE candidate_id = ?",
        (candidate_id,),
    )
    conn.execute(
        """
        INSERT INTO memory_candidate_embeddings (candidate_id, embedding)
        VALUES (?, ?)
        """,
        (candidate_id, _serialize_float32(embedding)),
    )


def _merged_embedding(
    conn: sqlite3.Connection,
    candidate_id: int,
    incoming_embedding: list[float],
    current_hit_count: int,
) -> list[float]:
    row = conn.execute(
        """
        SELECT embedding
        FROM memory_candidate_embeddings
        WHERE candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        return _normalize_embedding(incoming_embedding)

    existing_embedding = _embedding_blob_to_list(row[0])
    total = current_hit_count + 1
    merged = [
        ((existing_value * current_hit_count) + incoming_value) / total
        for existing_value, incoming_value in zip(existing_embedding, incoming_embedding, strict=True)
    ]
    return _normalize_embedding(merged)


def _merge_candidate(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    embedding: list[float],
    *,
    match: DedupMatch,
) -> int:
    row = conn.execute(
        """
        SELECT source_message_ids, hit_count
        FROM memory_candidates
        WHERE id = ?
        """,
        (match.candidate_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Candidate embedding pointed at missing candidate {match.candidate_id}.")

    current_hit_count = int(row[1])
    merged_source_ids = _merge_source_message_ids(row[0], candidate.source_message_ids)
    conn.execute(
        """
        UPDATE memory_candidates
        SET hit_count = hit_count + 1,
            last_seen_at = CURRENT_TIMESTAMP,
            source_message_ids = ?,
            importance = MAX(importance, ?),
            confidence = MAX(confidence, ?),
            best_similarity = ?,
            occurred_at = COALESCE(NULLIF(occurred_at, ''), NULLIF(?, ''))
        WHERE id = ?
        """,
        (
            merged_source_ids,
            candidate.importance,
            candidate.confidence,
            match.similarity,
            candidate.occurred_at,
            match.candidate_id,
        ),
    )
    _replace_candidate_embedding(
        conn,
        match.candidate_id,
        _merged_embedding(conn, match.candidate_id, embedding, current_hit_count),
    )
    return match.candidate_id


def _stale_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    best_similarity: float,
) -> None:
    conn.execute(
        """
        UPDATE memory_candidates
        SET stale = 1,
            best_similarity = ?
        WHERE id = ?
        """,
        (best_similarity, candidate_id),
    )


def _flag_candidate_for_review(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    review_neighbor_id: int,
    best_similarity: float,
) -> None:
    conn.execute(
        """
        UPDATE memory_candidates
        SET needs_review = 1,
            review_neighbor_id = ?,
            best_similarity = ?
        WHERE id = ?
        """,
        (review_neighbor_id, best_similarity, candidate_id),
    )


def _log_dedup_event(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    matched_candidate_id: int | None,
    best_similarity: float | None,
    decision: DedupDecision,
    incoming_fact_text: str,
    incoming_source_message_ids: list[int],
) -> None:
    conn.execute(
        """
        INSERT INTO memory_dedup_events
            (candidate_id, matched_candidate_id, best_similarity, decision,
             incoming_fact_text, incoming_source_message_ids)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            matched_candidate_id,
            best_similarity,
            decision,
            incoming_fact_text,
            json.dumps(sorted(set(incoming_source_message_ids))),
        ),
    )


def _commit_candidates_with_dedup(
    conn: sqlite3.Connection,
    candidates: list[FactCandidate],
    candidate_embeddings: list[list[float]],
    *,
    agent_id: str | None,
) -> DedupStats:
    inserted = 0
    merged = 0
    review = 0

    for candidate, embedding in zip(candidates, candidate_embeddings, strict=True):
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(f"Expected {EMBEDDING_DIM}-dim embedding; got {len(embedding)}.")

        embedding = _normalize_embedding(embedding)
        match = _nearest_candidate(conn, candidate, embedding, agent_id=agent_id)
        if match is None or match.similarity < DEDUP_NO_MATCH_THRESHOLD:
            candidate_id = _insert_candidate(
                conn,
                candidate,
                embedding,
                agent_id=agent_id,
                needs_review=False,
                review_neighbor_id=None,
                best_similarity=None if match is None else match.similarity,
            )
            _log_dedup_event(
                conn,
                candidate_id=candidate_id,
                matched_candidate_id=None if match is None else match.candidate_id,
                best_similarity=None if match is None else match.similarity,
                decision="insert",
                incoming_fact_text=candidate.fact_text,
                incoming_source_message_ids=candidate.source_message_ids,
            )
            inserted += 1
        elif match.similarity >= DEDUP_MERGE_THRESHOLD:
            candidate_id = _merge_candidate(conn, candidate, embedding, match=match)
            _log_dedup_event(
                conn,
                candidate_id=candidate_id,
                matched_candidate_id=match.candidate_id,
                best_similarity=match.similarity,
                decision="merge",
                incoming_fact_text=candidate.fact_text,
                incoming_source_message_ids=candidate.source_message_ids,
            )
            merged += 1
        else:
            candidate_id = _insert_candidate(
                conn,
                candidate,
                embedding,
                agent_id=agent_id,
                needs_review=True,
                review_neighbor_id=match.candidate_id,
                best_similarity=match.similarity,
            )
            _log_dedup_event(
                conn,
                candidate_id=candidate_id,
                matched_candidate_id=match.candidate_id,
                best_similarity=match.similarity,
                decision="review",
                incoming_fact_text=candidate.fact_text,
                incoming_source_message_ids=candidate.source_message_ids,
            )
            review += 1

    return DedupStats(inserted=inserted, merged=merged, review=review)


def _load_candidate_by_id(conn: sqlite3.Connection, candidate_id: int) -> FactCandidate:
    row = conn.execute(
        """
        SELECT fact_text, subject, category, importance, confidence,
               source_message_ids, editable, occurred_at
        FROM memory_candidates
        WHERE id = ?
        """,
        (candidate_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Missing memory candidate {candidate_id}.")

    return FactCandidate(
        fact_text=row[0],
        subject=row[1],
        category=row[2],
        importance=row[3],
        confidence=row[4],
        source_message_ids=_load_source_message_ids(row[5]),
        editable=bool(row[6]),
        occurred_at=row[7],
    )


def _candidate_agent_id(conn: sqlite3.Connection, candidate_id: int) -> str | None:
    row = conn.execute(
        "SELECT agent_id FROM memory_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Missing memory candidate {candidate_id}.")
    return row[0]


def backfill_missing_candidate_embeddings(
    db_path: str,
    candidate_embeddings: list[tuple[int, list[float]]],
    *,
    forbidden_secret_values: Iterable[str] = (),
) -> int:
    init_vector_memory(db_path)
    with closing(connect(db_path)) as conn:
        with conn:
            _ensure_vector_memory_schema(conn)
            count = 0
            for candidate_id, embedding in candidate_embeddings:
                if len(embedding) != EMBEDDING_DIM:
                    raise ValueError(f"Expected {EMBEDDING_DIM}-dim embedding; got {len(embedding)}.")
                candidate = _load_candidate_by_id(conn, candidate_id)
                _guard_candidate_texts(forbidden_secret_values, [candidate])
                embedding = _normalize_embedding(embedding)
                match = _nearest_candidate(
                    conn,
                    candidate,
                    embedding,
                    agent_id=_candidate_agent_id(conn, candidate_id),
                )

                if match is None or match.similarity < DEDUP_NO_MATCH_THRESHOLD:
                    _replace_candidate_embedding(conn, candidate_id, embedding)
                    _log_dedup_event(
                        conn,
                        candidate_id=candidate_id,
                        matched_candidate_id=None if match is None else match.candidate_id,
                        best_similarity=None if match is None else match.similarity,
                        decision="insert",
                        incoming_fact_text=candidate.fact_text,
                        incoming_source_message_ids=candidate.source_message_ids,
                    )
                elif match.similarity >= DEDUP_MERGE_THRESHOLD:
                    merged_id = _merge_candidate(conn, candidate, embedding, match=match)
                    _stale_candidate(
                        conn,
                        candidate_id=candidate_id,
                        best_similarity=match.similarity,
                    )
                    _log_dedup_event(
                        conn,
                        candidate_id=merged_id,
                        matched_candidate_id=match.candidate_id,
                        best_similarity=match.similarity,
                        decision="merge",
                        incoming_fact_text=candidate.fact_text,
                        incoming_source_message_ids=candidate.source_message_ids,
                    )
                else:
                    _flag_candidate_for_review(
                        conn,
                        candidate_id=candidate_id,
                        review_neighbor_id=match.candidate_id,
                        best_similarity=match.similarity,
                    )
                    _replace_candidate_embedding(conn, candidate_id, embedding)
                    _log_dedup_event(
                        conn,
                        candidate_id=candidate_id,
                        matched_candidate_id=match.candidate_id,
                        best_similarity=match.similarity,
                        decision="review",
                        incoming_fact_text=candidate.fact_text,
                        incoming_source_message_ids=candidate.source_message_ids,
                    )
                count += 1
            return count


def keyword_candidate_ids(
    db_path: str,
    query: str,
    *,
    k: int,
    agent_id: str | None = None,
    as_of: str | None = None,
    event_after: str | None = None,
    event_before: str | None = None,
) -> list[int]:
    """BM25-ranked active candidate ids for a free-text query, best first.

    The keyword half of the candidate-fallback hybrid retriever.
    Filters to the active-candidate predicate so promoted/retired/stale/review
    candidates never surface as unverified notes.

    `as_of`, if given, restricts results to rows where
    `COALESCE(NULLIF(c.occurred_at, ''), c.created_at) <= as_of` — a plain
    TEXT-affinity string comparison. `event_after`/`event_before`, if given,
    are the lower/upper bounds of a temporal range over the same
    `COALESCE(NULLIF(c.occurred_at, ''), c.created_at)` fallback:
    `... >= event_after` and/or `... <= event_before`. All three are optional
    and independent; `event_before` and `as_of` may coexist (both `<=` clauses
    are emitted). `occurred_at` is a partial-precision ISO
    string; a partial string is always lexicographically `<=` any of its own
    completions, so a candidate with an unknown exact day always passes an
    `as_of` or `event_before` check for any cutoff at or after that partial
    period's start.
    `created_at` is the full `"YYYY-MM-DD HH:MM:SS"` fallback used when
    `occurred_at` is NULL or empty — callers must pass these bounds in a
    directly comparable shape (matching separator/precision) or same-day
    boundary comparisons will behave unexpectedly. This is a deliberate,
    documented approximation, not a bug.
    """
    safe_query = _fts_match_query(query, any_token=True)
    if safe_query is None:
        return []

    date_clause = ""
    if as_of is not None:
        date_clause += " AND COALESCE(NULLIF(c.occurred_at, ''), c.created_at) <= ?"
    if event_after is not None:
        date_clause += " AND COALESCE(NULLIF(c.occurred_at, ''), c.created_at) >= ?"
    if event_before is not None:
        date_clause += " AND COALESCE(NULLIF(c.occurred_at, ''), c.created_at) <= ?"

    init_db(db_path)
    with closing(connect(db_path)) as conn:
        try:
            rows = conn.execute(
                f"""
                SELECT f.rowid
                FROM memory_candidates_fts AS f
                JOIN memory_candidates AS c ON c.id = f.rowid
                WHERE memory_candidates_fts MATCH ?
                    AND {_ACTIVE_CANDIDATE_PREDICATE}
                    AND c.agent_id IS ?
                    {date_clause}
                ORDER BY rank
                LIMIT ?
                """,
                (
                    safe_query,
                    agent_id,
                    *((as_of,) if as_of is not None else ()),
                    *((event_after,) if event_after is not None else ()),
                    *((event_before,) if event_before is not None else ()),
                    k,
                ),
            ).fetchall()
        except (sqlite3.OperationalError, ValueError) as exc:
            # A malformed FTS MATCH is a sqlite3.OperationalError locally and a
            # bare ValueError on hosted libSQL (ADR 0019); both mean "no hits".
            # Unrelated ValueErrors re-raise.
            if not is_operational_error(exc):
                raise
            return []
    return [int(row[0]) for row in rows]


def record_candidate_retrieval(
    db_path: str,
    candidate_ids: list[int],
    *,
    session_id: str,
    agent_id: str | None = None,
    query: str,
    forbidden_secret_values: Iterable[str],
) -> list[int]:
    """Reinforcement observation: these candidates were surfaced by fallback.

    Writes one candidate_retrieval_events row per candidate and increments
    retrieved_count in the same transaction, mirroring record_long_term_retrieval
    so the counter stays derivable and rebuild-safe.
    The `used` verdict is deferred — rows stay used = NULL. Returns the new
    event ids in candidate_ids order.
    """
    if not candidate_ids:
        return []
    # Persistence secret guard (docs/ai/AGENTS.md): the query is the one new piece of
    # text this path persists, so it fails closed exactly like save_messages.
    assert_no_forbidden_secret_values(forbidden_secret_values, query)
    event_ids: list[int] = []
    init_db(db_path)
    with closing(connect(db_path)) as conn:
        with conn:
            placeholders = ", ".join("?" for _ in candidate_ids)
            scoped_ids = {
                int(row[0])
                for row in conn.execute(
                    f"""
                    SELECT id
                    FROM memory_candidates
                    WHERE id IN ({placeholders})
                        AND agent_id IS ?
                    """,
                    [*candidate_ids, agent_id],
                ).fetchall()
            }
            scoped_candidate_ids = [
                candidate_id for candidate_id in candidate_ids if candidate_id in scoped_ids
            ]
            if not scoped_candidate_ids:
                return []

            for candidate_id in scoped_candidate_ids:
                cursor = conn.execute(
                    """
                    INSERT INTO candidate_retrieval_events
                        (candidate_id, session_id, agent_id, query)
                    VALUES (?, ?, ?, ?)
                    """,
                    (candidate_id, session_id, agent_id, query),
                )
                event_ids.append(int(cursor.lastrowid))
            scoped_placeholders = ", ".join("?" for _ in scoped_candidate_ids)
            conn.execute(
                f"""
                UPDATE memory_candidates
                SET retrieved_count = retrieved_count + 1
                WHERE id IN ({scoped_placeholders})
                    AND agent_id IS ?
                """,
                [*scoped_candidate_ids, agent_id],
            )
    return event_ids


def fetch_candidate_notes(
    db_path: str,
    candidate_ids: list[int],
    *,
    agent_id: str | None = None,
) -> list[CandidateNote]:
    """Load candidates by id as unverified notes, preserving order; unknown skipped.

    Mirrors fetch_long_term_facts: load only note-visible fields, and re-check
    active eligibility so lifecycle changes after retrieval cannot surface.
    """
    if not candidate_ids:
        return []

    init_db(db_path)
    placeholders = ", ".join("?" for _ in candidate_ids)
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.fact_text, c.category, c.source_message_ids, c.created_at
            FROM memory_candidates AS c
            WHERE c.id IN ({placeholders})
                AND {_ACTIVE_CANDIDATE_PREDICATE}
                AND c.agent_id IS ?
            """,
            [*candidate_ids, agent_id],
        ).fetchall()

    by_id = {
        int(row[0]): CandidateNote(
            candidate_id=int(row[0]),
            fact_text=str(row[1]),
            category=str(row[2]),
            source_message_ids=_load_source_message_ids(row[3]),
            created_at=str(row[4]),
        )
        for row in rows
    }
    return [by_id[candidate_id] for candidate_id in candidate_ids if candidate_id in by_id]


def nearest_candidate_ids(
    db_path: str,
    embedding: list[float],
    *,
    k: int,
    agent_id: str | None = None,
    as_of: str | None = None,
    event_after: str | None = None,
    event_before: str | None = None,
) -> list[int]:
    """sqlite-vec KNN over active candidate embeddings, nearest first.

    The vector half of the candidate-fallback hybrid retriever.
    sqlite-vec applies its KNN before the eligibility join, so over-fetch then
    keep the k nearest active candidates — same shape as nearest_long_term_facts.

    `as_of`, if given, restricts results to rows where
    `COALESCE(NULLIF(c.occurred_at, ''), c.created_at) <= as_of` — a plain
    TEXT-affinity string comparison. `event_after`/`event_before`, if given,
    are the lower/upper bounds of a temporal range over the same
    `COALESCE(NULLIF(c.occurred_at, ''), c.created_at)` fallback:
    `... >= event_after` and/or `... <= event_before`. All three are optional
    and independent; `event_before` and `as_of` may coexist (both `<=` clauses
    are emitted). `occurred_at` is a partial-precision ISO
    string; a partial string is always lexicographically `<=` any of its own
    completions, so a candidate with an unknown exact day always passes an
    `as_of` or `event_before` check for any cutoff at or after that partial
    period's start.
    `created_at` is the full `"YYYY-MM-DD HH:MM:SS"` fallback used when
    `occurred_at` is NULL or empty — callers must pass these bounds in a
    directly comparable shape (matching separator/precision) or same-day
    boundary comparisons will behave unexpectedly. This is a deliberate,
    documented approximation, not a bug.
    """
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim embedding; got {len(embedding)}.")
    normalized = _normalize_embedding(embedding)

    date_clause = ""
    if as_of is not None:
        date_clause += " AND COALESCE(NULLIF(c.occurred_at, ''), c.created_at) <= ?"
    if event_after is not None:
        date_clause += " AND COALESCE(NULLIF(c.occurred_at, ''), c.created_at) >= ?"
    if event_before is not None:
        date_clause += " AND COALESCE(NULLIF(c.occurred_at, ''), c.created_at) <= ?"

    fetch_k = max(k * 4, k + 10)
    init_vector_memory(db_path)
    with closing(connect(db_path)) as conn:
        _ensure_vector_memory_schema(conn)
        backend = select_vector_backend(conn)
        knn = backend.knn_subquery(
            table="memory_candidate_embeddings", id_column="candidate_id"
        )
        rows = conn.execute(
            f"""
            SELECT e._id
            FROM ({knn}) AS e
            JOIN memory_candidates AS c ON c.id = e._id
            WHERE {_ACTIVE_CANDIDATE_PREDICATE}
                AND c.agent_id IS ?
                {date_clause}
            ORDER BY e._distance
            LIMIT ?
            """,
            (
                _serialize_float32(normalized),
                fetch_k,
                agent_id,
                *((as_of,) if as_of is not None else ()),
                *((event_after,) if event_after is not None else ()),
                *((event_before,) if event_before is not None else ()),
                k,
            ),
        ).fetchall()
    return [int(row[0]) for row in rows]


def load_candidates_missing_embeddings(
    db_path: str,
    *,
    agent_id: str | None = None,
) -> list[tuple[int, str]]:
    init_vector_memory(db_path)
    with closing(connect(db_path)) as conn:
        _ensure_vector_memory_schema(conn)
        # Only live Tier-2 staging candidates are eligible for embedding repair.
        # A promoted/retired/needs_review row has already left staging; backfill
        # (which can merge a repaired row into an active neighbor and mark the
        # original stale) must never mutate one — that would drift counters and
        # rewrite an already-promoted candidate. Same active-candidate predicate
        # the promotion/REM loaders enforce.
        rows = conn.execute(
            """
            SELECT c.id, c.fact_text
            FROM memory_candidates AS c
            LEFT JOIN memory_candidate_embeddings AS e
                ON e.candidate_id = c.id
            WHERE e.candidate_id IS NULL
                AND c.promoted = 0
                AND c.retired = 0
                AND c.stale = 0
                AND c.needs_review = 0
                AND c.agent_id IS ?
            ORDER BY c.id ASC
            """,
            (agent_id,),
        ).fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]


def _parse_db_datetime(value: str) -> datetime:
    # Candidate timestamps are stored as naive UTC (SQLite CURRENT_TIMESTAMP)
    # or ISO strings. Normalize to aware UTC for recency math.
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_promotion_candidates(
    db_path: str,
    *,
    agent_id: str | None = None,
) -> list[PromotionCandidate]:
    init_vector_memory(db_path)
    with closing(connect(db_path)) as conn:
        _ensure_vector_memory_schema(conn)
        rows = conn.execute(
            """
            SELECT c.id, c.fact_text, c.subject, c.category, c.confidence,
                   c.importance, c.hit_count, c.last_seen_at, c.rem_boost,
                   e.embedding, c.occurred_at
            FROM memory_candidates AS c
            JOIN memory_candidate_embeddings AS e ON e.candidate_id = c.id
            WHERE c.promoted = 0
                AND c.retired = 0
                AND c.stale = 0
                AND c.needs_review = 0
                AND c.agent_id IS ?
            ORDER BY c.id ASC
            """,
            (agent_id,),
        ).fetchall()

    return [
        PromotionCandidate(
            candidate_id=int(row[0]),
            fact_text=str(row[1]),
            subject=str(row[2]),
            category=str(row[3]),
            confidence=float(row[4]),
            importance=int(row[5]),
            hit_count=int(row[6]),
            last_seen_at=_parse_db_datetime(str(row[7])),
            rem_boost=float(row[8]),
            embedding=_embedding_blob_to_list(row[9]),
            occurred_at=row[10],
        )
        for row in rows
    ]


def load_rem_candidates(db_path: str, *, agent_id: str | None) -> list[RemCandidate]:
    init_vector_memory(db_path)
    with closing(connect(db_path)) as conn:
        _ensure_vector_memory_schema(conn)
        # LEFT JOIN, not INNER: a candidate whose embedding is missing must
        # still be returned so REM writes it a 0.0 boost, resetting any stale
        # boost from an earlier cycle.
        rows = conn.execute(
            """
            SELECT c.id, e.embedding
            FROM memory_candidates AS c
            LEFT JOIN memory_candidate_embeddings AS e ON e.candidate_id = c.id
            WHERE c.promoted = 0
                AND c.retired = 0
                AND c.stale = 0
                AND c.needs_review = 0
                AND c.agent_id IS ?
            ORDER BY c.id ASC
            """,
            (agent_id,),
        ).fetchall()

    return [
        RemCandidate(
            candidate_id=int(row[0]),
            embedding=None if row[1] is None else _embedding_blob_to_list(row[1]),
        )
        for row in rows
    ]


def commit_rem_cycle(
    db_path: str,
    boosts: dict[int, float],
    *,
    agent_id: str | None,
    started_at: str,
    finished_at: str | None,
    status: DreamStatus = "ok",
    error_detail: str | None = None,
    model_requests: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost_micros: int = 0,
    forbidden_secret_values: Iterable[str] = (),
) -> RemStats:
    init_db(db_path)
    if status == "error" and boosts:
        raise ValueError("Error REM cycles must not include boosts.")
    assert_no_forbidden_secret_values(forbidden_secret_values, error_detail or "")
    for boost in boosts.values():
        if not 0.0 <= boost <= 1.0:
            raise ValueError(f"REM boost must be between 0 and 1, got {boost}.")

    with closing(connect(db_path)) as conn:
        with conn:
            boosted = 0
            for candidate_id, boost in boosts.items():
                updated = conn.execute(
                    """
                    UPDATE memory_candidates
                    SET rem_boost = ?
                    WHERE id = ?
                        AND agent_id IS ?
                        AND promoted = 0
                        AND retired = 0
                        AND stale = 0
                    """,
                    (boost, candidate_id, agent_id),
                )
                boosted += updated.rowcount

            conn.execute(
                """
                INSERT INTO dream_runs
                    (started_at, finished_at, status, agent_id, messages_processed,
                     last_processed_message_id, candidates_boosted, error_detail,
                     model_requests, input_tokens, output_tokens, total_tokens,
                     estimated_cost_micros)
                VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    finished_at,
                    status,
                    agent_id,
                    boosted,
                    error_detail,
                    model_requests,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    estimated_cost_micros,
                ),
            )

    return RemStats(boosted=boosted)


def _current_ok_watermark(conn: sqlite3.Connection, agent_id: str | None) -> int:
    # Conn-scoped read of the Light watermark inside the open commit
    # transaction, mirroring transcript.get_watermark's MAX-over-ok-runs query.
    # Reading through the same locked transaction is what makes the
    # compare-and-set below atomic against a concurrent Light run.
    row = conn.execute(
        """
        SELECT MAX(last_processed_message_id)
        FROM dream_runs
        WHERE agent_id IS ?
            AND status = 'ok'
        """,
        (agent_id,),
    ).fetchone()
    return row[0] if row[0] is not None else 0


def commit_dream_cycle(
    db_path: str,
    candidates: list[FactCandidate],
    *,
    agent_id: str | None,
    status: DreamStatus,
    started_at: str,
    finished_at: str | None,
    messages_processed: int,
    last_processed_message_id: int,
    error_detail: str | None = None,
    candidate_embeddings: list[list[float]] | None = None,
    observed_watermark: int | None = None,
    model_requests: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost_micros: int = 0,
    forbidden_secret_values: Iterable[str] = (),
) -> None:
    init_db(db_path)
    if status == "error" and candidates:
        raise ValueError("Error dream cycles must not include candidates.")
    if candidate_embeddings is not None and len(candidate_embeddings) != len(candidates):
        raise ValueError("candidate_embeddings must match candidates length.")
    if candidates and candidate_embeddings is None:
        raise ValueError("candidate_embeddings are required when committing candidates.")
    _guard_candidate_texts(forbidden_secret_values, candidates)
    assert_no_forbidden_secret_values(forbidden_secret_values, error_detail or "")

    with closing(connect(db_path)) as conn:
        # Explicit transaction, not the bare `with conn:` used elsewhere: the
        # optional compare-and-set below reads the watermark and then writes in
        # one atomic unit, and libSQL only opens a real multi-statement
        # transaction on an explicit BEGIN (ADR 0019 caveat in connection.py).
        # sqlite gets BEGIN IMMEDIATE so the write lock is taken before the
        # re-read; a second concurrent Light run then blocks (or fails busy)
        # instead of reading a stale watermark and double-writing.
        if isinstance(conn, sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        try:
            # Watermark compare-and-set: if the caller observed watermark W at
            # read time but another Light run has since advanced it, this window
            # was already processed. Abort as an audit-only no-op rather than
            # re-committing the same candidates (double hit_count / duplicate
            # staging rows) and re-advancing the watermark. Only 'ok' cycles
            # carry a watermark to defend; error/no-op audit rows never claim
            # to have processed the window.
            superseded = (
                observed_watermark is not None
                and status == "ok"
                and _current_ok_watermark(conn, agent_id) != observed_watermark
            )

            stats = DedupStats()
            if candidates and not superseded:
                _ensure_vector_memory_schema(conn)
                stats = _commit_candidates_with_dedup(
                    conn,
                    candidates,
                    candidate_embeddings or [],
                    agent_id=agent_id,
                )

            if superseded:
                # A no-op audit row: no candidates, and last_processed_message_id
                # left at 0 so it cannot lift MAX(last_processed_message_id) past
                # the run that already advanced the watermark.
                conn.execute(
                    """
                    INSERT INTO dream_runs
                        (started_at, finished_at, status, agent_id, messages_processed,
                         candidates_inserted, candidates_merged, candidates_review,
                         last_processed_message_id, error_detail, model_requests,
                         input_tokens, output_tokens, total_tokens, estimated_cost_micros)
                    VALUES (?, ?, 'ok', ?, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        started_at,
                        finished_at,
                        agent_id,
                        "superseded: watermark advanced by a concurrent Light run",
                        model_requests,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        estimated_cost_micros,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO dream_runs
                        (started_at, finished_at, status, agent_id, messages_processed,
                         candidates_inserted, candidates_merged, candidates_review,
                         last_processed_message_id, error_detail, model_requests,
                         input_tokens, output_tokens, total_tokens, estimated_cost_micros)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        started_at,
                        finished_at,
                        status,
                        agent_id,
                        messages_processed,
                        stats.inserted + stats.review,
                        stats.merged,
                        stats.review,
                        last_processed_message_id,
                        error_detail,
                        model_requests,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        estimated_cost_micros,
                    ),
                )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def read_candidate_for_promotion(
    conn: sqlite3.Connection,
    candidate_id: int,
) -> tuple | None:
    # Conn-scoped read the promotion module uses inside its cross-tier
    # transaction. Returns the full eligibility row, or None when the candidate
    # is missing. Column order is the promotion module's contract; occurred_at
    # is appended last so the module's fixed-arity unpack keeps working.
    return conn.execute(
        """
        SELECT fact_text, subject, category, importance, confidence,
               source_message_ids, agent_id, editable, retrieved_count, used_count,
               promoted, retired, stale, occurred_at
        FROM memory_candidates
        WHERE id = ?
        """,
        (candidate_id,),
    ).fetchone()


def claim_candidate_for_promotion(
    conn: sqlite3.Connection,
    candidate_id: int,
) -> bool:
    # Atomic claim: the conditional WHERE is the real concurrency guard. If a
    # concurrent deep cycle already flipped this candidate to promoted, rowcount
    # is 0 and the caller must abort without a duplicate Tier 3 write. Returns
    # True iff this caller won the claim.
    #
    # `needs_review = 0` mirrors the loader's eligibility predicate: a candidate
    # flagged for review AFTER selection but before this claim (e.g. a
    # concurrent backfill/merge that raised best_similarity into the review
    # band) must not sneak into Tier 3. The read in read_candidate_for_promotion
    # does not carry needs_review, so this claim is the only guard against that
    # race — a lost claim returns rowcount 0 and the caller aborts cleanly.
    claim = conn.execute(
        """
        UPDATE memory_candidates
        SET promoted = 1
        WHERE id = ? AND promoted = 0 AND retired = 0 AND stale = 0
            AND needs_review = 0
        """,
        (candidate_id,),
    )
    return claim.rowcount > 0


def retire_candidate_for_fact(
    conn: sqlite3.Connection,
    candidate_id: int,
    retired_by_fact_id: int,
    *,
    agent_id: str | None,
) -> bool:
    # Conn-scoped retirement used by the promotion module. The winning Tier 3
    # fact id is known only inside the cross-tier transaction, so candidate
    # retirement stays behind the promotion seam.
    retired = conn.execute(
        """
        UPDATE memory_candidates
        SET retired = 1,
            retired_at = CURRENT_TIMESTAMP,
            retired_by_fact_id = ?
        WHERE id = ?
            AND agent_id IS ?
            AND promoted = 0
            AND retired = 0
            AND stale = 0
        """,
        (retired_by_fact_id, candidate_id, agent_id),
    )
    return retired.rowcount > 0


def link_candidate_to_promoted_fact(
    conn: sqlite3.Connection,
    candidate_id: int,
    fact_id: int,
) -> None:
    conn.execute(
        "UPDATE memory_candidates SET promoted_fact_id = ? WHERE id = ?",
        (fact_id, candidate_id),
    )
