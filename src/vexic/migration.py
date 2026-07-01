from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import init_db, init_vector_memory
from vexic.storage.operators import MemoryProjectionRepairReport, repair_memory_projections
from vexic.storage.connection import connect, row_as_dict, rows_as_dicts

ARTIFACT_VERSION = "vexic.canonical-migration.v1"
MIGRATION_METADATA_TABLE = "canonical_migration_imports"

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
VEXIC_OPERATOR_TABLES = frozenset({MIGRATION_METADATA_TABLE})


@dataclass(frozen=True)
class CanonicalMigrationExportReport:
    artifact_path: Path
    rows_exported: int
    bytes_written: int


@dataclass(frozen=True)
class CanonicalMigrationImportReport:
    rows_imported: int
    repair_report: MemoryProjectionRepairReport


class _CanonicalMigrationScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    project_id: str | None


class _CanonicalMigrationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_version: str
    scope: _CanonicalMigrationScope
    tables: dict[str, list[dict[str, object]]]

    @field_validator("tables")
    @classmethod
    def _validate_tables(
        cls,
        tables: dict[str, list[dict[str, object]]],
    ) -> dict[str, list[dict[str, object]]]:
        expected = set(CANONICAL_TABLES)
        actual = set(tables)
        if actual != expected:
            raise ValueError("canonical migration artifact tables are invalid.")
        for table_name, rows in tables.items():
            for row in rows:
                row_id = row.get("id")
                if not isinstance(row_id, int) or isinstance(row_id, bool):
                    raise ValueError(
                        f"canonical migration artifact row in {table_name} must have integer id."
                    )
        return tables


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
    if not _table_exists(conn, table_name):
        return []
    return rows_as_dicts(conn.execute(f'SELECT * FROM "{table_name}" ORDER BY id ASC'))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
            AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _target_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]


def _assert_no_host_owned_tables(conn: sqlite3.Connection) -> None:
    known_tables = (
        frozenset(CANONICAL_TABLES)
        | VEXIC_PROJECTION_TABLES
        | VEXIC_OPERATOR_TABLES
    )
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
    init_db(db_path)
    target = Path(artifact_path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite migration artifact: {target}")

    with closing(connect(db_path)) as conn:
        _assert_no_host_owned_tables(conn)
        payload = {
            "artifact_version": ARTIFACT_VERSION,
            "scope": {"tenant_id": tenant_id, "project_id": project_id},
            "tables": {table_name: _rows(conn, table_name) for table_name in CANONICAL_TABLES},
        }

    try:
        assert_no_forbidden_secret_values(
            tuple(forbidden_secret_values),
            *_iter_payload_strings(payload),
        )
    except Exception:
        if overwrite:
            target.unlink(missing_ok=True)
        raise
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
        _assert_no_extra_rows(conn, table_name, set())
        return 0
    columns = _target_columns(conn, table_name)
    expected = set(columns)
    for row in rows:
        if set(row) != expected:
            raise ValueError(f"canonical migration artifact row in {table_name} has invalid columns.")
    column_sql = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    imported = 0
    _assert_no_extra_rows(conn, table_name, {int(row["id"]) for row in rows})
    for row in rows:
        existing_cursor = conn.execute(
            f'SELECT {column_sql} FROM "{table_name}" WHERE id = ?',
            (row["id"],),
        )
        existing = row_as_dict(existing_cursor, existing_cursor.fetchone())
        if existing is not None:
            if existing != {column: row[column] for column in columns}:
                raise ValueError(f"Conflicting canonical row in {table_name} id {row['id']}.")
            continue
        conn.execute(
            f'INSERT INTO "{table_name}" ({column_sql}) VALUES ({placeholders})',
            [row[column] for column in columns],
        )
        imported += 1
    return imported


def _assert_no_extra_rows(
    conn: sqlite3.Connection,
    table_name: str,
    artifact_ids: set[int],
) -> None:
    if not _table_exists(conn, table_name):
        return
    target_ids = {
        int(row[0])
        for row in conn.execute(f'SELECT id FROM "{table_name}"').fetchall()
    }
    extra_ids = sorted(target_ids - artifact_ids)
    if extra_ids:
        raise ValueError(
            f"Target table {table_name} contains canonical rows outside the artifact: "
            + ", ".join(str(row_id) for row_id in extra_ids)
        )


def _record_import_metadata(
    target_db_path: str,
    *,
    tenant_id: str,
    project_id: str | None,
) -> None:
    with closing(connect(target_db_path)) as conn:
        with conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {MIGRATION_METADATA_TABLE} (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    artifact_version TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT,
                    imported_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO {MIGRATION_METADATA_TABLE}
                    (id, artifact_version, tenant_id, project_id)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    artifact_version = excluded.artifact_version,
                    tenant_id = excluded.tenant_id,
                    project_id = excluded.project_id,
                    imported_at = CURRENT_TIMESTAMP
                """,
                (ARTIFACT_VERSION, tenant_id, project_id),
            )


def _load_artifact(artifact_path: str | Path) -> _CanonicalMigrationArtifact:
    try:
        return _CanonicalMigrationArtifact.model_validate_json(Path(artifact_path).read_text())
    except ValidationError as exc:
        raise ValueError("Invalid canonical migration artifact.") from exc


def import_canonical_migration(
    artifact_path: str | Path,
    target_db_path: str,
    *,
    tenant_id: str,
    project_id: str | None,
    forbidden_secret_values: Iterable[str] = (),
) -> CanonicalMigrationImportReport:
    artifact = _load_artifact(artifact_path)
    if artifact.artifact_version != ARTIFACT_VERSION:
        raise ValueError("Unsupported canonical migration artifact version.")
    if artifact.scope.tenant_id != tenant_id or artifact.scope.project_id != project_id:
        raise PermissionError("Migration artifact scope does not match hosted operator scope.")
    assert_no_forbidden_secret_values(
        tuple(forbidden_secret_values),
        *_iter_payload_strings(artifact.model_dump(mode="json")),
    )

    target = Path(target_db_path)
    if target.exists():
        with closing(connect(target)) as conn:
            _assert_no_host_owned_tables(conn)
    init_vector_memory(target_db_path)
    rows_imported = 0
    with closing(connect(target_db_path)) as conn:
        with conn:
            for table_name in CANONICAL_TABLES:
                rows_imported += _insert_rows(conn, table_name, artifact.tables[table_name])

    repair_report = repair_memory_projections(
        target_db_path,
        forbidden_secret_values=forbidden_secret_values,
    )
    _record_import_metadata(
        target_db_path,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    return CanonicalMigrationImportReport(
        rows_imported=rows_imported,
        repair_report=repair_report,
    )
