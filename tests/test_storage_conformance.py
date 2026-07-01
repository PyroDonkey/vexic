from __future__ import annotations

import importlib.util
import os
import uuid
from typing import Any

import pytest

from vexic.embeddings import EMBEDDING_DIM
from vexic.storage.connection import StorageTarget, connect, rows_as_dicts
from vexic.storage import schema as storage_schema
from vexic.storage.errors import is_unique_violation
from vexic.storage.schema import _normalize_embedding, _serialize_float32
from vexic.storage.vectors import select_vector_backend

# Storage-adapter conformance (ADR 0005 / ADR 0019): the same vector search, FTS5
# search, and row-mapping behaviors must hold on the local sqlite-vec reference
# adapter AND the hosted libSQL adapter. The libSQL parameter is skipped when no
# Turso creds are in the environment, so the default `uv run pytest` stays green
# for contributors without a Turso account (mirrors the other live-gated tests).
#
# Isolated `_conf_*` table names keep the shared Turso dev database clean; the
# fixture drops them on entry and exit for the remote backend.

_TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_LIBSQL_INSTALLED = importlib.util.find_spec("libsql") is not None
_HAS_TURSO = bool(_TURSO_URL and _TURSO_TOKEN and _LIBSQL_INSTALLED)

# Per-process suffix so concurrent test runs never collide on the shared Turso
# dev database (the tables live in one remote DB).
_SUFFIX = uuid.uuid4().hex[:12]
_CONF_EMB = f"_conf_emb_{_SUFFIX}"
_CONF_BASE = f"_conf_base_{_SUFFIX}"
_CONF_FTS = f"_conf_fts_{_SUFFIX}"


def _unit(index: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[index] = 1.0
    return vec


def _drop_conformance_tables(conn: Any) -> None:
    for name in (_CONF_EMB, _CONF_FTS, _CONF_BASE):
        try:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
        except Exception:  # noqa: BLE001 -- best-effort cleanup on a shared remote DB
            pass
    conn.commit()


@pytest.fixture(
    params=[
        "local",
        pytest.param(
            "libsql",
            marks=pytest.mark.skipif(
                not _HAS_TURSO,
                reason="Turso creds not in environment, or libsql (vexic[hosted]) not installed",
            ),
        ),
    ]
)
def conformance_conn(request: pytest.FixtureRequest, tmp_path: Any) -> Any:
    if request.param == "local":
        conn = connect(str(tmp_path / "conformance.db"))
    else:
        conn = connect(StorageTarget(_TURSO_URL, auth_token=_TURSO_TOKEN))  # type: ignore[arg-type]
        _drop_conformance_tables(conn)
    try:
        yield conn
    finally:
        if request.param == "libsql":
            _drop_conformance_tables(conn)
        conn.close()


def test_vector_knn_ranks_nearest_first(conformance_conn: Any) -> None:
    conn = conformance_conn
    backend = select_vector_backend(conn)
    backend.prepare(conn)
    backend.create_embeddings_table(conn, table=_CONF_EMB, id_column="item_id")

    near = _normalize_embedding([0.9, 0.1] + [0.0] * (EMBEDDING_DIM - 2))
    vectors = {1: _unit(0), 2: _unit(1), 3: near}
    for item_id, vec in vectors.items():
        conn.execute(
            f"INSERT INTO {_CONF_EMB} (item_id, embedding) VALUES (?, ?)",
            (item_id, _serialize_float32(vec)),
        )
    conn.commit()

    knn = backend.knn_subquery(table=_CONF_EMB, id_column="item_id")
    rows = conn.execute(
        f"SELECT e._id, e._distance FROM ({knn}) AS e ORDER BY e._distance",
        (_serialize_float32(_unit(0)), 3),
    ).fetchall()

    ranked = [int(row[0]) for row in rows]
    assert ranked[0] == 1  # exact match is nearest
    assert set(ranked) == {1, 2, 3}
    assert ranked.index(3) < ranked.index(2)  # item 3 nearer to the query than item 2

    top_similarity = backend.similarity(float(rows[0][1]))
    assert top_similarity == pytest.approx(1.0, abs=1e-4)  # identical vector


def test_fts5_external_content_match(conformance_conn: Any) -> None:
    conn = conformance_conn
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_CONF_BASE} (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_CONF_FTS} "
        f"USING fts5(body, content='{_CONF_BASE}', content_rowid='id')"
    )
    conn.execute(f"INSERT INTO {_CONF_BASE} (id, body) VALUES (1, 'alpha beta gamma')")
    conn.execute(f"INSERT INTO {_CONF_FTS} (rowid, body) VALUES (1, 'alpha beta gamma')")
    conn.commit()

    rows = conn.execute(
        f"SELECT rowid FROM {_CONF_FTS} WHERE {_CONF_FTS} MATCH ?", ("beta",)
    ).fetchall()
    assert [int(row[0]) for row in rows] == [1]


def test_rows_as_dicts_on_backend(conformance_conn: Any) -> None:
    conn = conformance_conn
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_CONF_BASE} (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute(f"INSERT INTO {_CONF_BASE} (id, name) VALUES (1, 'alpha')")
    conn.commit()

    dicts = rows_as_dicts(conn.execute(f"SELECT id, name FROM {_CONF_BASE} ORDER BY id"))
    assert dicts == [{"id": 1, "name": "alpha"}]


def test_knn_subquery_filters_after_knn(conformance_conn: Any) -> None:
    # The subquery-wrapped KNN must preserve "nearest by vector, THEN relational
    # filter" on both backends -- the exact shape the candidate/long-term
    # retrievers use. Item 1 is the nearest vector but inactive; item 2 is
    # slightly farther but active, so only item 2 may come back.
    conn = conformance_conn
    backend = select_vector_backend(conn)
    backend.prepare(conn)
    backend.create_embeddings_table(conn, table=_CONF_EMB, id_column="item_id")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_CONF_BASE} (id INTEGER PRIMARY KEY, active INTEGER)")

    query = _unit(0)
    near = _normalize_embedding([0.95, 0.05] + [0.0] * (EMBEDDING_DIM - 2))
    conn.execute(f"INSERT INTO {_CONF_EMB} (item_id, embedding) VALUES (1, ?)", (_serialize_float32(query),))
    conn.execute(f"INSERT INTO {_CONF_EMB} (item_id, embedding) VALUES (2, ?)", (_serialize_float32(near),))
    conn.execute(f"INSERT INTO {_CONF_BASE} (id, active) VALUES (1, 0)")
    conn.execute(f"INSERT INTO {_CONF_BASE} (id, active) VALUES (2, 1)")
    conn.commit()

    knn = backend.knn_subquery(table=_CONF_EMB, id_column="item_id")
    rows = conn.execute(
        f"""
        SELECT e._id
        FROM ({knn}) AS e
        JOIN {_CONF_BASE} AS b ON b.id = e._id
        WHERE b.active = 1
        ORDER BY e._distance
        """,
        (_serialize_float32(query), 5),
    ).fetchall()
    assert [int(row[0]) for row in rows] == [2]


def test_transaction_rolls_back_on_exception(conformance_conn: Any) -> None:
    # `with conn:` must roll back an uncommitted write when the block raises, on
    # both sqlite3 and libSQL (264c spike verified libSQL matches sqlite3 here).
    conn = conformance_conn
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_CONF_BASE} (id INTEGER PRIMARY KEY)")
    conn.commit()

    with pytest.raises(RuntimeError):
        with conn:
            conn.execute(f"INSERT INTO {_CONF_BASE} (id) VALUES (1)")
            raise RuntimeError("boom")

    remaining = conn.execute(f"SELECT count(*) FROM {_CONF_BASE}").fetchone()[0]
    assert remaining == 0


def test_local_libsql_initializes_full_storage_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    libsql = pytest.importorskip("libsql")
    db_path = tmp_path / "local-libsql-schema.db"

    def connect_local_libsql(_target: str) -> Any:
        return libsql.connect(str(db_path))

    monkeypatch.setattr(storage_schema, "connect", connect_local_libsql)

    storage_schema.init_db("libsql://local-test")
    storage_schema.init_vector_memory("libsql://local-test")

    conn = libsql.connect(str(db_path))
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "messages" in tables
    assert "memory_candidates" in tables
    assert "memory_candidate_embeddings" in tables
    assert "long_term_memory_embeddings" in tables


def test_is_unique_violation_detected_on_backend(conformance_conn: Any) -> None:
    # Constraint-violation detection parity (Task 9b): the shared classifier must
    # recognize a UNIQUE conflict on BOTH backends -- sqlite3 raises
    # sqlite3.IntegrityError, hosted libSQL raises a bare ValueError carrying the
    # Hrana/SQLITE_CONSTRAINT payload. The classifier normalizes both.
    conn = conformance_conn
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_CONF_BASE} (id INTEGER PRIMARY KEY, uid TEXT UNIQUE)")
    conn.execute(f"INSERT INTO {_CONF_BASE} (id, uid) VALUES (1, 'dup')")
    conn.commit()

    raised: Exception | None = None
    try:
        conn.execute(f"INSERT INTO {_CONF_BASE} (id, uid) VALUES (2, 'dup')")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- backends raise different types
        raised = exc
    finally:
        conn.rollback()

    assert raised is not None, "duplicate UNIQUE insert should raise on this backend"
    assert is_unique_violation(raised) is True


def test_explicit_begin_savepoint_parity(conformance_conn: Any) -> None:
    # Explicit-BEGIN SAVEPOINT parity (Task 9b): under an explicit BEGIN, a
    # SAVEPOINT / insert / ROLLBACK TO / RELEASE sequence must behave identically
    # on both backends -- the rolled-back row is absent, the kept row present.
    # libSQL's `with conn:` does NOT open an implicit transaction, so the ingest
    # path relies on the explicit BEGIN exercised here.
    conn = conformance_conn
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_CONF_BASE} (id INTEGER PRIMARY KEY)")
    conn.commit()

    conn.execute("BEGIN")
    try:
        # Kept row, no savepoint.
        conn.execute(f"INSERT INTO {_CONF_BASE} (id) VALUES (1)")

        # Rolled-back row, inside a savepoint.
        conn.execute("SAVEPOINT conf_sp")
        conn.execute(f"INSERT INTO {_CONF_BASE} (id) VALUES (2)")
        conn.execute("ROLLBACK TO SAVEPOINT conf_sp")
        conn.execute("RELEASE SAVEPOINT conf_sp")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()

    ids = {int(row[0]) for row in conn.execute(f"SELECT id FROM {_CONF_BASE}").fetchall()}
    assert ids == {1}, "kept row present, rolled-back savepoint row absent"


def test_storage_target_is_exported() -> None:
    from vexic.storage import StorageTarget as ST
    assert ST is not None
