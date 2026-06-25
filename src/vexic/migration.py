from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import init_vector_memory
from vexic.storage.operators import MemoryProjectionRepairReport, repair_memory_projections

ARTIFACT_VERSION = "vexic.canonical-migration.v1"

CANONICAL_TABLES = (
    "messages",
    "source_transcript_ledger",
    "memory_candidates",
    "memory_dedup_events",
    "dream_runs",
    "long_term_memory",
    "retrieval_events",
    "candidate_retrieval_events",
    "scope_tombstones",
    "promotion_labels",
    "session_summaries",
)

VEXIC_PROJECTION_TABLES = frozenset(
    {
        "embedding_metadata",
        "messages_fts",
        "messages_fts_config",
        "messages_fts_content",
        "messages_fts_data",
        "messages_fts_docsize",
        "messages_fts_idx",
        "memory_candidates_fts",
        "memory_candidates_fts_config",
        "memory_candidates_fts_data",
        "memory_candidates_fts_docsize",
        "memory_candidates_fts_idx",
        "long_term_memory_fts",
        "long_term_memory_fts_config",
        "long_term_memory_fts_data",
        "long_term_memory_fts_docsize",
        "long_term_memory_fts_idx",
        "memory_candidate_embeddings",
        "memory_candidate_embeddings_chunks",
        "memory_candidate_embeddings_info",
        "memory_candidate_embeddings_rowids",
        "memory_candidate_embeddings_vector_chunks00",
        "long_term_memory_embeddings",
        "long_term_memory_embeddings_chunks",
        "long_term_memory_embeddings_info",
        "long_term_memory_embeddings_rowids",
        "long_term_memory_embeddings_vector_chunks00",
    }
)


@dataclass(frozen=True)
class CanonicalMigrationExportReport:
    artifact_path: Path
    rows_exported: int
    bytes_written: int


@dataclass(frozen=True)
class CanonicalMigrationImportReport:
    rows_imported: int
    repair_report: MemoryProjectionRepairReport


def _iter_payload_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_payload_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _iter_payload_strings(item)


def _rows(conn: sqlite3.Connection, table_name: str) -> list[dict[str, object]]:
    conn.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in conn.execute(f'SELECT * FROM "{table_name}" ORDER BY id ASC')
    ]


def _assert_no_host_owned_tables(conn: sqlite3.Connection) -> None:
    known_tables = frozenset(CANONICAL_TABLES) | VEXIC_PROJECTION_TABLES
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
            AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    unknown = [str(row[0]) for row in rows if str(row[0]) not in known_tables]
    if unknown:
        raise ValueError(
            "Found host-owned extension table(s) without a migration plan: "
            + ", ".join(unknown)
        )


def export_canonical_migration(
    db_path: str,
    artifact_path: str | Path,
    *,
    tenant_id: str,
    project_id: str | None,
    forbidden_secret_values: Iterable[str] = (),
    overwrite: bool = False,
) -> CanonicalMigrationExportReport:
    init_vector_memory(db_path)
    target = Path(artifact_path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite migration artifact: {target}")

    with closing(sqlite3.connect(db_path)) as conn:
        _assert_no_host_owned_tables(conn)
        payload = {
            "artifact_version": ARTIFACT_VERSION,
            "scope": {"tenant_id": tenant_id, "project_id": project_id},
            "tables": {table_name: _rows(conn, table_name) for table_name in CANONICAL_TABLES},
        }

    assert_no_forbidden_secret_values(
        tuple(forbidden_secret_values),
        *_iter_payload_strings(payload),
    )
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode()
    temp_path = target.with_name(f".{target.name}.tmp")
    try:
        temp_path.write_bytes(encoded)
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return CanonicalMigrationExportReport(
        artifact_path=target,
        rows_exported=sum(len(rows) for rows in payload["tables"].values()),
        bytes_written=len(encoded),
    )


def _insert_rows(
    conn: sqlite3.Connection,
    table_name: str,
    rows: list[dict[str, object]],
) -> int:
    if not rows:
        return 0
    columns = list(rows[0])
    column_sql = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    imported = 0
    conn.row_factory = sqlite3.Row
    for row in rows:
        existing = conn.execute(
            f'SELECT {column_sql} FROM "{table_name}" WHERE id = ?',
            (row["id"],),
        ).fetchone()
        if existing is not None:
            if dict(existing) != {column: row[column] for column in columns}:
                raise ValueError(f"Conflicting canonical row in {table_name} id {row['id']}.")
            continue
        conn.execute(
            f'INSERT INTO "{table_name}" ({column_sql}) VALUES ({placeholders})',
            [row[column] for column in columns],
        )
        imported += 1
    return imported


def import_canonical_migration(
    artifact_path: str | Path,
    target_db_path: str,
    *,
    tenant_id: str,
    project_id: str | None,
    forbidden_secret_values: Iterable[str] = (),
) -> CanonicalMigrationImportReport:
    payload = json.loads(Path(artifact_path).read_text())
    if payload.get("artifact_version") != ARTIFACT_VERSION:
        raise ValueError("Unsupported canonical migration artifact version.")
    if payload.get("scope") != {"tenant_id": tenant_id, "project_id": project_id}:
        raise PermissionError("Migration artifact scope does not match hosted operator scope.")
    assert_no_forbidden_secret_values(
        tuple(forbidden_secret_values),
        *_iter_payload_strings(payload),
    )

    init_vector_memory(target_db_path)
    rows_imported = 0
    with closing(sqlite3.connect(target_db_path)) as conn:
        with conn:
            tables = payload["tables"]
            for table_name in CANONICAL_TABLES:
                rows_imported += _insert_rows(conn, table_name, tables[table_name])

    repair_report = repair_memory_projections(
        target_db_path,
        forbidden_secret_values=forbidden_secret_values,
    )
    return CanonicalMigrationImportReport(
        rows_imported=rows_imported,
        repair_report=repair_report,
    )
