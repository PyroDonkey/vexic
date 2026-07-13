"""Hot-path hardening for the Turso/libSQL adapter (ADR 0019 Addendum 2).

Two gaps ADR 0019's real-Turso verification spike recorded as fix-soon items,
closed here:

1. ``TenantTokenCache`` had TTL expiry but no size bound -- an unbounded
   in-process dict that grows with tenant count.
2. ``connect()`` had no explicit timeout and no retry/backoff on the hot path
   against remote libSQL, risking a hang under network degradation.

Every test here is hermetic: the libSQL driver is faked at the lazy
``import libsql`` boundary and the clock/sleep are injected, so nothing in this
file touches the network or sleeps in real time.
"""

from __future__ import annotations

import sqlite3
import sys
import types

import pytest

from adapters.turso_adapter import TenantTokenCache


class FakeClock:
    """Controllable clock. Starts at 0.0, advances only on `advance()`."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakePort:
    """Counts `mint_token` calls and returns a distinguishable fake jwt."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._counter = 0

    def mint_token(self, db_name: str, *, expiration: str = "5m", read_only: bool = True) -> str:
        self._counter += 1
        self.calls.append(db_name)
        return f"jwt-{db_name}-{self._counter}"


def test_cache_size_never_exceeds_its_bound_under_sustained_inserts():
    """Sustained distinct tenants must not grow the cache without limit."""
    port = FakePort()
    cache = TenantTokenCache(port, ttl_seconds=600, clock=FakeClock(), max_entries=4)

    for index in range(100):
        cache.get_token(f"tenant-{index}")
        assert len(cache) <= 4

    assert len(cache) == 4


def test_cache_evicts_least_recently_used_and_keeps_the_recently_used():
    """Eviction is LRU, not arbitrary: a token that keeps being used survives
    a flood of new tenants, so the hot tenant does not get evicted by cold ones.
    """
    port = FakePort()
    cache = TenantTokenCache(port, ttl_seconds=600, clock=FakeClock(), max_entries=2)

    hot = cache.get_token("hot")
    cache.get_token("cold")
    # Touch `hot` so `cold` becomes the least-recently-used entry.
    assert cache.get_token("hot") == hot
    cache.get_token("new")  # evicts `cold`, not `hot`

    assert port.calls == ["hot", "cold", "new"]
    # `hot` is still cached -- served without a re-mint.
    assert cache.get_token("hot") == hot
    assert port.calls == ["hot", "cold", "new"]
    # `cold` was evicted -- asking again re-mints.
    cache.get_token("cold")
    assert port.calls == ["hot", "cold", "new", "cold"]


def test_expired_entry_is_never_served_even_while_within_the_size_bound():
    """The size bound must not cannibalize TTL: a token past its TTL is
    re-minted, never handed out stale, even when the cache is nowhere near full.
    """
    port = FakePort()
    clock = FakeClock()
    cache = TenantTokenCache(port, ttl_seconds=600, clock=clock, max_entries=64)

    first = cache.get_token("tenant-a")
    clock.advance(601)
    second = cache.get_token("tenant-a")

    assert second != first
    assert port.calls == ["tenant-a", "tenant-a"]
    assert len(cache) == 1


def test_max_entries_must_be_at_least_one():
    with pytest.raises(ValueError):
        TenantTokenCache(FakePort(), max_entries=0)


# --------------------------------------------------------------------------
# connect() timeout + bounded retry (remote libSQL path only)
# --------------------------------------------------------------------------

REMOTE = "libsql://tenant-a.turso.io"
LOCAL = ":memory:"


class FakeLibsql:
    """Stands in for the lazily-imported `libsql` module.

    Records every `connect` call and replays a scripted sequence of outcomes:
    an exception instance is raised, anything else is returned as the
    connection. No network.
    """

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def connect(self, database: str, **kwargs: object) -> object:
        self.calls.append({"database": database, **kwargs})
        outcome = self._outcomes.pop(0) if self._outcomes else "connection"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def fake_libsql(monkeypatch):
    """Installs a FakeLibsql at the `import libsql` boundary inside connect()."""

    def install(*outcomes: object) -> FakeLibsql:
        fake = FakeLibsql(*outcomes)
        module = types.ModuleType("libsql")
        module.connect = fake.connect  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "libsql", module)
        return fake

    return install


@pytest.fixture
def no_sleep(monkeypatch):
    """Records backoff sleeps instead of actually sleeping. Keeps the retry
    tests instant and makes the backoff schedule assertable."""
    from vexic.storage import connection

    slept: list[float] = []
    monkeypatch.setattr(connection, "_sleep", slept.append)
    return slept


def test_remote_connect_passes_an_explicit_timeout(fake_libsql):
    """A remote libSQL connect must not inherit an implicit driver default --
    the timeout is what turns a network hang into a raised error."""
    from vexic.storage.connection import LIBSQL_CONNECT_TIMEOUT_SECONDS, connect

    fake = fake_libsql()

    connect(REMOTE, auth_token="token-xyz")

    assert len(fake.calls) == 1
    assert fake.calls[0]["timeout"] == LIBSQL_CONNECT_TIMEOUT_SECONDS
    assert fake.calls[0]["auth_token"] == "token-xyz"


def test_remote_connect_retries_a_transient_failure_then_succeeds(fake_libsql, no_sleep):
    """A transient network/IO fault is retried rather than surfaced."""
    from vexic.storage.connection import connect

    fake = fake_libsql(ConnectionError("connection reset by peer"), "live-connection")

    conn = connect(REMOTE, auth_token="token-xyz")

    assert conn == "live-connection"
    assert len(fake.calls) == 2
    assert no_sleep == [0.5]  # one backoff between the two attempts


def test_remote_connect_retry_is_bounded_to_an_exact_attempt_count(fake_libsql, no_sleep):
    """The retry bound is a hard stop. Asserting the EXACT call count means an
    infinite-retry regression fails loudly here rather than hanging production.
    """
    from vexic.storage.connection import LIBSQL_CONNECT_ATTEMPTS, connect

    boom = TimeoutError("libsql connect timed out")
    # More failures queued than attempts allowed: if connect() retried without
    # a bound it would keep consuming these and never raise.
    fake = fake_libsql(*[boom] * 25)

    with pytest.raises(TimeoutError, match="libsql connect timed out"):
        connect(REMOTE, auth_token="token-xyz")

    assert LIBSQL_CONNECT_ATTEMPTS == 3
    assert len(fake.calls) == LIBSQL_CONNECT_ATTEMPTS  # exactly 3, never more
    # Backoff is bounded and finite too -- one sleep between each attempt.
    assert no_sleep == [0.5, 1.0]
    assert sum(no_sleep) <= 5.0


def test_remote_connect_surfaces_a_clear_error_after_exhausting_retries(fake_libsql, no_sleep):
    """Exhaustion must raise the underlying fault, not hang and not swallow it.

    The original exception is re-raised (rather than wrapped in a new type) so
    the existing hosted classifiers keep working: `_value_error_response` in
    `hosted_http.py` maps a retryable libSQL `ValueError` to a 503
    `storage_unavailable`, and a new exception type would silently break that
    mapping into a generic 500.
    """
    from vexic.storage.connection import connect

    hrana_locked = ValueError('Hrana: `Error { message: "database is locked" }`')
    fake = fake_libsql(hrana_locked, hrana_locked, hrana_locked)

    with pytest.raises(ValueError, match="database is locked") as caught:
        connect(REMOTE, auth_token="token-xyz")

    assert caught.value is hrana_locked
    assert len(fake.calls) == 3


def test_remote_connect_does_not_retry_a_non_retryable_failure(fake_libsql, no_sleep):
    """An auth failure is not transient. Retrying it burns latency on the hot
    path and cannot succeed, so it must surface on the first attempt.
    """
    from vexic.storage.connection import connect

    unauthorized = ValueError("Hrana: `Error { message: \"401 Unauthorized\" }`")
    fake = fake_libsql(unauthorized, "never-reached")

    with pytest.raises(ValueError, match="401 Unauthorized"):
        connect(REMOTE, auth_token="bad-token")

    assert len(fake.calls) == 1  # no retry
    assert no_sleep == []  # no backoff


def test_local_sqlite_path_is_untouched_by_timeout_and_retry(monkeypatch, no_sleep):
    """The seam is shared with local SQLite. Retrying a local file open is
    meaningless and an injected timeout would surprise every local call site --
    including the local reference service and the whole test suite. So the local
    branch must add NO retry and NO timeout of its own.
    """
    from vexic.storage import connection

    calls: list[tuple] = []
    real_sqlite_connect = connection.sqlite3.connect

    def recording_connect(target, **kwargs):
        calls.append((target, kwargs))
        return real_sqlite_connect(target, **kwargs)

    monkeypatch.setattr(connection.sqlite3, "connect", recording_connect)

    conn = connection.connect(LOCAL)
    conn.close()

    assert calls == [(LOCAL, {})]  # no timeout injected, kwargs untouched
    assert no_sleep == []  # no backoff on the local path


def test_local_sqlite_failure_is_not_retried(monkeypatch, no_sleep):
    """A failing local open raises immediately -- exactly once, no backoff."""
    from vexic.storage import connection

    attempts: list[str] = []

    def failing_connect(target, **kwargs):
        attempts.append(target)
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(connection.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError):
        connection.connect("/nonexistent/dir/db.sqlite")

    assert len(attempts) == 1  # not retried, despite being a "retryable" marker
    assert no_sleep == []


def test_explicit_caller_timeout_overrides_the_remote_default(fake_libsql):
    """The seam already accepts **kwargs; an explicit timeout still wins."""
    from vexic.storage.connection import connect

    fake = fake_libsql()

    connect(REMOTE, auth_token="token-xyz", timeout=30.0)

    assert fake.calls[0]["timeout"] == 30.0
