from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vexic.storage.errors import is_retryable_operational_error

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

# Hot-path bounds for the REMOTE libSQL path only (ADR 0019 Addendum 2
# follow-up). The local SQLite path keeps sqlite3's own semantics untouched:
# retrying a local file open is meaningless, and adding latency there would
# hit the local reference service and every test.
#
# The driver's own default is 5.0s; naming it here makes the bound explicit
# and reviewable rather than inherited. Attempts are TOTAL (1 initial try + 2
# retries), so worst-case added latency is bounded by
# 2*timeout + 0.5 + 1.0 backoff -- it can never grow without limit.
LIBSQL_CONNECT_TIMEOUT_SECONDS = 5.0
LIBSQL_CONNECT_ATTEMPTS = 3
LIBSQL_CONNECT_BACKOFF_SECONDS = 0.5
LIBSQL_CONNECT_BACKOFF_MAX_SECONDS = 2.0

# Injection seam for the backoff sleep so retry tests run instantly and assert
# the schedule. Not a config surface -- callers never pass this.
_sleep = time.sleep


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

    The REMOTE path additionally gets an explicit connect timeout and a bounded
    retry/backoff (ADR 0019): without them a network degradation could hang the
    hot path indefinitely. Both apply to the libSQL branch ONLY -- the local
    SQLite branch keeps sqlite3's own semantics exactly, since retrying a local
    file open is meaningless and an injected timeout would surprise every local
    call site. A caller-supplied ``timeout`` kwarg still wins on either branch.
    """
    if isinstance(target, StorageTarget):
        if auth_token is not None:
            raise ValueError("Pass auth_token via StorageTarget or the kwarg, not both.")
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

        timeout = kwargs.pop("timeout", LIBSQL_CONNECT_TIMEOUT_SECONDS)
        if auth_token is not None:
            kwargs["auth_token"] = auth_token
        return _connect_libsql_with_retry(libsql, target, timeout=timeout, **kwargs)
    return sqlite3.connect(target, **kwargs)


def _is_retryable_connect_error(exc: BaseException) -> bool:
    """True for a remote-connect fault a bounded retry might clear.

    Two families qualify. Network/transport faults reach us as :class:`OSError`
    subclasses (:class:`ConnectionError`, :class:`TimeoutError` -- including the
    driver timeout above). Server-side busy/locked/IO conditions reach us as the
    libSQL bare :class:`ValueError` carrying a Hrana payload, which the shared
    classifier in :mod:`vexic.storage.errors` already recognizes; reusing it
    keeps one definition of "retryable" across the codebase.

    Everything else -- notably an auth failure -- is NOT retried: it cannot
    succeed on a second attempt and would only add latency to the hot path.
    """
    if isinstance(exc, OSError):
        return True
    return is_retryable_operational_error(exc)


def _connect_libsql_with_retry(libsql: Any, target: str, **kwargs: Any) -> StorageConnection:
    """Open a remote libSQL connection with a BOUNDED retry and backoff.

    Bounded means bounded (ADR 0019): at most ``LIBSQL_CONNECT_ATTEMPTS`` total
    attempts and a backoff capped at ``LIBSQL_CONNECT_BACKOFF_MAX_SECONDS``, so
    worst-case latency here is finite and small. On exhaustion the LAST
    exception is re-raised unchanged -- deliberately not wrapped in a new type,
    because the hosted adapter classifies the raw libSQL ``ValueError`` to map a
    storage fault to a 503 ``storage_unavailable``, and a wrapper type would
    silently downgrade that to a generic 500.
    """
    for attempt in range(1, LIBSQL_CONNECT_ATTEMPTS + 1):
        try:
            return libsql.connect(target, **kwargs)
        except Exception as exc:
            if attempt == LIBSQL_CONNECT_ATTEMPTS or not _is_retryable_connect_error(exc):
                raise
            backoff = min(
                LIBSQL_CONNECT_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                LIBSQL_CONNECT_BACKOFF_MAX_SECONDS,
            )
            _sleep(backoff)
    raise AssertionError("unreachable: the retry loop is bounded and always returns or raises")


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
