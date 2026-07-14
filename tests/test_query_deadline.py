"""Remote libSQL query deadline (ADR 0019 Addendum 7).

A degraded or black-holed remote can hang a query indefinitely: the driver's
``timeout=`` kwarg is not a network deadline and ``libsql.connect()`` does no
I/O (Vexic's ``connect()`` now performs one, via the readiness probe of ADR
0019 Addendum 8). ``DeadlineConnection`` bounds each driver call with a
wall-clock deadline.
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


def test_clean_transaction_exit_timeout_has_nonretryable_unknown_outcome(gate) -> None:
    """A commit that may still land must never come back retryable.

    ``with conn:`` commits on a clean exit, so a timeout there leaves the write
    in the same unknown state an ``executemany`` timeout does. This is the one
    place the outcome is decided by the context-manager state rather than the
    SQL text -- classify it retryable and a client retry double-writes.
    """
    conn = DeadlineConnection(HangingLibsqlConn(gate), deadline_seconds=_TEST_DEADLINE)
    with pytest.raises(MutationOutcomeUnknown) as excinfo:
        with conn:
            pass
    assert is_operational_error(excinfo.value)
    assert not is_retryable_operational_error(excinfo.value)


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
    # The readiness probe runs under the overridden deadline, so a
    # hung remote surfaces QueryDeadlineExceeded at connect() itself instead of
    # hanging the caller's first business statement.
    _patch_libsql_module(monkeypatch, HangingLibsqlConn(gate))
    target = StorageTarget(
        "libsql://example.turso.io",
        auth_token="tok",
        query_deadline_seconds=_TEST_DEADLINE,
    )
    with pytest.raises(QueryDeadlineExceeded):
        connect(target)


def test_hung_probe_fails_after_a_single_deadline_window(monkeypatch, gate) -> None:
    # A hung remote is not fixed by an immediate rebuild, so the probe must
    # not retry QueryDeadlineExceeded: the caller waits one deadline window,
    # not two.
    connect_calls = []

    def fake_driver_connect(target, **kwargs):
        connect_calls.append(target)
        return HangingLibsqlConn(gate)

    stub = types.ModuleType("libsql")
    stub.connect = fake_driver_connect
    monkeypatch.setitem(sys.modules, "libsql", stub)

    target = StorageTarget(
        "libsql://example.turso.io",
        auth_token="tok",
        query_deadline_seconds=_TEST_DEADLINE,
    )
    with pytest.raises(QueryDeadlineExceeded):
        connect(target)

    assert len(connect_calls) == 1


class _ProbeFaultLibsqlConn:
    """Driver-level fake whose every execute raises a given fault."""

    def __init__(self, fault: BaseException) -> None:
        self._fault = fault
        self.closed = False

    def execute(self, sql, parameters=(), /):
        raise self._fault

    def close(self) -> None:
        self.closed = True


def _upstream_502() -> ValueError:
    return ValueError(
        "Hrana: `api error: `status=502 Bad Gateway, "
        'body={"error":"connect to upstream failed"}``'
    )


def _patch_libsql_sequence(monkeypatch, driver_conns) -> list:
    """Stub ``libsql`` handing out ``driver_conns`` in order; returns the log."""
    handed_out: list = []

    def fake_connect(target, **kwargs):
        handed_out.append(driver_conns[len(handed_out)])
        return handed_out[-1]

    stub = types.ModuleType("libsql")
    stub.connect = fake_connect
    monkeypatch.setitem(sys.modules, "libsql", stub)
    return handed_out


def test_remote_connect_probe_retries_once_on_transient_fault(monkeypatch) -> None:
    # The readiness probe absorbs one transient edge fault (e.g. the
    # Hrana 502 upstream-connect failure) by rebuilding on a fresh handle.
    first = _ProbeFaultLibsqlConn(_upstream_502())
    second = FakeLibsqlConn()
    handed_out = _patch_libsql_sequence(monkeypatch, [first, second])

    conn = connect("libsql://example.turso.io", auth_token="tok")

    assert len(handed_out) == 2
    assert first.closed is True
    assert isinstance(conn, DeadlineConnection)
    conn.execute("CREATE TABLE t (v TEXT)")


class _LazyFetchFaultLibsqlConn:
    """Driver-level fake whose execute succeeds but whose cursor faults on
    fetch — a lazy-materializing driver shape."""

    def __init__(self, fault: BaseException) -> None:
        self._fault = fault
        self.closed = False

    def execute(self, sql, parameters=(), /):
        fault = self._fault

        class _LazyCursor:
            def fetchone(self):
                raise fault

        return _LazyCursor()

    def close(self) -> None:
        self.closed = True


def test_remote_connect_probe_covers_lazy_fetch_fault(monkeypatch) -> None:
    # The probe materializes its result (fetchone), so a driver that defers
    # the round-trip to fetch still surfaces the transient fault inside the
    # probe — and gets the same one-rebuild recovery.
    first = _LazyFetchFaultLibsqlConn(_upstream_502())
    second = FakeLibsqlConn()
    handed_out = _patch_libsql_sequence(monkeypatch, [first, second])

    conn = connect("libsql://example.turso.io", auth_token="tok")

    assert len(handed_out) == 2
    assert first.closed is True
    conn.execute("CREATE TABLE t (v TEXT)")


def test_remote_connect_probe_second_transient_fault_propagates(monkeypatch) -> None:
    first = _ProbeFaultLibsqlConn(_upstream_502())
    second = _ProbeFaultLibsqlConn(_upstream_502())
    handed_out = _patch_libsql_sequence(monkeypatch, [first, second])

    with pytest.raises(ValueError, match="connect to upstream failed"):
        connect("libsql://example.turso.io", auth_token="tok")

    assert len(handed_out) == 2
    assert first.closed is True
    assert second.closed is True


def test_remote_connect_probe_does_not_retry_nonretryable_fault(monkeypatch) -> None:
    fault = ValueError(
        'Hrana: `api error: `status=404 Not Found, body={"error":"database not found"}``'
    )
    first = _ProbeFaultLibsqlConn(fault)
    handed_out = _patch_libsql_sequence(monkeypatch, [first, FakeLibsqlConn()])

    with pytest.raises(ValueError, match="database not found"):
        connect("libsql://example.turso.io", auth_token="tok")

    assert len(handed_out) == 1
    assert first.closed is True


def test_wrapped_call_exceptions_propagate_unchanged() -> None:
    conn = DeadlineConnection(FakeLibsqlConn(), deadline_seconds=5.0)
    conn.execute("CREATE TABLE t (name TEXT UNIQUE)")
    conn.execute("INSERT INTO t (name) VALUES (?)", ("alpha",))
    with pytest.raises(Exception) as excinfo:
        conn.execute("INSERT INTO t (name) VALUES (?)", ("alpha",))
    assert is_unique_violation(excinfo.value)
