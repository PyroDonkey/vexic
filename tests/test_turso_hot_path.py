"""Bounded eviction for the per-tenant Turso token cache (ADR 0019 Addendum 6).

Addendum 2 recorded ``TenantTokenCache`` as "an unbounded in-process dict" with
no size-bounded eviction; Addendum 6 supersedes it and records the bound landing.
TTL alone does not bound the cache: an expired entry is never *served*, but it is
only dropped when that same ``db_name`` is asked for again, so a process that sees
a long tail of tenants would retain an entry per tenant forever. TTL governs
freshness; the bound governs size. These tests pin both, and pin that neither
cannibalizes the other.

Hermetic: the provisioning port and TTL clock are fakes, so nothing here touches
the network or mints a real token. Contention tests use short real monotonic waits
to verify the synchronization deadline itself.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from adapters.turso_adapter import TenantTokenCache
from vexic.storage.errors import (
    QueryDeadlineExceeded,
    is_retryable_operational_error,
)


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


@pytest.mark.parametrize("wait_timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_mint_wait_timeout_must_be_finite_and_positive(wait_timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        TenantTokenCache(FakePort(), mint_wait_timeout_seconds=wait_timeout)


def test_cache_hit_and_concurrent_eviction_are_atomic():
    """A hit cannot be evicted between lookup and its LRU promotion."""

    class BlockingClock:
        def __init__(self) -> None:
            self.block = False
            self.entered = threading.Event()
            self.release = threading.Event()

        def __call__(self) -> float:
            if self.block:
                self.entered.set()
                assert self.release.wait(timeout=2)
            return 0.0

    clock = BlockingClock()
    cache = TenantTokenCache(FakePort(), clock=clock, max_entries=1)
    hot = cache.get_token("hot")
    clock.block = True
    insert_started = threading.Event()

    def insert_cold() -> str:
        insert_started.set()
        return cache.get_token("cold")

    with ThreadPoolExecutor(max_workers=2) as executor:
        hit = executor.submit(cache.get_token, "hot")
        assert clock.entered.wait(timeout=2)
        insert = executor.submit(insert_cold)
        assert insert_started.wait(timeout=2)
        # The eviction waits for the in-progress hit's atomic LRU promotion.
        assert not insert.done()
        clock.block = False
        clock.release.set()
        assert hit.result(timeout=2) == hot
        assert insert.result(timeout=2).startswith("jwt-cold-")

    assert len(cache) == 1


def test_same_key_follower_wait_is_bounded_and_retryable():
    class BlockingPort(FakePort):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def mint_token(
            self,
            db_name: str,
            *,
            expiration: str = "5m",
            read_only: bool = True,
        ) -> str:
            self.entered.set()
            assert self.release.wait(timeout=2)
            return super().mint_token(
                db_name,
                expiration=expiration,
                read_only=read_only,
            )

    port = BlockingPort()
    cache = TenantTokenCache(port, mint_wait_timeout_seconds=0.05)

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(cache.get_token, "tenant-a")
        assert port.entered.wait(timeout=2)
        started_at = time.monotonic()
        try:
            with pytest.raises(QueryDeadlineExceeded) as excinfo:
                cache.get_token("tenant-a")
        finally:
            port.release.set()

        assert time.monotonic() - started_at < 0.5
        assert is_retryable_operational_error(excinfo.value)
        assert owner.result(timeout=2) == "jwt-tenant-a-1"


def test_invalidate_during_mint_discards_result_and_remints():
    class InvalidatablePort(FakePort):
        def __init__(self) -> None:
            super().__init__()
            self.first_mint_started = threading.Event()
            self.release_first_mint = threading.Event()

        def mint_token(
            self,
            db_name: str,
            *,
            expiration: str = "5m",
            read_only: bool = True,
        ) -> str:
            if self._counter == 0:
                self.first_mint_started.set()
                assert self.release_first_mint.wait(timeout=2)
            return super().mint_token(
                db_name,
                expiration=expiration,
                read_only=read_only,
            )

    port = InvalidatablePort()
    cache = TenantTokenCache(port)

    with ThreadPoolExecutor(max_workers=1) as executor:
        token_future = executor.submit(cache.get_token, "tenant-a")
        assert port.first_mint_started.wait(timeout=2)
        cache.invalidate("tenant-a")
        port.release_first_mint.set()
        assert token_future.result(timeout=2) == "jwt-tenant-a-2"

    assert port.calls == ["tenant-a", "tenant-a"]
    assert cache.get_token("tenant-a") == "jwt-tenant-a-2"
    assert port.calls == ["tenant-a", "tenant-a"]
