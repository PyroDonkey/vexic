"""`import_canonical_migration` accepts a libSQL
`StorageTarget` as the import target, not just a local filesystem path.

Creds-free: the import target is backed by a `FakeLibsqlConn` (in-memory
sqlite3 under the managed-libSQL DB-API contract) via
`vexic.storage.connection.connect` monkeypatched to return it for the given
`StorageTarget`, mirroring the pattern in tests/test_control_plane_libsql.py.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from tests.fakes.libsql import FakeLibsqlConn
from vexic.migration import export_canonical_migration, import_canonical_migration
from vexic.storage import SourceTranscriptInput, ingest_source_messages, init_db
from vexic.storage import single_message_adapter
from vexic.storage import StorageTarget


def _source_message(text: str) -> SourceTranscriptInput:
    message_json = single_message_adapter.dump_json(
        ModelRequest(parts=[UserPromptPart(content=text)])
    ).decode()
    return SourceTranscriptInput(
        source_host="local-vexic",
        source_session_id="session-a",
        source_message_id="message-a",
        message_json=message_json,
    )


class _NonClosingFakeConnHandle:
    """Wraps a shared `FakeLibsqlConn` so per-call `closing(connect(...))` in
    production code does not tear down the underlying fake between calls --
    a real libSQL/Turso connection is a persistent remote session, and the
    migration import target must see everything written across its several
    `connect()` calls (existence probe, row inserts, FTS repair, metadata)."""

    def __init__(self, fake_conn: FakeLibsqlConn) -> None:
        self._fake_conn = fake_conn

    def execute(self, sql, parameters=(), /):
        return self._fake_conn.execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self._fake_conn.executemany(sql, parameters)

    def cursor(self):
        return self._fake_conn.cursor()

    def commit(self):
        self._fake_conn.commit()

    def rollback(self):
        self._fake_conn.rollback()

    def close(self):
        pass  # intentionally does not close the shared fake

    def __enter__(self):
        self._fake_conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fake_conn.__exit__(*exc)


def _patch_connect_to_fake(monkeypatch, fake_conn: FakeLibsqlConn) -> None:
    import vexic.migration as migration
    import vexic.storage.schema as storage_schema
    import vexic.storage.operators as storage_operators
    import vexic.storage.transcript as storage_transcript

    def _fake_connect(target, *, auth_token=None, **kwargs):
        assert isinstance(target, StorageTarget), (
            f"expected migration import connect() to receive a StorageTarget, got {target!r}"
        )
        assert target.auth_token == "s3cr3t-token"
        return _NonClosingFakeConnHandle(fake_conn)

    monkeypatch.setattr(migration, "connect", _fake_connect)
    monkeypatch.setattr(storage_schema, "connect", _fake_connect)
    monkeypatch.setattr(storage_operators, "connect", _fake_connect)
    monkeypatch.setattr(storage_transcript, "connect", _fake_connect)


def _forbid_local_path_ops_on_target(monkeypatch, dsn: str) -> None:
    """Guard against `Path(<the StorageTarget's DSN>)` filesystem ops -- the
    real bug this test catches is migration code calling `Path(target_db_path)`
    /`.exists()` on what is actually a `StorageTarget`/DSN, not a local file.
    Only `Path.exists` calls on a path matching the DSN are trapped, so
    pytest's own internal `Path.exists` usage (traceback formatting, etc.)
    is unaffected.
    """
    real_exists = Path.exists

    def _guarded_exists(self, *args, **kwargs):
        if str(self) == dsn or str(self).endswith(dsn):
            raise AssertionError(
                f"Path.exists() must not run for a StorageTarget import target (got {self!r})."
            )
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", _guarded_exists)


def test_import_canonical_migration_accepts_libsql_storage_target(monkeypatch, tmp_path):
    source_db = tmp_path / "source.db"
    artifact = tmp_path / "canonical-migration.json"

    init_db(str(source_db))
    ingest_source_messages(
        str(source_db),
        [_source_message("cedar migration transcript")],
        session_id="session-a",
        agent_id="agent-a",
    )
    export_canonical_migration(
        str(source_db),
        artifact,
        tenant_id="tenant-a",
        project_id="project-a",
    )

    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    target = StorageTarget("libsql://fake-migration-target", auth_token="s3cr3t-token")
    _forbid_local_path_ops_on_target(monkeypatch, target.target)

    report = import_canonical_migration(
        artifact,
        target,
        tenant_id="tenant-a",
        project_id="project-a",
    )

    assert report.rows_imported > 0

    # Row parity: the message landed in the imported target's `messages` table.
    message_rows = fake_conn.execute("SELECT message_json FROM messages").fetchall()
    assert len(message_rows) == 1
    assert "cedar migration transcript" in message_rows[0][0]

    # FTS parity: the message is searchable via the rebuilt `messages_fts`
    # projection on the imported target (repair_memory_projections runs as
    # part of import_canonical_migration).
    fts_rows = fake_conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'cedar'"
    ).fetchall()
    assert len(fts_rows) == 1
