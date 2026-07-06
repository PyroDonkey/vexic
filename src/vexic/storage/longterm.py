import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass

from vexic.embeddings import EMBEDDING_DIM
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage.schema import (
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

# Tier 3 — durable, vector-indexed facts. Owns nearest-neighbor retrieval and
# the conn-scoped insert/retire primitives the promotion module calls inside its
# cross-tier transaction. Knows nothing about Tier 2; promotion is the only
# thing that bridges the two.


@dataclass(frozen=True)
class LongTermNeighbor:
    fact_id: int
    fact_text: str
    similarity: float
    confidence: float


# Provenance-rich Tier 3 row for the hybrid retrieval path. Carries everything
# the requesting agent is allowed to see (glass-box: text + provenance), never
# the embedding or lifecycle internals.
@dataclass(frozen=True)
class LongTermFact:
    fact_id: int
    fact_text: str
    subject: str
    category: str
    importance: int
    confidence: float
    source_message_ids: list[int]
    retrieved_count: int
    used_count: int
    editable: bool = True
    created_at: str = ""
    occurred_at: str | None = None


def keyword_long_term_fact_ids(
    db_path: str,
    query: str,
    *,
    k: int,
    agent_id: str | None = None,
    as_of: str | None = None,
    event_after: str | None = None,
    event_before: str | None = None,
) -> list[int]:
    """BM25-ranked live Tier 3 fact ids for a free-text query, best first.

    `as_of`, if given, restricts results to rows where
    `COALESCE(NULLIF(occurred_at, ''), created_at) <= as_of` -- a plain
    TEXT-affinity string comparison. `event_after`/`event_before`, if given,
    are the lower/upper bounds of a temporal range over the same
    `COALESCE(NULLIF(occurred_at, ''), created_at)` fallback:
    `... >= event_after` and/or `... <= event_before`. All three are optional
    and independent; `event_before` and `as_of` may coexist (both `<=` clauses
    are emitted). `occurred_at` is a partial-precision ISO
    string; a partial string is always lexicographically `<=` any of its own
    completions, so a fact with an unknown exact day always passes an `as_of`
    or `event_before` check for any cutoff at or after that partial period's
    start. `created_at`
    is the full `"YYYY-MM-DD HH:MM:SS"` fallback used when `occurred_at` is
    NULL or empty -- callers must pass these bounds in a directly comparable
    shape (matching separator/precision) or same-day boundary comparisons will
    behave unexpectedly. This is a deliberate, documented approximation, not
    a bug.
    """
    safe_query = _fts_match_query(query, any_token=True)
    if safe_query is None:
        return []

    date_clause = ""
    params: list[object] = [safe_query, agent_id]
    if as_of is not None:
        date_clause += " AND COALESCE(NULLIF(l.occurred_at, ''), l.created_at) <= ?"
        params.append(as_of)
    if event_after is not None:
        date_clause += " AND COALESCE(NULLIF(l.occurred_at, ''), l.created_at) >= ?"
        params.append(event_after)
    if event_before is not None:
        date_clause += " AND COALESCE(NULLIF(l.occurred_at, ''), l.created_at) <= ?"
        params.append(event_before)
    params.append(k)

    init_db(db_path)
    with closing(connect(db_path)) as conn:
        try:
            rows = conn.execute(
                f"""
                SELECT f.rowid
                FROM long_term_memory_fts AS f
                JOIN long_term_memory AS l ON l.id = f.rowid
                WHERE long_term_memory_fts MATCH ?
                    AND l.retired = 0
                    AND l.agent_id IS ?
                    {date_clause}
                ORDER BY rank
                LIMIT ?
                """,
                params,
            ).fetchall()
        except (sqlite3.OperationalError, ValueError) as exc:
            # A malformed FTS MATCH is a sqlite3.OperationalError locally and a
            # bare ValueError on hosted libSQL (ADR 0019); both mean "no hits".
            # Unrelated ValueErrors re-raise.
            if not is_operational_error(exc):
                raise
            return []
    return [int(row[0]) for row in rows]


def fetch_long_term_facts(
    db_path: str,
    fact_ids: list[int],
    *,
    agent_id: str | None = None,
) -> list[LongTermFact]:
    """Load Tier 3 facts by id, preserving the given order; unknown ids skipped."""
    if not fact_ids:
        return []

    init_db(db_path)
    placeholders = ", ".join("?" for _ in fact_ids)
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT id, fact_text, subject, category, importance, confidence,
                   source_message_ids, retrieved_count, used_count, editable, created_at,
                   occurred_at
            FROM long_term_memory
            WHERE id IN ({placeholders})
                AND agent_id IS ?
            """,
            [*fact_ids, agent_id],
        ).fetchall()

    by_id = {
        int(row[0]): LongTermFact(
            fact_id=int(row[0]),
            fact_text=str(row[1]),
            subject=str(row[2]),
            category=str(row[3]),
            importance=int(row[4]),
            confidence=float(row[5]),
            source_message_ids=[int(value) for value in json.loads(row[6])],
            retrieved_count=int(row[7]),
            used_count=int(row[8]),
            editable=bool(row[9]),
            created_at=str(row[10]),
            occurred_at=row[11],
        )
        for row in rows
    }
    return [by_id[fact_id] for fact_id in fact_ids if fact_id in by_id]


def _increment_counter(db_path: str, column: str, fact_ids: list[int]) -> None:
    if not fact_ids:
        return
    placeholders = ", ".join("?" for _ in fact_ids)
    with closing(connect(db_path)) as conn:
        with conn:
            conn.execute(
                f"""
                UPDATE long_term_memory
                SET {column} = {column} + 1
                WHERE id IN ({placeholders})
                """,
                fact_ids,
            )


def record_long_term_retrieval(
    db_path: str,
    fact_ids: list[int],
    *,
    session_id: str,
    agent_id: str | None = None,
    query: str,
    rewritten_query: str | None = None,
    keyword_fact_ids: Sequence[int] = (),
    vector_fact_ids: Sequence[int] = (),
    fused_fact_ids: Sequence[int] = (),
    forbidden_secret_values: Iterable[str],
) -> list[int]:
    """Reinforcement observation: these facts were surfaced by retrieval.

    Writes one retrieval_events row per fact and increments retrieved_count in
    the same transaction so counters and events cannot disagree.
    Recorded at the moment of retrieval, never recomputed. Returns the new
    event ids in fact_ids order so the use judge can target these exact rows.
    """
    if not fact_ids:
        return []
    # Persistence secret guard (docs/ai/AGENTS.md): retrieval persists original and
    # rewritten query text, so both fail closed exactly like save_messages.
    assert_no_forbidden_secret_values(
        forbidden_secret_values,
        query,
        "" if rewritten_query is None else rewritten_query,
    )
    keyword_fact_ids_json = json.dumps(list(keyword_fact_ids))
    vector_fact_ids_json = json.dumps(list(vector_fact_ids))
    fused_fact_ids_json = json.dumps(list(fused_fact_ids))
    event_ids: list[int] = []
    with closing(connect(db_path)) as conn:
        with conn:
            placeholders = ", ".join("?" for _ in fact_ids)
            scoped_ids = {
                int(row[0])
                for row in conn.execute(
                    f"""
                    SELECT id
                    FROM long_term_memory
                    WHERE id IN ({placeholders})
                        AND agent_id IS ?
                    """,
                    [*fact_ids, agent_id],
                ).fetchall()
            }
            scoped_fact_ids = [fact_id for fact_id in fact_ids if fact_id in scoped_ids]
            if not scoped_fact_ids:
                return []

            for fact_id in scoped_fact_ids:
                cursor = conn.execute(
                    """
                    INSERT INTO retrieval_events
                        (fact_id, session_id, agent_id, query, rewritten_query,
                         keyword_fact_ids, vector_fact_ids, fused_fact_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact_id,
                        session_id,
                        agent_id,
                        query,
                        rewritten_query,
                        keyword_fact_ids_json,
                        vector_fact_ids_json,
                        fused_fact_ids_json,
                    ),
                )
                event_ids.append(int(cursor.lastrowid))
            scoped_placeholders = ", ".join("?" for _ in scoped_fact_ids)
            conn.execute(
                f"""
                UPDATE long_term_memory
                SET retrieved_count = retrieved_count + 1
                WHERE id IN ({scoped_placeholders})
                    AND agent_id IS ?
                """,
                [*scoped_fact_ids, agent_id],
            )
    return event_ids


def record_fact_use_verdict(
    db_path: str,
    *,
    used_event_ids: list[int],
    unused_event_ids: list[int],
) -> None:
    """Land a use-judge verdict: mark this turn's retrieval events judged and
    increment used_count for the used facts, in one transaction.
    Events not in either list stay used = NULL — judge never ran on them.
    """
    if not used_event_ids and not unused_event_ids:
        return
    with closing(connect(db_path)) as conn:
        with conn:
            if used_event_ids:
                placeholders = ", ".join("?" for _ in used_event_ids)
                conn.execute(
                    f"""
                    UPDATE long_term_memory
                    SET used_count = used_count + (
                        SELECT COUNT(*)
                        FROM retrieval_events
                        WHERE retrieval_events.fact_id = long_term_memory.id
                        AND retrieval_events.id IN ({placeholders})
                        AND retrieval_events.used IS NULL
                    )
                    WHERE id IN (
                        SELECT fact_id FROM retrieval_events
                        WHERE id IN ({placeholders})
                        AND used IS NULL
                    )
                    """,
                    [*used_event_ids, *used_event_ids],
                )
            for event_ids, used in ((used_event_ids, 1), (unused_event_ids, 0)):
                if not event_ids:
                    continue
                placeholders = ", ".join("?" for _ in event_ids)
                conn.execute(
                    f"""
                    UPDATE retrieval_events
                    SET used = ?, judged_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders}) AND used IS NULL
                    """,
                    [used, *event_ids],
                )


def record_long_term_use(db_path: str, fact_ids: list[int]) -> None:
    """Reinforcement observation: bare used_count increment by fact id.

    The wired production path is `record_fact_use_verdict`, which lands the
    use judge's verdict on retrieval_events and increments used_count in one
    transaction. This primitive remains for callers that have fact ids but no
    event rows (manual corrections, backfills).
    """
    _increment_counter(db_path, "used_count", fact_ids)


def nearest_long_term_facts(
    db_path: str,
    embedding: list[float],
    *,
    k: int = 3,
    agent_id: str | None = None,
    as_of: str | None = None,
    event_after: str | None = None,
    event_before: str | None = None,
) -> list[LongTermNeighbor]:
    """Nearest live Tier 3 facts to `embedding` by cosine distance, best first.

    `as_of`, if given, restricts results to rows where
    `COALESCE(NULLIF(occurred_at, ''), created_at) <= as_of` -- a plain
    TEXT-affinity string comparison. `event_after`/`event_before`, if given,
    are the lower/upper bounds of a temporal range over the same
    `COALESCE(NULLIF(occurred_at, ''), created_at)` fallback:
    `... >= event_after` and/or `... <= event_before`. All three are optional
    and independent; `event_before` and `as_of` may coexist (both `<=` clauses
    are emitted). `occurred_at` is a partial-precision ISO
    string; a partial string is always lexicographically `<=` any of its own
    completions, so a fact with an unknown exact day always passes an `as_of`
    or `event_before` check for any cutoff at or after that partial period's
    start. `created_at`
    is the full `"YYYY-MM-DD HH:MM:SS"` fallback used when `occurred_at` is
    NULL or empty -- callers must pass these bounds in a directly comparable
    shape (matching separator/precision) or same-day boundary comparisons will
    behave unexpectedly. This is a deliberate, documented approximation, not
    a bug.
    """
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim embedding; got {len(embedding)}.")
    normalized = _normalize_embedding(embedding)

    # sqlite-vec returns the nearest `fetch_k` rows from the embedding table
    # *before* the retired filter is applied, so a retired fact nearer than
    # live ones would otherwise steal a slot. Over-fetch, then keep the k
    # nearest live neighbors.
    fetch_k = max(k * 4, k + 10)

    date_clause = ""
    params: list[object] = [_serialize_float32(normalized), fetch_k, agent_id]
    if as_of is not None:
        date_clause += " AND COALESCE(NULLIF(l.occurred_at, ''), l.created_at) <= ?"
        params.append(as_of)
    if event_after is not None:
        date_clause += " AND COALESCE(NULLIF(l.occurred_at, ''), l.created_at) >= ?"
        params.append(event_after)
    if event_before is not None:
        date_clause += " AND COALESCE(NULLIF(l.occurred_at, ''), l.created_at) <= ?"
        params.append(event_before)
    params.append(k)

    init_vector_memory(db_path)
    with closing(connect(db_path)) as conn:
        _ensure_vector_memory_schema(conn)
        backend = select_vector_backend(conn)
        knn = backend.knn_subquery(
            table="long_term_memory_embeddings", id_column="fact_id"
        )
        rows = conn.execute(
            f"""
            SELECT e._id, l.fact_text, e._distance, l.confidence
            FROM ({knn}) AS e
            JOIN long_term_memory AS l ON l.id = e._id
            WHERE l.retired = 0
                AND l.agent_id IS ?
                {date_clause}
            ORDER BY e._distance
            LIMIT ?
            """,
            params,
        ).fetchall()

    return [
        LongTermNeighbor(
            fact_id=int(row[0]),
            fact_text=str(row[1]),
            similarity=backend.similarity(float(row[2])),
            confidence=float(row[3]),
        )
        for row in rows
    ]


def long_term_fact_exists_for_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
) -> bool:
    # Source of truth for "this candidate already has a durable fact": the
    # UNIQUE-indexed promoted_from_candidate_id column. The promotion module uses
    # this to tell a benign already-promoted candidate (has a fact -> skip) from
    # a corrupt promoted flag with no fact (-> fail loud).
    row = conn.execute(
        "SELECT 1 FROM long_term_memory WHERE promoted_from_candidate_id = ? LIMIT 1",
        (candidate_id,),
    ).fetchone()
    return row is not None


def insert_long_term_fact(
    conn: sqlite3.Connection,
    *,
    fact_text: str,
    subject: str,
    category: str,
    importance: int,
    confidence: float,
    source_message_ids: list[int],
    agent_id: str | None,
    promoted_from_candidate_id: int,
    retrieved_count: int,
    used_count: int,
    editable: bool,
    embedding: list[float],
    occurred_at: str | None = None,
) -> int:
    # Conn-scoped Tier 3 write used by the promotion transaction. Inserts the
    # durable fact and its embedding in the caller's connection so the whole
    # promotion stays atomic. The UNIQUE(promoted_from_candidate_id) index is the
    # schema backstop that turns a racy double-claim into an IntegrityError here.
    cursor = conn.execute(
        """
        INSERT INTO long_term_memory
            (fact_text, subject, category, importance, confidence,
             source_message_ids, agent_id, promoted_from_candidate_id,
             retrieved_count, used_count, editable, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact_text,
            subject,
            category,
            importance,
            confidence,
            json.dumps(source_message_ids),
            agent_id,
            promoted_from_candidate_id,
            retrieved_count,
            used_count,
            editable,
            occurred_at,
        ),
    )
    fact_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO long_term_memory_embeddings (fact_id, embedding)
        VALUES (?, ?)
        """,
        (fact_id, _serialize_float32(_normalize_embedding(embedding))),
    )
    return fact_id


def retire_long_term_fact(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    superseded_by_fact_id: int | None,
    agent_id: str | None,
) -> bool:
    # `AND retired = 0` makes this idempotent: if two promotions in one cycle
    # both target the same neighbor, only the first retire counts and keeps the
    # retired_by_fact_id link. Returns True iff this call performed the retire.
    cursor = conn.execute(
        """
        UPDATE long_term_memory
        SET retired = 1,
            retired_at = CURRENT_TIMESTAMP,
            retired_by_fact_id = ?
        WHERE id = ?
            AND agent_id IS ?
            AND retired = 0
        """,
        (superseded_by_fact_id, fact_id, agent_id),
    )
    return cursor.rowcount > 0
