from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from vexic.storage.errors import (
    MutationOutcomeUnknown,
    QueryDeadlineExceeded,
    is_retryable_operational_error,
)


# A permanently black-holed driver call cannot be cancelled safely. Cap the
# number of workers that can be abandoned process-wide so repeated requests do
# not create an unbounded number of daemon threads and retained connections.
# Slots return automatically if the remote eventually responds.
MAX_OUTSTANDING_REMOTE_CALLS = 64
_REMOTE_CALL_SLOTS = threading.BoundedSemaphore(MAX_OUTSTANDING_REMOTE_CALLS)

_MUTATING_SQL_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "ATTACH",
        "COMMIT",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "END",
        "INSERT",
        "REINDEX",
        "RELEASE",
        "REPLACE",
        "TRUNCATE",
        "UPDATE",
        "VACUUM",
    }
)
_RETRYABLE_CONNECTION_LOCAL_PRAGMAS = frozenset({"FOREIGN_KEYS"})


def _strip_leading_sql_comments(sql: str) -> str:
    """Remove leading SQLite line/block comments before statement routing."""
    index = 0
    length = len(sql)
    while True:
        while index < length and sql[index].isspace():
            index += 1
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            if newline < 0:
                return ""
            index = newline + 1
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            if comment_end < 0:
                return ""
            index = comment_end + 2
            continue
        return sql[index:]


def _pragma_may_mutate_durably(normalized: str) -> bool:
    """Classify assigned PRAGMAs, excluding safe abandoned-connection state."""
    body = normalized.removeprefix("PRAGMA").strip()
    if "=" not in body:
        return False
    assignment_target = body.split("=", 1)[0].strip().split()
    if not assignment_target:
        # Malformed assignment SQL should reach the driver for its real error,
        # but if that call hangs, conservatively avoid an automatic retry.
        return True
    pragma_name = assignment_target[-1]
    pragma_name = pragma_name.rsplit(".", 1)[-1]
    return pragma_name not in _RETRYABLE_CONNECTION_LOCAL_PRAGMAS


def _sql_may_mutate(sql: str) -> bool:
    """Conservatively classify SQL whose timeout leaves a durable ambiguity."""
    normalized = " ".join(_strip_leading_sql_comments(sql).upper().split())
    if not normalized:
        return False
    keyword = normalized.partition(" ")[0]
    if keyword in _MUTATING_SQL_KEYWORDS:
        return True
    if keyword == "PRAGMA":
        # `foreign_keys` changes only this connection. Once a timeout poisons
        # and abandons that connection, repeating it on a fresh connection is
        # safe. Other assigned PRAGMAs remain conservative unknown outcomes.
        return _pragma_may_mutate_durably(normalized)
    if keyword == "WITH":
        # A CTE may prefix DML. This errs toward preventing an unsafe retry;
        # normal SELECT CTEs remain read-only unless they contain a DML verb.
        words = set(normalized.replace("(", " ").replace(")", " ").split())
        return bool(words & {"INSERT", "UPDATE", "DELETE", "REPLACE"})
    return False


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

    The driver's ``timeout=`` kwarg is not a network deadline and
    ``libsql.connect()`` does no I/O (ADR 0019 Addendum 6; Vexic's ``connect()``
    seam adds one round-trip via the Addendum 8 readiness probe), so a degraded
    remote can hang any query round-trip indefinitely. Each call runs on a daemon worker thread; if it
    outruns ``deadline_seconds`` the caller gets :class:`QueryDeadlineExceeded`
    -- a retryable storage fault -- and the hung call is abandoned (the daemon
    thread dies with the process rather than blocking interpreter shutdown,
    which a ``ThreadPoolExecutor``'s non-daemon threads would). Outstanding
    workers are capped process-wide. A mutating call that times out raises
    :class:`MutationOutcomeUnknown`, because the driver cannot prove whether
    that mutation committed after the caller's deadline.
    """

    def __init__(self, conn: Any, *, deadline_seconds: float) -> None:
        self._conn = conn
        self._deadline_seconds = deadline_seconds
        self._poisoned = False

    def _run(
        self,
        call: Callable[[], Any],
        *,
        mutation_outcome_unknown: bool = False,
        wait_for_worker_capacity: bool = True,
    ) -> Any:
        if self._poisoned:
            raise QueryDeadlineExceeded(
                "remote libSQL connection was abandoned after a query deadline "
                "timeout; open a fresh connection"
            )

        started_at = time.monotonic()
        worker_slots = _REMOTE_CALL_SLOTS
        capacity_timeout = self._deadline_seconds if wait_for_worker_capacity else 0
        if not worker_slots.acquire(timeout=capacity_timeout):
            # No driver call started, so even a would-be mutation is safe to
            # retry on a fresh request.
            raise QueryDeadlineExceeded(
                "remote libSQL worker capacity was exhausted before the call "
                "started; retry later"
            )
        if time.monotonic() - started_at >= self._deadline_seconds:
            worker_slots.release()
            raise QueryDeadlineExceeded(
                "remote libSQL query deadline elapsed before the call started; "
                "retry later"
            )

        outcome: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                outcome["result"] = call()
            except BaseException as exc:  # noqa: BLE001 - re-raised in caller
                outcome["error"] = exc
            finally:
                worker_slots.release()
                done.set()

        thread = threading.Thread(
            target=worker,
            name="vexic-libsql-deadline",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            worker_slots.release()
            raise

        remaining = max(0.0, self._deadline_seconds - (time.monotonic() - started_at))
        if not done.wait(remaining):
            self._poisoned = True
            if mutation_outcome_unknown:
                raise MutationOutcomeUnknown(
                    "remote libSQL mutation exceeded its query deadline; the "
                    "outcome is unknown and must not be retried automatically"
                )
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
            self._run(
                lambda: self._conn.execute(sql, parameters),
                mutation_outcome_unknown=_sql_may_mutate(sql),
            ),
            self,
        )

    def executemany(self, sql: str, parameters: Any, /) -> "DeadlineCursor":
        return DeadlineCursor(
            self._run(
                lambda: self._conn.executemany(sql, parameters),
                mutation_outcome_unknown=_sql_may_mutate(sql),
            ),
            self,
        )

    def cursor(self) -> "DeadlineCursor":
        # Cursors execute independently and share the connection's stream, so
        # they run under the same deadline and poison the whole connection.
        return DeadlineCursor(self._run(self._conn.cursor), self)

    def commit(self) -> None:
        self._run(self._conn.commit, mutation_outcome_unknown=True)

    def rollback(self) -> None:
        self._run(self._conn.rollback)

    def close(self) -> None:
        # A poisoned connection's underlying close() could hang on the same
        # dead remote; drop the reference instead. Callers universally use
        # ``with closing(connect(...))``, so close() must never raise or block.
        if self._poisoned:
            return
        try:
            # If every slot belongs to an abandoned worker, cleanup must not
            # impose a second full deadline after the request already failed.
            self._run(self._conn.close, wait_for_worker_capacity=False)
        except QueryDeadlineExceeded:
            # Closing is cleanup, not a request operation. The process-wide
            # worker cap contains an unresponsive close just like any other
            # remote call, and callers must not lose a successful result to it.
            return

    def __enter__(self) -> Any:
        self._run(self._conn.__enter__)
        return self

    def __exit__(self, *exc: Any) -> Any:
        # After a timeout the rollback-on-exit round-trip would hang on the
        # same dead remote; skip it and let the timeout itself propagate.
        if self._poisoned:
            return False
        return self._run(
            lambda: self._conn.__exit__(*exc),
            mutation_outcome_unknown=not exc or exc[0] is None,
        )


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
        self._parent._run(
            lambda: self._cursor.execute(sql, parameters),
            mutation_outcome_unknown=_sql_may_mutate(sql),
        )
        return self

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        self._parent._run(
            lambda: self._cursor.executemany(sql, parameters),
            mutation_outcome_unknown=_sql_may_mutate(sql),
        )
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

        # Readiness probe with one rebuild: ``libsql.connect()`` is
        # lazy, so a transient edge fault -- e.g. the Hrana 502 ``connect to
        # upstream failed`` observed live 2026-07-13 -- first surfaces on the
        # connection's first round-trip. Absorb one classified retryable fault
        # by discarding the handle and rebuilding on a fresh one; a second
        # fault propagates. The probe runs through DeadlineConnection so a
        # hung remote raises QueryDeadlineExceeded here rather than hanging
        # the caller's first business statement.
        for attempt in (0, 1):
            if auth_token is None:
                raw = libsql.connect(target)
            else:
                raw = libsql.connect(target, auth_token=auth_token)
            conn = DeadlineConnection(raw, deadline_seconds=deadline_seconds)
            try:
                conn.execute("SELECT 1").fetchone()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    conn.close()
                # A hung remote (QueryDeadlineExceeded) is not fixed by an
                # immediate rebuild; retrying would double the caller's wait
                # for the same outcome. Rebuild only on fast-fail faults.
                if (
                    attempt == 0
                    and not isinstance(exc, QueryDeadlineExceeded)
                    and is_retryable_operational_error(exc)
                ):
                    continue
                raise
            return conn
        raise AssertionError("unreachable")
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
