"""Remote libSQL query deadline (ADR 0019 Addendum 7).

A degraded or black-holed remote can hang a query indefinitely: the driver's
``timeout=`` kwarg is not a network deadline and ``connect()`` does no I/O.
``DeadlineConnection`` bounds each driver call with a wall-clock deadline.
Read-only timeouts are retryable; a timed-out mutation has an unknown outcome
and must not be retried automatically.

Deadlines here are tiny real waits (~0.05s) -- this is a genuine wall-clock
bound, the one behavior an injected clock cannot substitute for. Keep them
small so the suite stays fast.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import types
from typing import Any

import pytest

from tests.fakes.libsql import FakeLibsqlConn, HangingLibsqlConn
from vexic.storage import connection as connection_module
from vexic.storage.connection import (
    DEFAULT_QUERY_DEADLINE_SECONDS,
    DeadlineConnection,
    StorageTarget,
    connect,
)
from vexic.storage.errors import (
    MutationOutcomeUnknown,
    QueryDeadlineExceeded,
    is_operational_error,
    is_retryable_operational_error,
    is_unique_violation,
)

_TEST_DEADLINE = 0.05


@pytest.fixture
def gate():
    gate = threading.Event()
    yield gate
    # Release the abandoned worker thread so it exits with the fake.
    gate.set()


def test_remote_query_exceeding_deadline_raises_retryable_fault(gate) -> None:
    conn = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)
    with pytest.raises(QueryDeadlineExceeded) as excinfo:
        conn.execute("SELECT 1")
    assert is_retryable_operational_error(excinfo.value)


def test_query_under_deadline_returns_result_unchanged() -> None:
    conn = DeadlineConnection(FakeLibsqlConn(), deadline_seconds=5.0)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
    conn.execute("INSERT INTO t (name) VALUES (?)", ("alpha",))
    rows = conn.execute("SELECT name FROM t").fetchall()
    assert rows == [("alpha",)]


def test_timed_out_connection_is_poisoned_and_fails_fast(gate) -> None:
    fake = HangingLibsqlConn(gate)
    conn = DeadlineConnection(fake, deadline_seconds=_TEST_DEADLINE)
    with pytest.raises(QueryDeadlineExceeded):
        conn.execute("SELECT 1")

    # Subsequent calls fail immediately -- the hung stream is never reused.
    start = time.monotonic()
    with pytest.raises(QueryDeadlineExceeded):
        conn.execute("SELECT 1")
    with pytest.raises(QueryDeadlineExceeded):
        conn.commit()
    assert time.monotonic() - start < _TEST_DEADLINE

    # close() must not touch the underlying conn -- it could hang too. All
    # call sites use ``with closing(connect(...))``, so this must be safe.
    conn.close()
    assert fake.close_calls == 0


def test_timeout_inside_transaction_context_propagates_cleanly(gate) -> None:
    conn = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)
    # ``with conn:`` must not raise a second error from __exit__ against the
    # poisoned connection; the execute's own timeout is what propagates.
    with pytest.raises(QueryDeadlineExceeded, match="exceeded"):
        with conn:
            conn.execute("SELECT 1")


def test_executemany_exceeding_deadline_has_nonretryable_unknown_outcome(gate) -> None:
    conn = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)
    with pytest.raises(MutationOutcomeUnknown) as excinfo:
        conn.executemany("INSERT INTO t VALUES (?)", [("a",)])
    assert is_operational_error(excinfo.value)
    assert not is_retryable_operational_error(excinfo.value)


@pytest.mark.parametrize(
    "sql",
    [
        "-- request trace\nINSERT INTO t VALUES ('late')",
        "/* request trace */\n-- second trace\nUPDATE t SET value = 'late'",
    ],
)
def test_comment_prefixed_mutation_timeout_has_unknown_outcome(sql, gate) -> None:
    conn = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)

    with pytest.raises(MutationOutcomeUnknown):
        conn.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA foreign_keys=ON",
        "/* connection setup */ PRAGMA main.foreign_keys = ON",
    ],
)
def test_foreign_keys_pragma_timeout_remains_retryable(sql, gate) -> None:
    conn = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)

    with pytest.raises(QueryDeadlineExceeded) as excinfo:
        conn.execute(sql)

    assert is_retryable_operational_error(excinfo.value)


def test_durable_pragma_assignment_timeout_has_unknown_outcome(gate) -> None:
    conn = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)

    with pytest.raises(MutationOutcomeUnknown) as excinfo:
        conn.execute("-- configure storage\nPRAGMA journal_mode=WAL")

    assert not is_retryable_operational_error(excinfo.value)


def test_mutation_can_land_after_unknown_outcome_is_reported(gate) -> None:
    landed = threading.Event()

    class _LateMutationConn(FakeLibsqlConn):
        def __init__(self) -> None:
            super().__init__()
            self._conn.execute("CREATE TABLE t (value TEXT)")

        def execute(self, sql, parameters=(), /):
            if sql.lstrip().upper().startswith("INSERT"):
                gate.wait()
                cursor = super().execute(sql, parameters)
                landed.set()
                return cursor
            return super().execute(sql, parameters)

    fake = _LateMutationConn()
    conn = DeadlineConnection(fake, deadline_seconds=_TEST_DEADLINE)

    with pytest.raises(MutationOutcomeUnknown, match="must not be retried") as excinfo:
        conn.execute("INSERT INTO t VALUES (?)", ("late",))
    assert not is_retryable_operational_error(excinfo.value)

    # The wrapper cannot cancel the driver's in-flight operation. Prove why
    # callers receive the distinct outcome-unknown fault instead of a
    # retryable read deadline.
    gate.set()
    assert landed.wait(1.0)
    assert fake._conn.execute("SELECT value FROM t").fetchall() == [("late",)]


def test_outstanding_worker_cap_prevents_unbounded_threads(monkeypatch, gate) -> None:
    limited_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(connection_module, "_REMOTE_CALL_SLOTS", limited_slots)

    first = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)
    with pytest.raises(QueryDeadlineExceeded):
        first.execute("SELECT 1")

    class _CountingConn(FakeLibsqlConn):
        calls = 0

        def execute(self, sql, parameters=(), /):
            self.calls += 1
            return super().execute(sql, parameters)

    second_fake = _CountingConn()
    second = DeadlineConnection(second_fake, deadline_seconds=_TEST_DEADLINE)
    with pytest.raises(QueryDeadlineExceeded, match="capacity.*before.*started"):
        second.execute("SELECT 1")
    assert second_fake.calls == 0

    start = time.monotonic()
    second.close()
    assert time.monotonic() - start < _TEST_DEADLINE


def test_hanging_cursor_execute_times_out_and_poisons_connection(gate) -> None:
    fake = HangingLibsqlConn(gate)
    conn = DeadlineConnection(fake, deadline_seconds=_TEST_DEADLINE)
    cursor = conn.cursor()
    with pytest.raises(QueryDeadlineExceeded):
        cursor.execute("SELECT 1")
    # The parent connection shares the dead stream: poisoned too.
    with pytest.raises(QueryDeadlineExceeded, match="abandoned"):
        conn.execute("SELECT 1")


def test_execute_returns_deadline_bounded_cursor(gate) -> None:
    # ``conn.execute(...).fetchall()`` is the dominant call-site pattern; any
    # remote work deferred until fetch must run under the same deadline.
    class _HangingFetchConn(FakeLibsqlConn):
        def execute(self, sql, parameters=(), /):
            cursor = super().execute(sql, parameters)

            class _LazyFetchCursor:
                def fetchall(inner) -> Any:
                    gate.wait()
                    return cursor.fetchall()

                def __getattr__(inner, name):
                    return getattr(cursor, name)

            return _LazyFetchCursor()

    conn = DeadlineConnection(_HangingFetchConn(), deadline_seconds=_TEST_DEADLINE)
    with pytest.raises(QueryDeadlineExceeded):
        conn.execute("SELECT 1").fetchall()


def test_cursor_under_deadline_round_trips() -> None:
    conn = DeadlineConnection(FakeLibsqlConn(), deadline_seconds=5.0)
    conn.execute("CREATE TABLE t (name TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", ("alpha",))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM t")
    assert cursor.fetchall() == [("alpha",)]


def _patch_libsql_module(monkeypatch, driver_conn) -> None:
    """Install a stub ``libsql`` module so ``connect()``'s lazy import sees it."""
    stub = types.ModuleType("libsql")
    stub.connect = lambda target, **kwargs: driver_conn
    monkeypatch.setitem(sys.modules, "libsql", stub)


def test_local_sqlite_target_returns_raw_connection() -> None:
    conn = connect(":memory:")
    assert isinstance(conn, sqlite3.Connection)
    assert not isinstance(conn, DeadlineConnection)


def test_remote_libsql_target_returns_deadline_wrapped_connection(monkeypatch) -> None:
    _patch_libsql_module(monkeypatch, FakeLibsqlConn())
    conn = connect("libsql://example.turso.io", auth_token="tok")
    assert isinstance(conn, DeadlineConnection)
    assert conn._deadline_seconds == DEFAULT_QUERY_DEADLINE_SECONDS


def test_storage_target_deadline_overrides_default(monkeypatch, gate) -> None:
    _patch_libsql_module(monkeypatch, HangingLibsqlConn(gate))
    target = StorageTarget(
        "libsql://example.turso.io",
        auth_token="tok",
        query_deadline_seconds=_TEST_DEADLINE,
    )
    conn = connect(target)
    with pytest.raises(QueryDeadlineExceeded):
        conn.execute("SELECT 1")


def test_wrapped_call_exceptions_propagate_unchanged() -> None:
    conn = DeadlineConnection(FakeLibsqlConn(), deadline_seconds=5.0)
    conn.execute("CREATE TABLE t (name TEXT UNIQUE)")
    conn.execute("INSERT INTO t (name) VALUES (?)", ("alpha",))
    with pytest.raises(Exception) as excinfo:
        conn.execute("INSERT INTO t (name) VALUES (?)", ("alpha",))
    assert is_unique_violation(excinfo.value)
