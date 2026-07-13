"""Bounded eviction for the per-tenant Turso token cache (ADR 0019 Addendum 2).

Addendum 2 records ``TenantTokenCache`` as "an unbounded in-process dict" with
no size-bounded eviction. TTL alone does not bound it: an expired entry is never
*served*, but it is only dropped when that same ``db_name`` is asked for again,
so a process that sees a long tail of tenants retains an entry per tenant
forever. TTL governs freshness; the bound governs size. These tests pin both,
and pin that neither cannibalizes the other.

Hermetic: the provisioning port and the clock are fakes, so nothing here touches
the network, mints a real token, or reads a wall clock.
"""

from __future__ import annotations

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
