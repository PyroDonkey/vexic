from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

@dataclass(frozen=True)
class StorageTarget:
    """A resolved storage target: a filesystem path or libSQL DSN plus an
    optional auth token. The token is auth metadata, not identity, and must
    never be logged: it is excluded from repr/eq/hash."""
    target: str
    auth_token: str | None = field(default=None, repr=False, compare=False, hash=False)

    def __repr__(self) -> str:
        tok = "***" if self.auth_token else None
        return f"StorageTarget(target={self.target!r}, auth_token={tok})"

    def as_connect_args(self) -> tuple[str, str | None]:
        return self.target, self.auth_token


# Hosted libSQL/Turso targets (ADR 0019) arrive as URLs; a filesystem path or
# ":memory:" is the local SQLite backend. ``str.startswith`` accepts the tuple.
_LIBSQL_SCHEMES = ("libsql://", "https://", "http://", "wss://", "ws://")
_PLAINTEXT_LIBSQL_SCHEMES = ("http://", "ws://")


class StorageConnection(Protocol):
    """Structural type shared by the local ``sqlite3`` connection and the hosted
    libSQL connection behind the :func:`connect` seam.

    The libSQL connection (ADR 0019 verification spike) supports this DB-API
    subset -- ``execute``/``executemany``/``cursor``/``commit``/``rollback``/
    ``close`` and the ``with conn:`` transaction context (rollback on exception,
    verified equivalent to ``sqlite3``) -- but NOT a settable ``row_factory``
    (use :func:`rows_as_dicts`), named/dict params, or ``enable_load_extension``.
    """

    def execute(self, sql: str, parameters: Any = ..., /) -> Any: ...
    def executemany(self, sql: str, parameters: Any, /) -> Any: ...
    def cursor(self) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Any: ...
    def __exit__(self, *exc: Any) -> Any: ...


def _is_libsql_target(target: str | Path) -> bool:
    """True when ``target`` names a hosted libSQL database rather than a local
    SQLite file or ``:memory:``."""
    return isinstance(target, str) and target.startswith(_LIBSQL_SCHEMES)


def connect(
    target: str | Path,
    *,
    auth_token: str | None = None,
    **kwargs: Any,
) -> StorageConnection:
    """Open a Vexic storage connection for a local SQLite or hosted libSQL target.

    This is the single connection seam for Vexic storage. Every storage module
    opens its database through this function instead of calling
    ``sqlite3.connect`` directly, so the hosted Turso/libSQL cutover (ADR 0019)
    dispatches a remote target here rather than editing every call site.

    A filesystem path or ``:memory:`` opens local SQLite, preserving the
    positional target and keyword arguments (for example ``timeout``). A
    ``libsql://``/``https://`` target opens a managed libSQL connection using the
    already-resolved ``auth_token`` supplied by the caller: the token is passed
    in by the repo-root ``adapters/`` layer, and ``src/vexic`` never reads it
    from the environment (ADR 0019). The ``libsql`` client is an optional
    ``hosted`` extra, imported lazily so the local path needs no extra dependency.
    """
    if _is_libsql_target(target):
        # ``target`` is trusted configuration -- a resolved DSN handed in by the
        # adapters/ layer -- never user input, so scheme-based routing here is
        # not an outbound-request boundary.
        if auth_token is not None and target.startswith(_PLAINTEXT_LIBSQL_SCHEMES):
            raise ValueError("Refusing to send a libSQL auth token over plaintext.")
        try:
            import libsql  # lazy: optional ``hosted`` extra, remote targets only
        except ImportError as exc:  # pragma: no cover - only when the extra is absent
            raise ImportError(
                "libSQL/Turso targets require the optional 'hosted' extra. Install "
                "it with `uv sync --extra hosted` (or `pip install vexic[hosted]`)."
            ) from exc

        if auth_token is None:
            return libsql.connect(target)
        return libsql.connect(target, auth_token=auth_token)
    return sqlite3.connect(target, **kwargs)


def rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    """Map every remaining row of an executed cursor to a column-keyed dict.

    Backend-agnostic replacement for ``conn.row_factory = sqlite3.Row``: the
    hosted libSQL connection (ADR 0019) exposes no settable ``row_factory``, but
    both drivers expose ``cursor.description``, so column names are read from
    there. A statement with no result set (DDL/DML) has a falsy description
    (``None`` on sqlite3, ``()`` on libSQL) and yields an empty list.
    """
    description = cursor.description
    if not description:
        return []
    columns = [column[0] for column in description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def row_as_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    """Map a single already-fetched row to a column-keyed dict, or ``None``.

    Companion to :func:`rows_as_dicts` for the ``fetchone`` call sites. Returns
    ``None`` when the row is ``None`` (no match) or the statement produced no
    result set.
    """
    if row is None:
        return None
    description = cursor.description
    if not description:
        return None
    columns = [column[0] for column in description]
    return dict(zip(columns, row, strict=True))
