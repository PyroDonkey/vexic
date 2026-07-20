import json
import re
import sqlite3
import struct
import threading
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from vexic.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL_NAME

if TYPE_CHECKING:
    from vexic.ports import ContentCodec

# Back-compat re-export: storage-internal callers keep importing the persistence
# secret guard from here under the old private name.
from vexic.redaction import assert_no_forbidden_secret_values as _assert_no_forbidden_secret_values
from vexic.storage.connection import connect
from vexic.storage.errors import is_duplicate_column_error

# Process-level init-once memo: DDL against a hosted libSQL/Turso
# target incurs per-request remote round-trips, so re-running the full
# init_db/init_vector_memory body on every storage call is a latency bug there
# (harmless-but-wasteful on local sqlite). Keyed on target identity only -- an
# auth token rotation is not a schema change -- and populated only after the
# guarded DDL transaction commits, so a failed init never poisons the memo.
_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


def _memo_key(db_path: "str | StorageTarget") -> str:
    from vexic.storage.connection import StorageTarget

    return db_path.target if isinstance(db_path, StorageTarget) else str(db_path)


def _reset_init_memo() -> None:  # test hook
    with _INIT_LOCK:
        _INITIALIZED.clear()

# Shared spine for the three memory tiers. Owns the connection seam (WAL,
# schema creation, vec-extension load), the embedding-blob math reused across
# tiers, and the persistence secret guard. Tier modules (transcript, candidates,
# longterm) and the cross-tier promotion module import from here; this module
# never imports them at module load (init_db reaches transcript lazily to build
# the Tier-1 FTS shadow without creating an import cycle).

DreamStatus = Literal["ok", "error", "partial"]
EMBEDDING_DISTANCE_METRIC = "l2"

CATEGORY_CHECK = (
    "category IN ('preference', 'fact', 'goal', 'event', "
    "'relationship', 'skill', 'constraint', 'context')"
)


def _fts_match_query(query: str) -> str | None:
    # Shared FTS5 MATCH sanitizer: free-text queries may contain FTS operators
    # (AND, NEAR, unbalanced quotes), so each token is quoted into a bare-word
    # phrase. None means "nothing searchable" — callers return no rows.
    # Tokens are ORed: every keyword leg (transcript, Tier 2, Tier 3) is
    # recall-oriented, and bm25 rank ordering surfaces messages matching more
    # tokens first. All-tokens-must-match AND semantics were retired by
    # ADR 0036 — one dead token must not empty a recall surface.
    tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [row[1] for row in rows]


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    # Two hosted containers migrating the same tenant database (rolling deploy
    # overlap) can both observe the column missing before either ALTER commits.
    # The loser's ALTER then fails with ``duplicate column name`` -- the schema
    # is already in the desired state, so that error is swallowed; anything
    # else propagates. The hosted libSQL driver reports it as a bare ValueError
    # rather than sqlite3.OperationalError (ADR 0019).
    if column_name in _table_columns(conn, table_name):
        return
    try:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}")
    except (sqlite3.OperationalError, ValueError) as exc:
        if not is_duplicate_column_error(exc):
            raise


def _earliest_date_from_timestamps(values: Iterable[object]) -> str | None:
    # Shared parse core for mentioned_at derivation (ADR 0037). Fail-soft:
    # messages.timestamp is stored host-supplied and unvalidated, so blank or
    # unparseable values are skipped rather than raised on — a raise here would
    # abort a Light batch or brick init_db via the ensure backfill.
    earliest: datetime | None = None
    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                # astimezone can raise OverflowError for parseable boundary
                # values like "0001-01-01T00:00:00+23:59" — fail-soft covers
                # the whole normalization, not just parsing.
                parsed = parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            continue
        if earliest is None or parsed < earliest:
            earliest = parsed
    if earliest is None:
        return None
    return earliest.date().isoformat()


def _earliest_mention_date(
    conn: sqlite3.Connection,
    source_message_ids: Iterable[int],
) -> str | None:
    # mentioned_at derivation (ADR 0037): the earliest UTC calendar date of the
    # cited Tier 1 messages, as a date-only ISO string. Deterministic provenance
    # — a pure function of source_message_ids over the append-only messages
    # table — so insert, merge, and backfill all reproduce the same value.
    ids = sorted({int(value) for value in source_message_ids})
    if not ids:
        return None
    # Ids ride a single JSON parameter (the purge.py json_each precedent) so
    # an IN (?, ?, ...) clause can never blow SQLITE_MAX_VARIABLE_NUMBER on a
    # row citing many messages.
    rows = conn.execute(
        "SELECT timestamp FROM messages WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    ).fetchall()
    return _earliest_date_from_timestamps(row[0] for row in rows)


def _backfill_mentioned_at(conn: sqlite3.Connection, table_name: str) -> None:
    # Heal pre-column rows (the eval-DB undated-event sink) on init, following
    # the last_seen_at ensure-backfill precedent. Batched: one read of the
    # legacy rows, one read of every cited message, one executemany write —
    # not a per-row round trip, which matters against hosted libSQL/Turso.
    # Rows whose sources are missing/unparseable derive None and stay NULL;
    # they are rescanned on later inits, a benign no-op at this row count.
    rows = conn.execute(
        f"SELECT id, source_message_ids FROM {table_name} WHERE mentioned_at IS NULL"
    ).fetchall()
    if not rows:
        return
    parsed_rows: list[tuple[int, list[int]]] = []
    cited: set[int] = set()
    for row_id, ids_json in rows:
        try:
            loaded = json.loads(ids_json)
            ids = [int(value) for value in loaded]
        except (TypeError, ValueError):
            continue
        parsed_rows.append((int(row_id), ids))
        cited.update(ids)
    if not cited:
        return
    # Single JSON parameter, not one `?` per id — see _earliest_mention_date.
    timestamp_by_id = dict(
        conn.execute(
            "SELECT id, timestamp FROM messages WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(cited)),),
        ).fetchall()
    )
    updates = []
    for row_id, ids in parsed_rows:
        derived = _earliest_date_from_timestamps(
            timestamp_by_id.get(message_id) for message_id in ids
        )
        if derived is not None:
            updates.append((derived, row_id))
    if updates:
        # Conditional on the row still being NULL: on hosted libSQL each
        # statement auto-commits, so a concurrent merge can land a fresher
        # union-derived value between the snapshot above and this write. The
        # merge value is computed over a superset of sources and always wins.
        conn.executemany(
            f"UPDATE {table_name} SET mentioned_at = ? "
            "WHERE id = ? AND mentioned_at IS NULL",
            updates,
        )


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def _serialize_float32(embedding: list[float]) -> bytes:
    from sqlite_vec import serialize_float32

    return serialize_float32(embedding)


def _normalize_embedding(embedding: list[float]) -> list[float]:
    magnitude = sum(value * value for value in embedding) ** 0.5
    if magnitude == 0:
        raise ValueError("Embedding magnitude must be greater than zero.")
    return [value / magnitude for value in embedding]


def _embedding_blob_to_list(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{EMBEDDING_DIM}f", blob))


def _similarity_from_distance(distance: float) -> float:
    similarity = 1.0 - ((distance * distance) / 2.0)
    return max(-1.0, min(1.0, similarity))


def _ensure_memory_candidate_columns(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "memory_candidates")
    if not columns:
        return

    _ensure_column(conn, "memory_candidates", "agent_id", "agent_id TEXT")
    _ensure_column(conn, "memory_candidates", "hit_count", "hit_count INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "memory_candidates", "retrieved_count", "retrieved_count INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "memory_candidates", "used_count", "used_count INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "memory_candidates", "rem_boost", "rem_boost REAL NOT NULL DEFAULT 0.0")
    _ensure_column(conn, "memory_candidates", "last_seen_at", "last_seen_at DATETIME")
    _ensure_column(conn, "memory_candidates", "promoted", "promoted BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(conn, "memory_candidates", "promoted_fact_id", "promoted_fact_id INTEGER")
    _ensure_column(conn, "memory_candidates", "retired", "retired BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(conn, "memory_candidates", "retired_at", "retired_at DATETIME")
    _ensure_column(conn, "memory_candidates", "retired_by_fact_id", "retired_by_fact_id INTEGER")
    _ensure_column(conn, "memory_candidates", "stale", "stale BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(conn, "memory_candidates", "needs_review", "needs_review BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(conn, "memory_candidates", "review_neighbor_id", "review_neighbor_id INTEGER")
    _ensure_column(conn, "memory_candidates", "best_similarity", "best_similarity REAL")
    # TEXT, not DATETIME: DATETIME has NUMERIC column affinity in SQLite, which
    # silently coerces a well-formed-looking value like "2025" (a valid partial-
    # precision ISO string) into an INTEGER on write. occurred_at must round-trip
    # as the exact string the extractor produced, at whatever precision it knew.
    _ensure_column(conn, "memory_candidates", "occurred_at", "occurred_at TEXT")
    # TEXT for the same affinity reason; date-only provenance string (ADR 0037).
    _ensure_column(conn, "memory_candidates", "mentioned_at", "mentioned_at TEXT")

    conn.execute(
        """
        UPDATE memory_candidates
        SET last_seen_at = created_at
        WHERE last_seen_at IS NULL
        """
    )
    _backfill_mentioned_at(conn, "memory_candidates")


def _ensure_embedding_metadata(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_metadata (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            model_name TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            distance_metric TEXT NOT NULL
        )
        """
    )
    row = conn.execute(
        """
        SELECT model_name, dimension, distance_metric
        FROM embedding_metadata
        WHERE id = 1
        """
    ).fetchone()

    expected = (EMBEDDING_MODEL_NAME, EMBEDDING_DIM, EMBEDDING_DISTANCE_METRIC)
    if row is None:
        conn.execute(
            """
            INSERT INTO embedding_metadata
                (id, model_name, dimension, distance_metric)
            VALUES (1, ?, ?, ?)
            """,
            expected,
        )
    elif tuple(row) != expected:
        raise ValueError(
            "Embedding metadata mismatch. Rebuild memory_candidate_embeddings before "
            f"using vector dedup. Expected {expected}, got {tuple(row)}."
        )


def _ensure_dedup_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_dedup_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            candidate_id INTEGER NOT NULL,
            matched_candidate_id INTEGER,
            best_similarity REAL,
            decision TEXT NOT NULL CHECK (decision IN ('insert', 'merge', 'review')),
            incoming_fact_text TEXT NOT NULL,
            incoming_source_message_ids TEXT NOT NULL
        )
        """
    )
    _ensure_column(conn, "memory_dedup_events", "incoming_fact_text", "incoming_fact_text TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "memory_dedup_events", "incoming_source_message_ids", "incoming_source_message_ids TEXT NOT NULL DEFAULT '[]'")


def _ensure_long_term_memory(conn: sqlite3.Connection) -> None:
    # Tier 3 durable fact store. Base table + FTS5 shadow live here (no vec
    # extension required) so the core transcript path stays usable even when
    # sqlite-vec is unavailable, mirroring the candidate base table.
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_text TEXT NOT NULL,
            subject TEXT NOT NULL,
            category TEXT NOT NULL CHECK ({CATEGORY_CHECK}),
            importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
            confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
            source_message_ids TEXT NOT NULL,
            agent_id TEXT,
            promoted_from_candidate_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            retrieved_count INTEGER NOT NULL DEFAULT 0,
            used_count INTEGER NOT NULL DEFAULT 0,
            retired BOOLEAN NOT NULL DEFAULT 0,
            retired_at DATETIME,
            retired_by_fact_id INTEGER,
            editable BOOLEAN NOT NULL DEFAULT 1
        )
        """
    )
    _ensure_column(conn, "long_term_memory", "agent_id", "agent_id TEXT")
    # TEXT, not DATETIME: see the matching comment on the memory_candidates
    # occurred_at column — NUMERIC affinity would mangle a bare-year string.
    _ensure_column(conn, "long_term_memory", "occurred_at", "occurred_at TEXT")
    # Date-only provenance string, same TEXT-affinity reasoning (ADR 0037).
    _ensure_column(conn, "long_term_memory", "mentioned_at", "mentioned_at TEXT")
    _backfill_mentioned_at(conn, "long_term_memory")
    # Idempotency backstop for promotion: at most one Tier 3 fact per source
    # candidate. The atomic claim in _promote_candidate is the primary guard;
    # this UNIQUE index is the schema-level safety net so even a racy double
    # claim cannot land two durable facts for the same candidate.
    #
    # The guard is partial over live (non-retired) rows: at most one *active*
    # Tier 3 fact per candidate. Live promotion only ever writes retired=0 rows
    # and never re-promotes a candidate, so a racy double-claim is still caught,
    # while a retired duplicate can legitimately coexist with its live successor.
    #
    # Migration preflight: a DB written before this index existed could already
    # hold duplicate rows from the historical double-promote race. Creating the
    # UNIQUE index over such data raises a bare IntegrityError mid-init, bricking
    # startup with no clue. Detect live duplicates first and fail loud with a
    # recoverable message instead. Because the index is scoped to retired=0,
    # retiring the redundant duplicate in place (a lossless UPDATE, no DELETE)
    # actually resolves the conflict before re-running.
    index_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_long_term_memory_promoted_from'"
    ).fetchone()
    if index_exists is None:
        duplicate_ids = conn.execute(
            """
            SELECT promoted_from_candidate_id
            FROM long_term_memory
            WHERE retired = 0
            GROUP BY promoted_from_candidate_id
            HAVING COUNT(*) > 1
            ORDER BY promoted_from_candidate_id
            """
        ).fetchall()
        if duplicate_ids:
            ids = ", ".join(str(row[0]) for row in duplicate_ids)
            raise ValueError(
                "Cannot enforce one live Tier 3 fact per candidate: long_term_memory still "
                f"holds duplicate active rows for promoted_from_candidate_id(s): {ids}. This is "
                "a pre-existing double-promotion from before the promotion race fix. Retire the "
                "redundant fact rows in place (set retired = 1; keep all rows, the history is "
                "lossless) so one live row remains per candidate, then re-run."
            )
        conn.execute(
            """
            CREATE UNIQUE INDEX idx_long_term_memory_promoted_from
            ON long_term_memory(promoted_from_candidate_id)
            WHERE retired = 0
            """
        )
    # External-content FTS5 shadow over fact_text, kept in sync by triggers.
    # Retirement is an UPDATE (fact_text unchanged), but the update trigger
    # keeps the index consistent regardless.
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS long_term_memory_fts
        USING fts5(fact_text, content='long_term_memory', content_rowid='id')
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS long_term_memory_ai
        AFTER INSERT ON long_term_memory BEGIN
            INSERT INTO long_term_memory_fts (rowid, fact_text)
            VALUES (new.id, new.fact_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS long_term_memory_ad
        AFTER DELETE ON long_term_memory BEGIN
            INSERT INTO long_term_memory_fts (long_term_memory_fts, rowid, fact_text)
            VALUES ('delete', old.id, old.fact_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS long_term_memory_au
        AFTER UPDATE ON long_term_memory BEGIN
            INSERT INTO long_term_memory_fts (long_term_memory_fts, rowid, fact_text)
            VALUES ('delete', old.id, old.fact_text);
            INSERT INTO long_term_memory_fts (rowid, fact_text)
            VALUES (new.id, new.fact_text);
        END
        """
    )


def _ensure_retrieval_events(conn: sqlite3.Connection) -> None:
    # One row per fact surfaced by one Tier 3 retrieval. The durable source that
    # makes retrieved_count/used_count derivable and rebuild-safe.
    # `used` is tri-state: NULL = use judge never ran, 0 = judged not used,
    # 1 = judged used — the only mutation a row ever receives is that judgment.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            agent_id TEXT,
            query TEXT NOT NULL,
            rewritten_query TEXT,
            keyword_fact_ids TEXT NOT NULL DEFAULT '[]',
            vector_fact_ids TEXT NOT NULL DEFAULT '[]',
            fused_fact_ids TEXT NOT NULL DEFAULT '[]',
            retrieved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            used INTEGER CHECK (used IN (0, 1)),
            judged_at DATETIME
        )
        """
    )
    _ensure_column(conn, "retrieval_events", "agent_id", "agent_id TEXT")
    _ensure_column(conn, "retrieval_events", "rewritten_query", "rewritten_query TEXT")
    _ensure_column(
        conn,
        "retrieval_events",
        "keyword_fact_ids",
        "keyword_fact_ids TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        conn,
        "retrieval_events",
        "vector_fact_ids",
        "vector_fact_ids TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        conn,
        "retrieval_events",
        "fused_fact_ids",
        "fused_fact_ids TEXT NOT NULL DEFAULT '[]'",
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_retrieval_events_fact
        ON retrieval_events(fact_id, retrieved_at)
        """
    )


def _ensure_memory_candidates_fts(conn: sqlite3.Connection) -> None:
    # External-content FTS5 shadow over candidate fact_text for the Tier 2
    # candidate-fallback keyword retriever, mirroring long_term_memory_fts.
    # Light-phase merge updates hit_count/source ids/embedding but not
    # fact_text, so the update trigger rarely fires; it is kept for
    # correctness regardless.
    #
    # Unlike long_term_memory (created together with its shadow), the candidate
    # base table predates this shadow, so existing rows are not covered by the
    # insert trigger. Detect first creation and rebuild from memory_candidates
    # once so pre-existing candidates become searchable.
    shadow_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'memory_candidates_fts'"
    ).fetchone()
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_candidates_fts
        USING fts5(fact_text, content='memory_candidates', content_rowid='id')
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memory_candidates_ai
        AFTER INSERT ON memory_candidates BEGIN
            INSERT INTO memory_candidates_fts (rowid, fact_text)
            VALUES (new.id, new.fact_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memory_candidates_ad
        AFTER DELETE ON memory_candidates BEGIN
            INSERT INTO memory_candidates_fts (memory_candidates_fts, rowid, fact_text)
            VALUES ('delete', old.id, old.fact_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memory_candidates_au
        AFTER UPDATE OF fact_text ON memory_candidates BEGIN
            INSERT INTO memory_candidates_fts (memory_candidates_fts, rowid, fact_text)
            VALUES ('delete', old.id, old.fact_text);
            INSERT INTO memory_candidates_fts (rowid, fact_text)
            VALUES (new.id, new.fact_text);
        END
        """
    )
    if shadow_exists is None:
        conn.execute(
            "INSERT INTO memory_candidates_fts (memory_candidates_fts) VALUES ('rebuild')"
        )


def _ensure_candidate_retrieval_events(conn: sqlite3.Connection) -> None:
    # One row per Tier 2 candidate surfaced by one candidate-fallback retrieval
    # path. Parallel to retrieval_events, never an extension of it: the referent
    # is a mutable candidate, not a durable fact, so the shipped Tier 3 events
    # path stays untouched. `used` is tri-state like retrieval_events (NULL = no
    # candidate use judge ran yet — deferred), reserved for a future verdict.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_retrieval_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            agent_id TEXT,
            query TEXT NOT NULL,
            retrieved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            used INTEGER CHECK (used IN (0, 1)),
            judged_at DATETIME
        )
        """
    )
    _ensure_column(conn, "candidate_retrieval_events", "agent_id", "agent_id TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_retrieval_events_candidate
        ON candidate_retrieval_events(candidate_id, retrieved_at)
        """
    )


def _ensure_scope_tombstones(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scope_tombstones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_tenant_id TEXT NOT NULL,
            target_project_id TEXT,
            target_user_id TEXT,
            target_session_id TEXT,
            target_agent_id TEXT,
            created_by_principal_id TEXT NOT NULL,
            created_by_principal_type TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            retrieval_blocked INTEGER NOT NULL DEFAULT 1 CHECK (retrieval_blocked IN (0, 1)),
            export_blocked INTEGER NOT NULL DEFAULT 1 CHECK (export_blocked IN (0, 1)),
            replay_blocked INTEGER NOT NULL DEFAULT 1 CHECK (replay_blocked IN (0, 1)),
            rebuild_blocked INTEGER NOT NULL DEFAULT 1 CHECK (rebuild_blocked IN (0, 1)),
            physical_purge_deferred INTEGER NOT NULL DEFAULT 1 CHECK (physical_purge_deferred IN (0, 1))
        )
        """
    )
    _ensure_column(conn, "scope_tombstones", "target_agent_id", "target_agent_id TEXT")
    # Purge audit trail (ADR 0022): when the deferred purge actually runs, the
    # tombstone keeps the proof -- timestamp plus per-table deleted counts.
    _ensure_column(conn, "scope_tombstones", "purged_at", "purged_at DATETIME")
    _ensure_column(conn, "scope_tombstones", "purged_counts", "purged_counts TEXT")
    conn.execute("DROP INDEX IF EXISTS idx_scope_tombstones_target")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scope_tombstones_target
        ON scope_tombstones(
            target_tenant_id,
            target_project_id,
            target_user_id,
            target_session_id,
            target_agent_id
        )
        """
    )


def _ensure_promotion_labels(conn: sqlite3.Connection) -> None:
    # Human promote/reject judgments over candidates: the eval corpus
    # that will eventually tune Deep scoring weights. fact_text is snapshotted
    # at label time so the label survives later candidate edits/retirement.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS promotion_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL UNIQUE,
            fact_text TEXT NOT NULL,
            label TEXT NOT NULL CHECK (label IN ('promote', 'reject')),
            reason TEXT,
            labeled_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        DELETE FROM promotion_labels
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM promotion_labels
            GROUP BY candidate_id
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_promotion_labels_candidate_id
        ON promotion_labels(candidate_id)
        """
    )


def _ensure_session_summaries(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            agent_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('leaf', 'condensed')),
            first_message_id INTEGER NOT NULL CHECK (first_message_id >= 1),
            last_message_id INTEGER NOT NULL CHECK (last_message_id >= first_message_id),
            summary_text TEXT NOT NULL,
            token_estimate INTEGER NOT NULL CHECK (token_estimate >= 0),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            replaces_summary_ids TEXT NOT NULL DEFAULT '[]',
            model_requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_micros INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _ensure_column(conn, "session_summaries", "agent_id", "agent_id TEXT")
    conn.execute("DROP INDEX IF EXISTS idx_session_summaries_session_range")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_session_summaries_session_range
        ON session_summaries(session_id, agent_id, first_message_id, last_message_id)
        """
    )


def _ensure_vector_memory_schema(conn: sqlite3.Connection) -> None:
    # Lazy import breaks a module cycle: vectors.py imports this module's shared
    # helpers (_load_vec_extension, _similarity_from_distance) at load time, so
    # the backend dispatch is reached here only at call time.
    from vexic.storage.vectors import select_vector_backend

    backend = select_vector_backend(conn)
    backend.prepare(conn)
    backend.create_embeddings_table(
        conn, table="memory_candidate_embeddings", id_column="candidate_id"
    )
    _ensure_embedding_metadata(conn)
    _ensure_dedup_events(conn)
    backend.create_embeddings_table(
        conn, table="long_term_memory_embeddings", id_column="fact_id"
    )


def _create_source_transcript_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_transcript_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_host TEXT NOT NULL,
            source_session_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            agent_id TEXT,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _source_transcript_ledger_has_legacy_unique(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA index_list(source_transcript_ledger)").fetchall():
        if not row[2] or (len(row) > 4 and row[4]):
            continue
        columns = [
            column[0]
            for column in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (row[1],),
            ).fetchall()
        ]
        if columns == ["source_host", "source_session_id", "source_message_id"]:
            return True
    return False


def _migrate_source_transcript_ledger_unique(conn: sqlite3.Connection) -> None:
    if not _source_transcript_ledger_has_legacy_unique(conn):
        return

    conn.execute("ALTER TABLE source_transcript_ledger RENAME TO source_transcript_ledger_old")
    _create_source_transcript_ledger(conn)
    conn.execute(
        """
        INSERT INTO source_transcript_ledger
            (id, source_host, source_session_id, source_message_id,
             agent_id, message_id, created_at)
        SELECT id, source_host, source_session_id, source_message_id,
               agent_id, message_id, created_at
        FROM source_transcript_ledger_old
        """
    )
    conn.execute("DROP TABLE source_transcript_ledger_old")


def _ensure_source_transcript_ledger(conn: sqlite3.Connection) -> None:
    _create_source_transcript_ledger(conn)
    _ensure_column(conn, "source_transcript_ledger", "agent_id", "agent_id TEXT")
    _migrate_source_transcript_ledger_unique(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_source_transcript_ledger_agent_unique
        ON source_transcript_ledger(source_host, source_session_id, source_message_id, agent_id)
        WHERE agent_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_source_transcript_ledger_shared_unique
        ON source_transcript_ledger(source_host, source_session_id, source_message_id)
        WHERE agent_id IS NULL
        """
    )


def init_db(
    db_path: str,
    *,
    force: bool = False,
    content_codec: "ContentCodec | None" = None,
) -> None:
    # Lazy import breaks the schema<->transcript cycle: transcript imports the
    # secret guard from this module at load time, so the Tier-1 FTS builder is
    # reached here only at call time, once both modules are fully initialized.
    # ``content_codec`` is threaded to the FTS builder so a rebuild against an
    # encoded transcript decodes rows instead of choking on ciphertext; codec-
    # aware callers (the service, the dream pipeline) pass their own codec so
    # whichever call wins the memoized first-init carries it (ADR 0023).
    from vexic.storage.transcript import _ensure_messages_fts

    key = _memo_key(db_path)
    if not force:
        with _INIT_LOCK:
            if key in _INITIALIZED:
                return

    with closing(connect(db_path)) as conn:
        # WAL is a persistent database property: once set it applies to every
        # later connection. It lets readers run alongside a single writer, so a
        # scheduled cron brief and a live chat turn don't contend on memory.db.
        # (Python's sqlite3 already defaults timeout=5.0 for writer waits.)
        # Managed libSQL (ADR 0019) rejects ``PRAGMA journal_mode=WAL`` with a
        # SQL_PARSE_ERROR and manages durability/replication server-side, so the
        # pragma is applied to local SQLite only (verified by the 264c spike).
        if isinstance(conn, sqlite3.Connection):
            conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL DEFAULT 'default',
                    agent_id TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    message_json TEXT NOT NULL
                )
                """
            )
            _ensure_column(conn, "messages", "session_id", "session_id TEXT NOT NULL DEFAULT 'default'")
            _ensure_column(conn, "messages", "agent_id", "agent_id TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_session_id_id
                ON messages(session_id, id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_session_agent_id
                ON messages(session_id, agent_id, id)
                """
            )

            _ensure_messages_fts(conn, content_codec)
            _ensure_source_transcript_ledger(conn)

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_text TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    category TEXT NOT NULL CHECK (category IN (
                        'preference', 'fact', 'goal', 'event',
                        'relationship', 'skill', 'constraint', 'context'
                    )),
                    importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
                    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    source_message_ids TEXT NOT NULL,
                    agent_id TEXT,
                    hit_count INTEGER NOT NULL DEFAULT 1,
                    retrieved_count INTEGER NOT NULL DEFAULT 0,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    rem_boost REAL NOT NULL DEFAULT 0.0,
                    editable BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    promoted BOOLEAN NOT NULL DEFAULT 0,
                    promoted_fact_id INTEGER,
                    retired BOOLEAN NOT NULL DEFAULT 0,
                    retired_at DATETIME,
                    retired_by_fact_id INTEGER,
                    stale BOOLEAN NOT NULL DEFAULT 0,
                    needs_review BOOLEAN NOT NULL DEFAULT 0,
                    review_neighbor_id INTEGER,
                    best_similarity REAL,
                    occurred_at TEXT,
                    mentioned_at TEXT
                )
                """
            )
            _ensure_memory_candidate_columns(conn)
            _ensure_memory_candidates_fts(conn)

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dream_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at DATETIME NOT NULL,
                    finished_at DATETIME,
                    status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'partial')),
                    agent_id TEXT,
                    messages_processed INTEGER NOT NULL DEFAULT 0 CHECK (messages_processed >= 0),
                    candidates_inserted INTEGER NOT NULL DEFAULT 0 CHECK (candidates_inserted >= 0),
                    candidates_merged INTEGER NOT NULL DEFAULT 0 CHECK (candidates_merged >= 0),
                    candidates_review INTEGER NOT NULL DEFAULT 0 CHECK (candidates_review >= 0),
                    last_processed_message_id INTEGER NOT NULL DEFAULT 0 CHECK (last_processed_message_id >= 0),
                    error_detail TEXT
                )
                """
            )
            _ensure_column(conn, "dream_runs", "candidates_merged", "candidates_merged INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "agent_id", "agent_id TEXT")
            _ensure_column(conn, "dream_runs", "candidates_review", "candidates_review INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "candidates_boosted", "candidates_boosted INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "promotions", "promotions INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "retirements", "retirements INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "model_requests", "model_requests INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "input_tokens", "input_tokens INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "output_tokens", "output_tokens INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "total_tokens", "total_tokens INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "estimated_cost_micros", "estimated_cost_micros INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "dream_runs", "candidates_dropped", "candidates_dropped INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dream_runs_agent_status_watermark
                ON dream_runs(agent_id, status, last_processed_message_id)
                """
            )

            _ensure_long_term_memory(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_long_term_memory_agent_active
                ON long_term_memory(agent_id, retired, id)
                """
            )
            _ensure_retrieval_events(conn)
            _ensure_candidate_retrieval_events(conn)
            _ensure_scope_tombstones(conn)
            _ensure_promotion_labels(conn)
            _ensure_session_summaries(conn)

    with _INIT_LOCK:
        _INITIALIZED.add(key)  # only after successful commit


def init_vector_memory(db_path: str, *, force: bool = False) -> None:
    init_db(db_path, force=force)
    vector_key = "vec:" + _memo_key(db_path)
    if not force:
        with _INIT_LOCK:
            if vector_key in _INITIALIZED:
                return

    with closing(connect(db_path)) as conn:
        with conn:
            _ensure_vector_memory_schema(conn)

    with _INIT_LOCK:
        _INITIALIZED.add(vector_key)  # only after successful commit
