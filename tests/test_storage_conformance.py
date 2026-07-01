from __future__ import annotations

import importlib.util
import os
import uuid
from typing import Any

import pytest

from vexic.embeddings import EMBEDDING_DIM
from vexic.storage.connection import connect, rows_as_dicts
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
        conn = connect(_TURSO_URL, auth_token=_TURSO_TOKEN)  # type: ignore[arg-type]
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
