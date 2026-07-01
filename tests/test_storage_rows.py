import sqlite3

from vexic.storage.connection import row_as_dict, rows_as_dicts


def _seed_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO t (id, name) VALUES (1, 'alpha'), (2, 'beta')")
    return conn


def test_rows_as_dicts_maps_columns_to_values() -> None:
    conn = _seed_conn()
    cursor = conn.execute("SELECT id, name FROM t ORDER BY id")
    assert rows_as_dicts(cursor) == [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]


def test_rows_as_dicts_empty_result_set_returns_empty_list() -> None:
    conn = _seed_conn()
    cursor = conn.execute("SELECT id, name FROM t WHERE id = 999")
    assert rows_as_dicts(cursor) == []


def test_rows_as_dicts_non_select_statement_returns_empty_list() -> None:
    # DDL/DML produces no result set: description is None on sqlite3 (and () on
    # libSQL). The guard must not crash trying to read column names.
    conn = sqlite3.connect(":memory:")
    cursor = conn.execute("CREATE TABLE t (id INTEGER)")
    assert rows_as_dicts(cursor) == []


def test_row_as_dict_maps_single_row() -> None:
    conn = _seed_conn()
    cursor = conn.execute("SELECT id, name FROM t WHERE id = 1")
    assert row_as_dict(cursor, cursor.fetchone()) == {"id": 1, "name": "alpha"}


def test_row_as_dict_missing_row_returns_none() -> None:
    conn = _seed_conn()
    cursor = conn.execute("SELECT id, name FROM t WHERE id = 999")
    assert row_as_dict(cursor, cursor.fetchone()) is None
