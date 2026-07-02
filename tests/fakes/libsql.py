"""Creds-free test double for the managed-libSQL connection contract.

Backed by an in-memory ``sqlite3`` connection, but enforces the subset of the
DB-API that the hosted libSQL/Turso driver actually supports (see
``vexic.storage.connection.StorageConnection`` for the documented contract):

- No settable ``row_factory``.
- No named/dict params -- only positional ``?`` params.
- No ``enable_load_extension`` (accessing or calling it raises
  ``AttributeError``, matching the real driver).
- ``execute``/``executemany``/``cursor``/``commit``/``rollback``/``close``
  and the ``with conn:`` transaction context (rollback on exception) all
  delegate to the underlying ``sqlite3`` connection.

This lets control-plane tests exercise libSQL-shaped code paths without
Turso credentials.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def _reject_dict_params(parameters: Any) -> None:
    if isinstance(parameters, dict):
        raise TypeError(
            "FakeLibsqlConn: named/dict parameters are not supported by the "
            "libSQL connection contract; use positional '?' params."
        )


class FakeLibsqlConn:
    """Minimal stand-in for a managed libSQL connection, backed by sqlite3."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")

    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        _reject_dict_params(parameters)
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        for row in parameters:
            _reject_dict_params(row)
        return self._conn.executemany(sql, parameters)

    def cursor(self) -> Any:
        return self._conn.cursor()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "FakeLibsqlConn":
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)

    @property
    def enable_load_extension(self) -> Any:
        raise AttributeError(
            "FakeLibsqlConn has no attribute 'enable_load_extension' "
            "(the managed libSQL connection does not support it)"
        )

    @property
    def row_factory(self) -> Any:
        raise AttributeError(
            "FakeLibsqlConn has no attribute 'row_factory' "
            "(the managed libSQL connection exposes no settable row_factory; "
            "use vexic.storage.connection.rows_as_dicts instead)"
        )

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        raise AttributeError(
            "FakeLibsqlConn has no attribute 'row_factory' "
            "(the managed libSQL connection exposes no settable row_factory; "
            "use vexic.storage.connection.rows_as_dicts instead)"
        )
