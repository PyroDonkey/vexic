from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from vexic.storage.errors import QueryDeadlineExceeded

@dataclass(frozen=True)
class StorageTarget:
    """A resolved storage target: a filesystem path or libSQL DSN plus an
    optional auth token. The token is auth metadata, not identity, and must
    never be logged: it is excluded from repr/eq/hash."""
    target: str
    auth_token: str | None = field(default=None, repr=False, compare=False, hash=False)
    # Wall-clock bound on each remote driver call (ADR 0019 Addendum 7). ``None`` means the
    # module default; ignored for local SQLite targets.
    query_deadline_seconds: float | None = None

    def __repr__(self) -> str:
        tok = "***" if self.auth_token else None
        return f"StorageTarget(target={self.target!r}, auth_token={tok})"

    def as_connect_args(self) -> tuple[str, str | None]:
        return self.target, self.auth_token


# Hosted libSQL/Turso targets (ADR 0019) arrive as URLs; a filesystem path or
# ":memory:" is the local SQLite backend. ``str.startswith`` accepts the tuple.
_LIBSQL_SCHEMES = ("libsql://", "https://", "http://", "wss://", "ws://")
_PLAINTEXT_LIBSQL_SCHEMES = ("http://", "ws://")

# Wall-clock bound on each remote libSQL driver call (ADR 0019 Addendum 7). Well above
# observed Turso latencies and the ~10s idle-stream reap; well below "hangs
# forever". Local SQLite is never bounded.
DEFAULT_QUERY_DEADLINE_SECONDS = 30.0


class StorageConnection(Protocol):
    """Structural type shared by the local ``sqlite3`` connection and the hosted
    libSQL connection behind the :func:`connect` seam.

    The libSQL connection (ADR 0019 verification spike) supports this DB-API
    subset -- ``execute``/``executemany``/``cursor``/``commit``/``rollback``/
    ``close`` and the ``with conn:`` transaction context (rollback on
    exception) -- but NOT a settable ``row_factory`` (use :func:`rows_as_dicts`),
    named/dict params, or ``enable_load_extension``.

    Transaction caveat (verified live against Turso, ADR 0019): the ``with conn:``
    context rolls back on exception on both backends, but on managed libSQL it
    does NOT open an implicit transaction the way ``sqlite3`` does -- each
    ``execute`` auto-commits its own micro-transaction. So a ``SAVEPOINT`` /
    ``ROLLBACK TO SAVEPOINT`` / ``RELEASE SAVEPOINT`` sequence does NOT persist
    across ``execute`` calls under a bare ``with conn:`` on libSQL (the savepoint
    is gone by the next call, "no such savepoint"). Nested savepoint logic must
    issue an explicit ``BEGIN`` first so it has a real open transaction to nest
    inside (see ``transcript.ingest_source_messages``).
    """

    def execute(self, sql: str, parameters: Any = ..., /) -> Any: ...
    def executemany(self, sql: str, parameters: Any, /) -> Any: ...
    def cursor(self) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Any: ...
    def __exit__(self, *exc: Any) -> Any: ...


class DeadlineConnection:
    """Bound every remote libSQL driver call with a wall-clock deadline (ADR 0019 Addendum 7).

    The driver's ``timeout=`` kwarg is not a network deadline and ``connect()``
    does no I/O (ADR 0019 Addendum 6), so a degraded remote can hang any query
    round-trip indefinitely. Each call runs on a daemon worker thread; if it
    outruns ``deadline_seconds`` the caller gets :class:`QueryDeadlineExceeded`
    -- a retryable storage fault -- and the hung call is abandoned (the daemon
    thread dies with the process rather than blocking interpreter shutdown,
    which a ``ThreadPoolExecutor``'s non-daemon threads would).
    """

    def __init__(self, conn: Any, *, deadline_seconds: float) -> None:
        self._conn = conn
        self._deadline_seconds = deadline_seconds
        self._poisoned = False

    def _run(self, call: Callable[[], Any]) -> Any:
        if self._poisoned:
            raise QueryDeadlineExceeded(
                "remote libSQL connection was abandoned after a query deadline "
                "timeout; open a fresh connection"
            )
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                outcome["result"] = call()
            except BaseException as exc:  # noqa: BLE001 - re-raised in caller
                outcome["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        if not done.wait(self._deadline_seconds):
            self._poisoned = True
            raise QueryDeadlineExceeded(
                f"remote libSQL call exceeded the {self._deadline_seconds}s "
                "query deadline; abandoning the connection"
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def execute(self, sql: str, parameters: Any = (), /) -> "DeadlineCursor":
        # Wrap the returned cursor: ``conn.execute(...).fetchall()`` is the
        # dominant call-site pattern, and remote work deferred until fetch
        # must run under the same deadline.
        return DeadlineCursor(
            self._run(lambda: self._conn.execute(sql, parameters)), self
        )

    def executemany(self, sql: str, parameters: Any, /) -> "DeadlineCursor":
        return DeadlineCursor(
            self._run(lambda: self._conn.executemany(sql, parameters)), self
        )

    def cursor(self) -> "DeadlineCursor":
        # Cursors execute independently and share the connection's stream, so
        # they run under the same deadline and poison the whole connection.
        return DeadlineCursor(self._run(self._conn.cursor), self)

    def commit(self) -> None:
        self._run(self._conn.commit)

    def rollback(self) -> None:
        self._run(self._conn.rollback)

    def close(self) -> None:
        # A poisoned connection's underlying close() could hang on the same
        # dead remote; drop the reference instead. Callers universally use
        # ``with closing(connect(...))``, so close() must never raise or block.
        if self._poisoned:
            return
        self._run(self._conn.close)

    def __enter__(self) -> Any:
        self._run(self._conn.__enter__)
        return self

    def __exit__(self, *exc: Any) -> Any:
        # After a timeout the rollback-on-exit round-trip would hang on the
        # same dead remote; skip it and let the timeout itself propagate.
        if self._poisoned:
            return False
        return self._run(lambda: self._conn.__exit__(*exc))


class DeadlineCursor:
    """Cursor companion to :class:`DeadlineConnection`.

    Round-trip methods run under the parent connection's deadline via its
    shared runner, so a hung cursor call poisons the whole connection (the
    dead stream is common to both). Non-round-trip attributes (``description``,
    ``lastrowid``, ...) pass through.
    """

    def __init__(self, cursor: Any, parent: DeadlineConnection) -> None:
        self._cursor = cursor
        self._parent = parent

    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        self._parent._run(lambda: self._cursor.execute(sql, parameters))
        return self

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        self._parent._run(lambda: self._cursor.executemany(sql, parameters))
        return self

    def fetchone(self) -> Any:
        return self._parent._run(self._cursor.fetchone)

    def fetchmany(self, size: int | None = None) -> Any:
        if size is None:
            return self._parent._run(self._cursor.fetchmany)
        return self._parent._run(lambda: self._cursor.fetchmany(size))

    def fetchall(self) -> Any:
        return self._parent._run(self._cursor.fetchall)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


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
    deadline_seconds = DEFAULT_QUERY_DEADLINE_SECONDS
    if isinstance(target, StorageTarget):
        if auth_token is not None:
            raise ValueError("Pass auth_token via StorageTarget or the kwarg, not both.")
        if target.query_deadline_seconds is not None:
            deadline_seconds = target.query_deadline_seconds
        target, auth_token = target.as_connect_args()

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
            raw = libsql.connect(target)
        else:
            raw = libsql.connect(target, auth_token=auth_token)
        return DeadlineConnection(raw, deadline_seconds=deadline_seconds)
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
