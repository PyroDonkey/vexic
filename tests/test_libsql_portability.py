# tests/test_libsql_portability.py
from __future__ import annotations
import importlib.util, os, uuid
import pytest
from vexic.storage.connection import connect, rows_as_dicts
from vexic.storage.errors import is_operational_error

_URL = os.environ.get("TURSO_DATABASE_URL")
_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_HAS = bool(_URL and _TOKEN and importlib.util.find_spec("libsql"))
pytestmark = pytest.mark.skipif(not _HAS, reason="Turso creds or libsql missing")

_T = f"_probe_{uuid.uuid4().hex[:12]}"

@pytest.fixture
def conn():
    c = connect(_URL, auth_token=_TOKEN)
    yield c
    try:
        c.execute(f"DROP TABLE IF EXISTS {_T}"); c.commit()
    finally:
        c.close()

def test_autoincrement_and_foreign_keys(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"CREATE TABLE {_T} (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    conn.execute(f"INSERT INTO {_T} (v) VALUES (?)", ("a",))
    conn.commit()
    rows = rows_as_dicts(conn.execute(f"SELECT id, v FROM {_T}"))
    assert rows == [{"id": 1, "v": "a"}]

def test_begin_immediate(conn):
    conn.execute(f"CREATE TABLE {_T} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"INSERT INTO {_T} DEFAULT VALUES")
    conn.commit()

def test_julianday_expression_index(conn):
    # Gate: if this raises, the control-plane usage-event index needs a fallback.
    conn.execute(f"CREATE TABLE {_T} (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT)")
    conn.execute(f"CREATE INDEX idx_{_T}_jd ON {_T}(julianday(recorded_at))")
    conn.commit()


def test_concurrent_read_then_write_conflicts_on_libsql():
    # The promotion pipeline's read-then-write CAS paths (commit_dream_cycle
    # watermark CAS; backfill_missing_candidate_embeddings liveness recheck)
    # open their transaction with a plain BEGIN on libSQL (managed Turso has no
    # local pre-read write lock; see ADR 0019 Addendum 3).
    # Their concurrent-Light safety RELIES on Turso rejecting a stale write at
    # commit rather than a BEGIN IMMEDIATE lock. This probe proves that
    # reliance directly: two connections that both read a value, then one
    # advances and commits, must NOT let the other silently commit a write
    # derived from its now-stale snapshot. SQLite (and libSQL, which inherits
    # its transaction semantics) returns SQLITE_BUSY_SNAPSHOT to the stale
    # writer. The pipeline's CAS then re-reads and aborts as a no-op; here we
    # assert the raw storage guarantee the CAS stands on.
    a = connect(_URL, auth_token=_TOKEN)
    b = connect(_URL, auth_token=_TOKEN)
    try:
        a.execute(f"CREATE TABLE {_T} (id INTEGER PRIMARY KEY, w INTEGER)")
        a.execute(f"INSERT INTO {_T} (id, w) VALUES (1, 0)")
        a.commit()

        # Both transactions read watermark w = 0 under their own snapshot.
        a.execute("BEGIN")
        b.execute("BEGIN")
        assert rows_as_dicts(a.execute(f"SELECT w FROM {_T} WHERE id = 1")) == [{"w": 0}]
        assert rows_as_dicts(b.execute(f"SELECT w FROM {_T} WHERE id = 1")) == [{"w": 0}]

        # Connection A advances the watermark to 1 and commits.
        a.execute(f"UPDATE {_T} SET w = 1 WHERE id = 1")
        a.commit()

        # Connection B, still on its stale snapshot, tries to advance to 2. A
        # silent success here would be the lost update the pipeline CAS guards
        # against; libSQL/Turso must reject it as a busy/snapshot conflict.
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 -- backend raises ValueError, not a typed error
            b.execute(f"UPDATE {_T} SET w = 2 WHERE id = 1")
            b.commit()
        assert is_operational_error(excinfo.value), (
            f"stale writer must fail as an operational (busy/snapshot) conflict, "
            f"got: {excinfo.value!r}"
        )
        b.rollback()

        # The committed writer's value stands; the stale write did not land.
        assert rows_as_dicts(a.execute(f"SELECT w FROM {_T} WHERE id = 1")) == [{"w": 1}]
    finally:
        for c in (a, b):
            try:
                c.execute(f"DROP TABLE IF EXISTS {_T}")
                c.commit()
            except Exception:  # noqa: BLE001 -- best-effort cleanup on a shared remote DB
                pass
            c.close()
