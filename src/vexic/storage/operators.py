import sqlite3
import os
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from vexic.embeddings import EMBEDDING_DIM
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage.schema import (
    CANONICAL_TABLES,
    _ensure_vector_memory_schema,
    _normalize_embedding,
    _serialize_float32,
    init_db,
)
from vexic.ports import ContentCodec
from vexic.storage.transcript import _rebuild_messages_fts
from vexic.storage.connection import connect, rows_as_dicts
from vexic.storage.errors import is_operational_error


@dataclass(frozen=True)
class MemoryProjectionRepairReport:
    messages_fts_rows: int
    candidate_fts_rows: int = 0
    long_term_fts_rows: int = 0
    candidate_counters_recomputed: int = 0
    long_term_counters_recomputed: int = 0
    candidate_embeddings_repaired: int = 0
    long_term_embeddings_repaired: int = 0


@dataclass(frozen=True)
class MemoryExportReport:
    output_path: Path
    rows_exported: int
    bytes_written: int


@dataclass(frozen=True)
class MemoryRebuildCopyReport:
    output_path: Path
    repair_report: MemoryProjectionRepairReport


def _bool_text(value: object) -> str:
    return "true" if bool(value) else "false"


def _rebuild_external_content_fts(
    conn: sqlite3.Connection,
    table_name: str,
) -> int:
    conn.execute(f"INSERT INTO {table_name}({table_name}) VALUES ('rebuild')")
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _recompute_candidate_counters(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        UPDATE memory_candidates
        SET retrieved_count = (
                SELECT COUNT(*)
                FROM candidate_retrieval_events
                WHERE candidate_retrieval_events.candidate_id = memory_candidates.id
            ),
            used_count = (
                SELECT COUNT(*)
                FROM candidate_retrieval_events
                WHERE candidate_retrieval_events.candidate_id = memory_candidates.id
                    AND candidate_retrieval_events.used = 1
            )
        """
    )
    return int(conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0])


def _recompute_long_term_counters(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        UPDATE long_term_memory
        SET retrieved_count = (
                SELECT COUNT(*)
                FROM retrieval_events
                WHERE retrieval_events.fact_id = long_term_memory.id
            ),
            used_count = (
                SELECT COUNT(*)
                FROM retrieval_events
                WHERE retrieval_events.fact_id = long_term_memory.id
                    AND retrieval_events.used = 1
            )
        """
    )
    return int(conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0])


def _replace_embedding_rows(
    conn: sqlite3.Connection,
    table_name: str,
    id_column: str,
    embeddings: Mapping[int, list[float]],
) -> int:
    for row_id, embedding in embeddings.items():
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(f"Expected {EMBEDDING_DIM}-dim embedding; got {len(embedding)}.")
        conn.execute(
            f"DELETE FROM {table_name} WHERE {id_column} = ?",
            (row_id,),
        )
        conn.execute(
            f"""
            INSERT INTO {table_name} ({id_column}, embedding)
            VALUES (?, ?)
            """,
            (row_id, _serialize_float32(_normalize_embedding(embedding))),
        )
    return len(embeddings)


def _guard_rebuildable_projection_text(
    conn: sqlite3.Connection,
    forbidden_secret_values: Iterable[str],
) -> None:
    for table_name, column_name in (
        ("messages", "message_json"),
        ("memory_candidates", "fact_text"),
        ("long_term_memory", "fact_text"),
    ):
        for row in conn.execute(f"SELECT {column_name} FROM {table_name}").fetchall():
            assert_no_forbidden_secret_values(forbidden_secret_values, str(row[0]))


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace("\"", "\"\"")}"'


# The file-copy secret scan covers every column of every table regardless of its
# declared type. SQLite is dynamically typed, so a declared type does not
# constrain what a column stores, and host-owned extension tables carry column
# types Vexic does not control -- a declared-type filter would let a secret in a
# `payload STRING` or `payload BLOB` column ride out in the copy, which
# Invariant 9 forbids.
#
# These three columns are the one exemption: they hold JSON arrays of integer
# fact ids, never free text, so scanning them can only produce spurious
# digit-substring matches on a recovery path an operator runs mid-incident.
_FILE_COPY_TEXT_GUARD_SKIPPED_COLUMNS = frozenset(
    {
        ("retrieval_events", "keyword_fact_ids"),
        ("retrieval_events", "vector_fact_ids"),
        ("retrieval_events", "fused_fact_ids"),
    }
)


def _scan_text(value: object) -> str:
    """Coerce any stored SQLite value to text for the forbidden-value scan.

    Bytes are decoded rather than repr'd so a secret stored as a BLOB is matched
    the same way it would be as TEXT; undecodable bytes become replacement
    characters and cannot mask an ASCII secret.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _guard_database_text_for_file_copy(
    conn: sqlite3.Connection,
    forbidden_secret_values: Iterable[str],
) -> None:
    forbidden_values = tuple(forbidden_secret_values)
    table_rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
            AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    for table_row in table_rows:
        table_name = str(table_row[0])
        try:
            column_rows = conn.execute(
                f"PRAGMA table_info({_quote_identifier(table_name)})"
            ).fetchall()
        except (sqlite3.OperationalError, ValueError) as exc:
            # FTS5/vec virtual tables whose module is unavailable raise "no such
            # module" -- a sqlite3.OperationalError locally, a bare ValueError on
            # hosted libSQL (ADR 0019). Skip those; re-raise anything else,
            # including unrelated ValueErrors.
            if not is_operational_error(exc):
                raise
            if "no such module" in str(exc):
                continue
            raise

        for column_row in column_rows:
            column_name = str(column_row[1])
            if (table_name, column_name) in _FILE_COPY_TEXT_GUARD_SKIPPED_COLUMNS:
                continue
            try:
                rows = conn.execute(
                    "SELECT "
                    f"{_quote_identifier(column_name)} "
                    "FROM "
                    f"{_quote_identifier(table_name)} "
                    "WHERE "
                    f"{_quote_identifier(column_name)} IS NOT NULL"
                ).fetchall()
            except (sqlite3.OperationalError, ValueError) as exc:
                # Same virtual-table "no such module" case as above; a
                # sqlite3.OperationalError locally, a bare ValueError on hosted
                # libSQL (ADR 0019). Re-raise anything else.
                if not is_operational_error(exc):
                    raise
                if "no such module" in str(exc):
                    continue
                raise
            for row in rows:
                assert_no_forbidden_secret_values(forbidden_values, _scan_text(row[0]))


# `memory_dedup_events` is excluded deliberately: it belongs to the vector
# schema (`init_vector_memory`), so a transcript-only database legitimately
# lacks it, and the projection repair below never recreates it -- it cannot be
# silently substituted, which is the failure this check exists to catch.
_REQUIRED_SOURCE_TABLES = tuple(
    table for table in CANONICAL_TABLES if table != "memory_dedup_events"
)


def _assert_source_is_complete(conn: sqlite3.Connection) -> None:
    """Reject a source database that is missing a canonical table.

    The rebuild copy runs ``init_db`` on the *copy*, which is how rebuildable
    projections get repaired. Applied to a half-restored source that is missing,
    say, ``long_term_memory``, that same step would create an empty replacement
    and report success -- the operator would be told the rebuild worked while
    Tier 3 was silently gone. Presence, not row count, is the test: a freshly
    initialized database with every canonical table and zero rows is valid input.
    """
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = [table for table in _REQUIRED_SOURCE_TABLES if table not in present]
    if missing:
        raise ValueError(
            "Refusing to rebuild from an incomplete source database; missing "
            f"canonical tables: {', '.join(missing)}."
        )


def repair_memory_projections(
    db_path: str,
    *,
    candidate_embeddings: Mapping[int, list[float]] | None = None,
    long_term_embeddings: Mapping[int, list[float]] | None = None,
    forbidden_secret_values: Iterable[str] = (),
    content_codec: "ContentCodec | None" = None,
) -> MemoryProjectionRepairReport:
    """Repair rebuildable memory projections without mutating the Transcript."""
    init_db(db_path)
    with closing(connect(db_path)) as conn:
        with conn:
            candidate_embeddings = candidate_embeddings or {}
            long_term_embeddings = long_term_embeddings or {}
            if candidate_embeddings or long_term_embeddings:
                _ensure_vector_memory_schema(conn)
            _guard_rebuildable_projection_text(conn, forbidden_secret_values)

            _rebuild_messages_fts(conn, content_codec)
            messages_fts_rows = int(
                conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            )
            candidate_fts_rows = _rebuild_external_content_fts(
                conn,
                "memory_candidates_fts",
            )
            long_term_fts_rows = _rebuild_external_content_fts(
                conn,
                "long_term_memory_fts",
            )
            candidate_counters_recomputed = _recompute_candidate_counters(conn)
            long_term_counters_recomputed = _recompute_long_term_counters(conn)
            candidate_embeddings_repaired = _replace_embedding_rows(
                conn,
                "memory_candidate_embeddings",
                "candidate_id",
                candidate_embeddings,
            )
            long_term_embeddings_repaired = _replace_embedding_rows(
                conn,
                "long_term_memory_embeddings",
                "fact_id",
                long_term_embeddings,
            )

    return MemoryProjectionRepairReport(
        messages_fts_rows=messages_fts_rows,
        candidate_fts_rows=candidate_fts_rows,
        long_term_fts_rows=long_term_fts_rows,
        candidate_counters_recomputed=candidate_counters_recomputed,
        long_term_counters_recomputed=long_term_counters_recomputed,
        candidate_embeddings_repaired=candidate_embeddings_repaired,
        long_term_embeddings_repaired=long_term_embeddings_repaired,
    )


def _render_memory_review_markdown(conn: sqlite3.Connection) -> tuple[str, int]:
    lines = ["# Memory Review Export", "", "## Candidates"]
    rows_exported = 0

    candidate_rows = rows_as_dicts(conn.execute(
        """
        SELECT c.id, c.fact_text, c.subject, c.category, c.importance,
               c.confidence, c.source_message_ids, c.hit_count,
               c.retrieved_count, c.used_count, c.rem_boost, c.promoted,
               c.promoted_fact_id, c.retired, c.retired_at,
               c.retired_by_fact_id, c.stale, c.needs_review, c.editable,
               l.label AS promotion_label, l.reason AS promotion_reason,
               COALESCE(e.cnt, 0) AS candidate_retrieval_event_count
        FROM memory_candidates AS c
        LEFT JOIN promotion_labels AS l ON l.candidate_id = c.id
        LEFT JOIN (
            SELECT candidate_id, COUNT(*) AS cnt
            FROM candidate_retrieval_events
            GROUP BY candidate_id
        ) AS e ON e.candidate_id = c.id
        ORDER BY c.id
        """
    ))
    for row in candidate_rows:
        rows_exported += 1
        if row["promotion_label"] is not None:
            rows_exported += 1
        lines.extend([
            "",
            f"### Candidate {row['id']}",
            f"- fact_text: {row['fact_text']}",
            f"- subject: {row['subject']}",
            f"- category: {row['category']}",
            f"- importance: {row['importance']}",
            f"- confidence: {row['confidence']}",
            f"- source_message_ids: {row['source_message_ids']}",
            f"- hit_count: {row['hit_count']}",
            f"- retrieved_count: {row['retrieved_count']}",
            f"- used_count: {row['used_count']}",
            f"- rem_boost: {row['rem_boost']}",
            f"- promoted: {_bool_text(row['promoted'])}",
            f"- promoted_fact_id: {row['promoted_fact_id']}",
            f"- retired: {_bool_text(row['retired'])}",
            f"- retired_at: {row['retired_at']}",
            f"- retired_by_fact_id: {row['retired_by_fact_id']}",
            f"- stale: {_bool_text(row['stale'])}",
            f"- needs_review: {_bool_text(row['needs_review'])}",
            f"- editable: {_bool_text(row['editable'])}",
            f"- candidate_retrieval_events: {row['candidate_retrieval_event_count']}",
        ])
        if row["promotion_label"] is not None:
            lines.extend([
                f"- promotion_label: {row['promotion_label']}",
                f"- promotion_reason: {row['promotion_reason']}",
            ])

    lines.extend(["", "## Long-term Facts"])
    fact_rows = rows_as_dicts(conn.execute(
        """
        SELECT id, fact_text, subject, category, importance, confidence,
               source_message_ids, promoted_from_candidate_id, retrieved_count,
               used_count, retired, retired_at, retired_by_fact_id, editable,
               COALESCE(e.cnt, 0) AS retrieval_event_count
        FROM long_term_memory AS f
        LEFT JOIN (
            SELECT fact_id, COUNT(*) AS cnt
            FROM retrieval_events
            GROUP BY fact_id
        ) AS e ON e.fact_id = f.id
        ORDER BY f.id
        """
    ))
    for row in fact_rows:
        rows_exported += 1
        lines.extend([
            "",
            f"### Long-term fact {row['id']}",
            f"- fact_text: {row['fact_text']}",
            f"- subject: {row['subject']}",
            f"- category: {row['category']}",
            f"- importance: {row['importance']}",
            f"- confidence: {row['confidence']}",
            f"- source_message_ids: {row['source_message_ids']}",
            f"- promoted_from_candidate_id: {row['promoted_from_candidate_id']}",
            f"- retrieved_count: {row['retrieved_count']}",
            f"- used_count: {row['used_count']}",
            f"- retired: {_bool_text(row['retired'])}",
            f"- retired_at: {row['retired_at']}",
            f"- retired_by_fact_id: {row['retired_by_fact_id']}",
            f"- editable: {_bool_text(row['editable'])}",
            f"- retrieval_events: {row['retrieval_event_count']}",
        ])

    return "\n".join(lines) + "\n", rows_exported


def export_memory_review(
    db_path: str,
    output_path: str | os.PathLike[str],
    *,
    forbidden_secret_values: Iterable[str] = (),
    overwrite: bool = False,
) -> MemoryExportReport:
    init_db(db_path)
    target = Path(output_path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing memory export: {target}")

    with closing(connect(db_path)) as conn:
        rendered, rows_exported = _render_memory_review_markdown(conn)

    assert_no_forbidden_secret_values(forbidden_secret_values, rendered)

    encoded = rendered.encode()
    temp_path = target.with_name(f".{target.name}.tmp")
    try:
        temp_path.write_bytes(encoded)
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    return MemoryExportReport(
        output_path=target,
        rows_exported=rows_exported,
        bytes_written=len(encoded),
    )


def create_memory_rebuild_copy(
    db_path: str,
    output_path: str | os.PathLike[str],
    *,
    forbidden_secret_values: Iterable[str] = (),
) -> MemoryRebuildCopyReport:
    target = Path(output_path)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing rebuild copy: {target}")

    forbidden_values = tuple(forbidden_secret_values)
    with closing(connect(db_path)) as conn:
        _assert_source_is_complete(conn)
        _guard_database_text_for_file_copy(conn, forbidden_values)
        try:
            conn.execute("VACUUM INTO ?", (str(target),))
        except Exception:
            target.unlink(missing_ok=True)
            raise

    try:
        repair_report = repair_memory_projections(
            str(target),
            forbidden_secret_values=forbidden_values,
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise

    return MemoryRebuildCopyReport(
        output_path=target,
        repair_report=repair_report,
    )
