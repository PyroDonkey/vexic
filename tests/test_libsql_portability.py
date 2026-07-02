# tests/test_libsql_portability.py
from __future__ import annotations
import importlib.util, os, uuid
import pytest
from vexic.storage.connection import connect, rows_as_dicts

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
