"""Tests for the in-server per-tenant dream sweeper (ADR 0030).

The sweeper is the thin periodic loop over the machinery that already
shipped with the trigger endpoint: pre-bound execution, per-(tenant, agent)
in-flight dedup, worker-thread event-loop isolation, and per-tenant budgets.
These tests drive `DreamSweeper.tick` directly with a fixed clock; background
jobs are awaited via `service._background_tasks` exactly like the trigger
endpoint's own tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import DreamPhase
from vexic import hosted
from vexic.hosted import HostedMemoryService
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.hosted_sweeper import (
    DreamSweeper,
    DreamSweeperConfig,
    SweepTickReport,
    sweeper_config_from_env,
)
from vexic.ports import DreamPhasePorts
from vexic.storage import agent_watermarks, init_db, save_messages
from vexic.storage.connection import StorageTarget

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


class _FakeAgent:
    """Factory-compatible fake for summary/extraction agents."""

    def __init__(self, output: object) -> None:
        self._output = output

    async def run(self, prompt: str, **kwargs: object) -> object:
        return SimpleNamespace(
            output=self._output,
            usage=lambda: SimpleNamespace(
                requests=1, input_tokens=6, output_tokens=4, total_tokens=10
            ),
        )


class _GatedAgent:
    """Fake agent that blocks until a threading gate opens.

    The gate is a `threading.Event` because the sweeper's jobs run on their
    own event loop inside a worker thread; a main-loop asyncio primitive
    cannot be awaited there.
    """

    def __init__(self, gate: threading.Event, output: object) -> None:
        self._gate = gate
        self._output = output

    async def run(self, prompt: str, **kwargs: object) -> object:
        await asyncio.to_thread(self._gate.wait)
        return SimpleNamespace(
            output=self._output,
            usage=lambda: SimpleNamespace(
                requests=1, input_tokens=6, output_tokens=4, total_tokens=10
            ),
        )


class _FailingAgent:
    """Fake agent whose every run raises."""

    async def run(self, prompt: str, **kwargs: object) -> object:
        raise RuntimeError("model call failed")


class _NotImplementedAgent:
    """Fake agent modelling a partially-wired host port: run raises
    NotImplementedError, which the hosted dream boundary rewraps as
    HostPortNotConfigured (`_run_dream_phase_with_usage`)."""

    async def run(self, prompt: str, **kwargs: object) -> object:
        raise NotImplementedError("adapter not wired")


EMBEDDING_DIM = 384


def _fake_embed(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        vector = [0.0] * EMBEDDING_DIM
        vector[hash(text) % EMBEDDING_DIM] = 1.0
        vectors.append(vector)
    return vectors


def _summary_ports() -> DreamPhasePorts:
    return DreamPhasePorts(
        model_group="fake",
        embed=_fake_embed,
        summary_agent_factory=lambda *_a, **_k: _FakeAgent("a fake summary"),
    )


def _seed_compactable_span(db_path: object, *, agent_id: str | None = None) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    save_messages(
        db_path,
        [ModelRequest(parts=[UserPromptPart(content="first summarize span")])],
        session_id="default",
        agent_id=agent_id,
        timestamp=start.isoformat(),
    )
    save_messages(
        db_path,
        [ModelRequest(parts=[UserPromptPart(content="second summarize span")])],
        session_id="default",
        agent_id=agent_id,
        timestamp=(start + timedelta(hours=3)).isoformat(),
    )


def _summary_row_count(db_path: object) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]


class CrossProcessDreamLeaseTests(unittest.IsolatedAsyncioTestCase):
    """The in-flight dedup lock must survive a container boundary.

    Railway does rolling deploys, so an outgoing and an incoming container
    overlap, and `DreamSweeper.run` sweeps immediately on boot. Both processes
    then sweep the same (tenant, agent) scope against the same tenant database.
    A process-local lock is invisible across that boundary, the writes collide,
    and libSQL surfaces the commit conflict as a bare ValueError that halts the
    chain (observed on all six production Light failures, each inside a deploy
    window). Two `HostedMemoryService` instances over one control plane are
    exactly that condition: shared catalog, separate in-process locks.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(self.root)
        self.keys = HostedApiKeyStore(self.root)

    def tearDown(self) -> None:
        # A cancelled dream job's phase keeps running: it executes on a
        # worker-thread event loop that cannot be interrupted (see
        # `_run_system_dream_job`). That thread can still be writing SQLite
        # sidecar files as the temp dir is removed, which raced `cleanup()` into
        # a spurious "Directory not empty". The uninterruptible worker is the
        # product's documented behaviour, so tolerate the debris here rather
        # than pretend the thread stopped.
        shutil.rmtree(self.root, ignore_errors=True)
        self.temp_dir.cleanup()

    def _service(self, ports: DreamPhasePorts) -> HostedMemoryService:
        return HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=ports,
        )

    async def test_failed_lease_acquire_does_not_wedge_the_scope_in_process(
        self,
    ) -> None:
        # The in-process key is taken before the durable lease. If the
        # control-plane write throws (transient libSQL fault), the key must not
        # be left behind -- that scope would then be skipped as
        # "already running" by every later sweep until the process restarts.
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        service = self._service(_summary_ports())

        def _boom(*args: object, **kwargs: object) -> bool:
            raise RuntimeError("control plane unavailable")

        with patch.object(self.catalog, "acquire_dream_lease", _boom):
            with self.assertRaises(RuntimeError):
                service.schedule_system_dream(
                    "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
                )

        # The control plane recovers; the scope must be claimable again.
        task = service.schedule_system_dream(
            "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
        )
        self.assertIsNotNone(task)
        await asyncio.gather(*list(service._background_tasks))

    async def test_failed_lease_release_does_not_escape_the_job(self) -> None:
        # Release runs in the job's `finally`. A throwing control plane there
        # would escape and mask the job's own outcome. Swallow it: the lease row
        # just lapses on its TTL, so the scope is skipped for at most one lease
        # period instead of the failure taking the job down with it.
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        service = self._service(_summary_ports())

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("control plane unavailable")

        with patch.object(self.catalog, "release_dream_lease", _boom):
            job = service.schedule_system_dream(
                "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
            )
            self.assertIsNotNone(job)
            await job  # must not raise

        self.assertTrue(job.done())
        self.assertIsNone(job.exception())

    async def test_cancelled_job_keeps_its_lease_until_the_ttl(self) -> None:
        # Cancelling does not stop the phase already in flight: it runs on a
        # worker-thread event loop that cannot be interrupted, so it keeps
        # writing. Releasing the lease on that path would hand the scope to the
        # next container while the old worker is still writing it -- the very
        # collision this lease exists to prevent. Let the lease lapse instead:
        # the TTL covers the draining worker.
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
        )
        holder = self._service(ports)
        rival = self._service(ports)

        try:
            job = holder.schedule_system_dream(
                "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
            )
            self.assertIsNotNone(job)

            # Let the job actually reach its worker thread. Cancelling a task
            # that has not started yet never runs its `finally`, so it would
            # pass this test without exercising the release path at all.
            for _ in range(100):
                await asyncio.sleep(0.02)
                if any(
                    event.status == "running"
                    for event in holder.dream_trigger_job_events
                ):
                    break
            else:
                self.fail("job never reached the worker thread")

            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job

            # The worker may still be draining, so the scope must stay claimed.
            contended = rival.schedule_system_dream(
                "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
            )
            self.assertIsNone(contended, "a cancelled job released its lease early")
        finally:
            gate.set()

    async def test_cancelling_does_not_let_the_same_process_reacquire(self) -> None:
        # The cancel path keeps the durable lease but clears the in-process key,
        # so the *same* process could try the scope again on its next tick. It
        # must still lose: the lease it is holding has not lapsed, and its own
        # uninterruptible worker is still draining. Acquire steals only an
        # expired row, and does not special-case its own holder id.
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
        )
        service = self._service(ports)

        try:
            job = service.schedule_system_dream(
                "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
            )
            for _ in range(100):
                await asyncio.sleep(0.02)
                if any(
                    event.status == "running"
                    for event in service.dream_trigger_job_events
                ):
                    break
            else:
                self.fail("job never reached the worker thread")

            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job

            again = service.schedule_system_dream(
                "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
            )
            self.assertIsNone(
                again, "the cancelling process reacquired its own draining scope"
            )
        finally:
            gate.set()

    async def test_lease_is_renewed_while_the_chain_is_still_running(self) -> None:
        # The lease TTL bounds a *dead* holder, but it must never lapse under a
        # *live* one. Deep alone has run 8 minutes in production and scales with
        # candidate count, so a long chain could outlive a fixed TTL -- and the
        # steal would hand the scope to a second container mid-write, which is
        # the exact collision the lease exists to prevent. A live holder
        # heartbeats.
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
        )
        holder = self._service(ports)
        rival = self._service(ports)

        with (
            patch.object(hosted, "DREAM_LEASE_TTL", timedelta(seconds=1)),
            patch.object(hosted, "DREAM_LEASE_RENEW_INTERVAL", timedelta(seconds=0.1)),
        ):
            held = holder.schedule_system_dream(
                "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
            )
            self.assertIsNotNone(held)

            # Outlive the TTL while the chain is still gated open.
            await asyncio.sleep(1.5)

            # The holder is alive, so its lease must still be good.
            contended = rival.schedule_system_dream(
                "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
            )
            self.assertIsNone(contended)

            gate.set()
            await asyncio.gather(*list(holder._background_tasks))

    async def test_losing_the_lease_mid_chain_stops_the_job(self) -> None:
        # Renewal returns False when this holder no longer owns the row (a
        # sustained control-plane outage let it lapse and another container
        # stole it). Carrying on would keep writing to the tenant database
        # while the new holder dreams the same scope -- the collision the lease
        # exists to prevent, now silent. Fail closed: stop the chain and let the
        # next tick re-evaluate.
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
        )
        service = self._service(ports)

        try:
            with (
                patch.object(hosted, "DREAM_LEASE_TTL", timedelta(seconds=1)),
                patch.object(
                    hosted, "DREAM_LEASE_RENEW_INTERVAL", timedelta(seconds=0.1)
                ),
                patch.object(self.catalog, "renew_dream_lease", lambda *a, **k: False),
            ):
                job = service.schedule_system_dream(
                    "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
                )
                self.assertIsNotNone(job)

                # The heartbeat discovers the lease is gone and stops the chain.
                # Budget generously (5s against a 0.1s renew interval): this
                # asserts that cancellation *happens*, not how fast, and a tight
                # budget only buys flakes on a loaded CI box.
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if job.done():
                        break

                self.assertTrue(
                    job.done(), "job kept running without holding the lease"
                )
        finally:
            # Release the gated agent and let the cancelled job unwind before
            # the temp dir goes away: a failing assertion would otherwise strand
            # the worker thread, and its in-flight writes would race teardown.
            gate.set()
            with contextlib.suppress(asyncio.CancelledError):
                await job
            await asyncio.sleep(0)

    def test_lapsed_lease_is_stealable_so_a_dead_holder_cannot_wedge_a_scope(
        self,
    ) -> None:
        # A container killed mid-chain never releases its lease. The scope must
        # become claimable once the lease lapses, or one crash silently stops
        # that scope dreaming forever.
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        held = self.catalog.acquire_dream_lease(
            "tenant-a",
            None,
            holder="container-1",
            now="2026-07-12T00:00:00+00:00",
            expires_at="2026-07-12T00:20:00+00:00",
        )
        self.assertTrue(held)

        # Still live: a second container must lose.
        contended = self.catalog.acquire_dream_lease(
            "tenant-a",
            None,
            holder="container-2",
            now="2026-07-12T00:05:00+00:00",
            expires_at="2026-07-12T00:25:00+00:00",
        )
        self.assertFalse(contended)

        # Lapsed: the scope is reclaimable.
        stolen = self.catalog.acquire_dream_lease(
            "tenant-a",
            None,
            holder="container-2",
            now="2026-07-12T00:30:00+00:00",
            expires_at="2026-07-12T00:50:00+00:00",
        )
        self.assertTrue(stolen)

        # The dead holder's late release must not free the new holder's scope.
        self.catalog.release_dream_lease("tenant-a", None, holder="container-1")
        still_held = self.catalog.acquire_dream_lease(
            "tenant-a",
            None,
            holder="container-3",
            now="2026-07-12T00:35:00+00:00",
            expires_at="2026-07-12T00:55:00+00:00",
        )
        self.assertFalse(still_held)

    async def test_second_container_cannot_dream_a_scope_another_holds(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
        )
        outgoing = self._service(ports)
        incoming = self._service(ports)

        held = outgoing.schedule_system_dream(
            "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
        )
        self.assertIsNotNone(held)

        # The incoming container boots mid-rollout and sweeps the same scope.
        # It must lose: the outgoing container still holds the lease.
        contended = incoming.schedule_system_dream(
            "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
        )
        self.assertIsNone(contended)

        gate.set()
        await asyncio.gather(*list(outgoing._background_tasks))

        # Once the holder finishes, the scope is claimable again.
        after = incoming.schedule_system_dream(
            "tenant-a", agent_id=None, phases=(DreamPhase.SUMMARIZE,)
        )
        self.assertIsNotNone(after)
        await asyncio.gather(*list(incoming._background_tasks))


class DreamSweeperTickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(self.root)
        self.keys = HostedApiKeyStore(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _service(self, ports: DreamPhasePorts | None) -> HostedMemoryService:
        return HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=ports,
        )

    def _sweeper(self, service: HostedMemoryService, **overrides: object) -> DreamSweeper:
        config = DreamSweeperConfig(stagger_seconds=0.0, **overrides)
        # Deterministic stamps: the sweeper's clock returns whatever the test
        # last assigned to `self.clock_now`.
        self.clock_now = NOW
        return DreamSweeper(service, config, clock=lambda: self.clock_now)

    @staticmethod
    def _scope_watermark(db_path: object, agent_id: str | None = None) -> int:
        return dict(agent_watermarks(db_path))[agent_id]

    async def _drain_background(self, service: HostedMemoryService) -> None:
        while service._background_tasks:
            await asyncio.gather(*list(service._background_tasks))

    async def test_tick_schedules_summarize_for_tenant_with_new_messages(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.summarize_scheduled, 1)
        self.assertGreater(_summary_row_count(tenant.db_path), 0)

    async def test_sweep_resolves_storage_through_customer_target_resolver(self) -> None:
        """Regression: with a customer-target resolver configured (Turso
        backend), the sweeper reads watermarks from the resolved target,
        never from the vestigial local ``tenant.db_path``."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        resolved_path = self.root / "customer-memory.db"
        init_db(str(resolved_path))
        _seed_compactable_span(str(resolved_path))
        target = StorageTarget(str(resolved_path))
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=_summary_ports(),
            customer_target_resolver=lambda _tenant: target,
        )
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.summarize_scheduled, 1)
        self.assertGreater(_summary_row_count(str(resolved_path)), 0)
        self.assertEqual(_summary_row_count(tenant.db_path), 0)

    async def test_second_tick_skips_when_no_new_messages(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)

        await sweeper.tick(now=NOW)
        await self._drain_background(service)
        report = await sweeper.tick(now=NOW + timedelta(minutes=30))

        self.assertEqual(report.summarize_scheduled, 0)
        self.assertEqual(report.skipped_no_new_messages, 1)

    async def test_disabled_tenant_is_skipped(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        self.catalog.set_dream_scheduling("tenant-a", enabled=False)
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)

        self.assertEqual(report.summarize_scheduled, 0)
        self.assertEqual(report.skipped_disabled, 1)
        self.assertEqual(_summary_row_count(tenant.db_path), 0)

    async def test_sweeps_each_recorded_agent_scope(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path, agent_id=None)
        _seed_compactable_span(tenant.db_path, agent_id="agent-b")
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.summarize_scheduled, 2)

    async def test_broken_tenant_does_not_stop_the_tick(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        tenant_b = self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        _seed_compactable_span(tenant_b.db_path)
        service = self._service(_summary_ports())
        # Break tenant-a's memory database path resolution.
        original_get_tenant = self.catalog.get_tenant

        def broken_get_tenant(tenant_id: str):
            if tenant_id == "tenant-a":
                raise RuntimeError("catalog corruption")
            return original_get_tenant(tenant_id)

        self.catalog.get_tenant = broken_get_tenant  # type: ignore[method-assign]
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.errors, 1)
        self.assertEqual(report.summarize_scheduled, 1)
        self.assertGreater(_summary_row_count(tenant_b.db_path), 0)

    async def test_every_tenant_failing_logs_a_distinct_error(self) -> None:
        """A sweep that fails for every tenant on every tick must surface
        loudly, not blend into per-tenant noise."""
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        service = self._service(_summary_ports())

        def broken_get_tenant(tenant_id: str):
            raise RuntimeError("catalog corruption")

        self.catalog.get_tenant = broken_get_tenant  # type: ignore[method-assign]
        sweeper = self._sweeper(service)

        with self.assertLogs("vexic.hosted_sweeper", level="ERROR") as logs:
            report = await sweeper.tick(now=NOW)

        self.assertEqual(report.errors, 2)
        self.assertTrue(
            any("every tenant" in message for message in logs.output),
            logs.output,
        )

    async def test_missing_summary_port_skips_without_crashing(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        service = self._service(None)
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)

        self.assertEqual(report.summarize_scheduled, 0)
        self.assertEqual(_summary_row_count(tenant.db_path), 0)

    async def test_full_dream_runs_when_due_and_records_completion(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _FakeAgent("a fake summary"),
            extraction_agent_factory=lambda *_a, **_k: _FakeAgent([]),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.dreams_scheduled, 1)
        with closing(sqlite3.connect(tenant.db_path)) as conn:
            dream_rows = conn.execute("SELECT COUNT(*) FROM dream_runs").fetchone()[0]
        self.assertGreater(dream_rows, 0)

        # Not due again within the interval: second tick schedules no dream.
        report_two = await sweeper.tick(now=NOW + timedelta(hours=1))
        await self._drain_background(service)
        self.assertEqual(report_two.dreams_scheduled, 0)

        # Due again after the interval passes.
        report_three = await sweeper.tick(now=NOW + timedelta(hours=25))
        await self._drain_background(service)
        self.assertEqual(report_three.dreams_scheduled, 1)

    async def test_summarize_watermark_advances_only_after_job_completes(self) -> None:
        """The watermark is sweep state for finished work, not scheduled work.

        Writing it at schedule time would let a process stop mid-job strand
        those rows: the next tick would see no new messages and skip them,
        breaking the module's "a lost tick or restart loses nothing" posture.
        """
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)

        self.assertEqual(report.summarize_scheduled, 1)
        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(state.last_summarize_watermark, 0)

        gate.set()
        await self._drain_background(service)

        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(
            state.last_summarize_watermark, self._scope_watermark(tenant.db_path)
        )

    async def test_failed_summarize_run_still_advances_watermark(self) -> None:
        """Deliberate spend posture: the job ran, its errors are in job
        events, and re-sweeping a persistently failing tenant every tick
        would burn model spend without an operator in the loop."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _FailingAgent(),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(_summary_row_count(tenant.db_path), 0)
        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(
            state.last_summarize_watermark, self._scope_watermark(tenant.db_path)
        )

    async def test_never_run_summarize_holds_watermark(self) -> None:
        """A dream chain that fails before SUMMARIZE never summarized those
        rows, so the watermark must NOT advance over them. Distinct
        from a summarize that ran-and-failed (advances, anti-spend): here Light
        fails first and the chain returns before SUMMARIZE executes. Advancing
        would make the next tick see no new messages and strand the span
        unsummarized until a fresh message arrives.
        """
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _FakeAgent("a fake summary"),
            extraction_agent_factory=lambda *_a, **_k: _FailingAgent(),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        # Light failed first: the chain returned before SUMMARIZE ever ran.
        self.assertEqual(report.dreams_scheduled, 1)
        self.assertEqual(_summary_row_count(tenant.db_path), 0)
        state = self.catalog.dream_sweep_state("tenant-a", None)
        # The dream stamp still advances — the failure is durably recorded —
        # but the summarize watermark is withheld: those rows were never
        # summarized, so the next tick must still see them as new.
        self.assertEqual(state.last_dream_completed_at, NOW.isoformat())
        self.assertEqual(state.last_summarize_watermark, 0)

    async def test_failed_dream_chain_still_records_completion(self) -> None:
        """Pins the deliberate stamp-on-failure posture for dream chains
        (see `_record_sweep_state_after`): a failing chain must not re-dream
        every tick; retry waits for the full dream interval."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _FakeAgent("a fake summary"),
            extraction_agent_factory=lambda *_a, **_k: _FailingAgent(),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.dreams_scheduled, 1)
        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(state.last_dream_completed_at, NOW.isoformat())

        # The failure that advanced the clock is durably queryable, not silent:
        # advancing is only safe because the operator can find this row.
        with closing(sqlite3.connect(tenant.db_path)) as conn:
            error_rows = conn.execute(
                "SELECT COUNT(*) FROM dream_runs WHERE status = 'error'"
            ).fetchone()[0]
        self.assertGreater(error_rows, 0)

        report_two = await sweeper.tick(now=NOW + timedelta(hours=1))
        await self._drain_background(service)
        self.assertEqual(report_two.dreams_scheduled, 0)

    async def test_dream_clock_holds_when_failure_unrecorded(self) -> None:
        """A dream phase that fails AND cannot durably record its
        terminal error row must NOT advance the 24h retry clock. Advancing over
        a silent failure is what stalled Tier 3 ~38h live; instead the chain
        retries on the next tick."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _FakeAgent("a fake summary"),
            extraction_agent_factory=lambda *_a, **_k: _FailingAgent(),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        def always_502(*_a: object, **_k: object) -> object:
            # The failing Light phase's error-row write itself hits a persistent
            # retryable Turso fault, so no dream_runs row lands: unrecorded.
            raise ValueError(
                "Hrana: `api error: `status=502 Bad Gateway, "
                'body={"error":"connect to upstream failed"}``'
            )

        with patch("vexic.pipeline.commit_dream_cycle", side_effect=always_502):
            report = await sweeper.tick(now=NOW)
            await self._drain_background(service)

        self.assertEqual(report.dreams_scheduled, 1)
        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertIsNone(state.last_dream_completed_at)

        # Clock never advanced, so the very next tick re-schedules the dream
        # instead of waiting a full interval.
        report_two = await sweeper.tick(now=NOW + timedelta(minutes=30))
        await self._drain_background(service)
        self.assertEqual(report_two.dreams_scheduled, 1)

    async def test_recorded_failure_advances_even_when_error_rewrapped(self) -> None:
        """A phase failure rewrapped at the hosted boundary (NotImplementedError
        -> HostPortNotConfigured) still counts as durably recorded: the mark
        rides the __cause__ chain, so the clock advances the full interval
        instead of re-dreaming every tick and burning model spend."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _FakeAgent("a fake summary"),
            extraction_agent_factory=lambda *_a, **_k: _NotImplementedAgent(),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.dreams_scheduled, 1)
        with closing(sqlite3.connect(tenant.db_path)) as conn:
            error_rows = conn.execute(
                "SELECT COUNT(*) FROM dream_runs WHERE status = 'error'"
            ).fetchone()[0]
        self.assertGreater(error_rows, 0)
        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(state.last_dream_completed_at, NOW.isoformat())

    async def test_stale_stream_record_failure_retries_on_fresh_connection(self) -> None:
        """A reaped Hrana stream loses the state write (verified live);
        the recorder must retry the whole record call so a fresh
        connection re-executes and commits."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)

        real_record = self.catalog.record_summarize_watermark
        calls = {"count": 0}

        def flaky_record(*args: object) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise ValueError(
                    "Hrana: `api error: `status=404 Not Found, "
                    'body={"error":"stream not found: 68426218:1738176"}``'
                )
            real_record(*args)

        self.catalog.record_summarize_watermark = flaky_record
        try:
            await sweeper.tick(now=NOW)
            await self._drain_background(service)
        finally:
            del self.catalog.record_summarize_watermark

        self.assertEqual(calls["count"], 2)
        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(
            state.last_summarize_watermark, self._scope_watermark(tenant.db_path)
        )

    async def test_watermark_record_failure_does_not_skip_dream_stamp(self) -> None:
        """The two state writes are independent: a failed watermark write must
        not skip the dream-completed stamp, or the dream re-fires every tick
        and burns model spend indefinitely."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _FakeAgent("a fake summary"),
            extraction_agent_factory=lambda *_a, **_k: _FakeAgent([]),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        def broken_record(*args: object) -> None:
            raise RuntimeError("control-plane write failed")

        self.catalog.record_summarize_watermark = broken_record
        try:
            with self.assertLogs("vexic.hosted_sweeper", level="ERROR") as logs:
                await sweeper.tick(now=NOW)
                await self._drain_background(service)
        finally:
            del self.catalog.record_summarize_watermark

        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(state.last_dream_completed_at, NOW.isoformat())
        self.assertEqual(state.last_summarize_watermark, 0)
        self.assertTrue(
            any("watermark" in line.lower() for line in logs.output)
        )
        self.assertEqual(sweeper._record_failures, 1)

    async def test_record_failures_surface_in_the_run_log(self) -> None:
        """The tick summary must not claim a clean sweep when recorder tasks
        failed after the tick returned; the counter is reported and reset on
        the next log line."""
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)
        sweeper._record_failures = 3
        stop = asyncio.Event()

        async def one_tick(*, now: datetime | None = None) -> SweepTickReport:
            stop.set()
            return SweepTickReport()

        sweeper.tick = one_tick
        with self.assertLogs("vexic.hosted_sweeper", level="INFO") as logs:
            await sweeper.run(stop)

        self.assertTrue(any("3 record failures" in line for line in logs.output))
        self.assertTrue(any("sweep errors" in line for line in logs.output))
        self.assertEqual(sweeper._record_failures, 0)

    async def test_shutdown_flushes_unlogged_record_failures(self) -> None:
        """A recorder failure that lands after the last tick log line must
        still surface before `run()` exits, not vanish into shutdown."""
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)
        stop = asyncio.Event()

        async def one_tick(*, now: datetime | None = None) -> SweepTickReport:
            return SweepTickReport()

        sweeper.tick = one_tick

        async def fail_then_stop() -> None:
            await asyncio.sleep(0)
            sweeper._record_failures += 1
            stop.set()

        with self.assertLogs("vexic.hosted_sweeper", level="INFO") as logs:
            await asyncio.gather(sweeper.run(stop), fail_then_stop())

        self.assertTrue(
            any("1 record failure" in line and "stopping" in line for line in logs.output)
        )
        self.assertEqual(sweeper._record_failures, 0)

    async def test_locked_scope_keeps_its_own_watermark_unadvanced(self) -> None:
        """A scope skipped by the in-flight lock never ran this tick's job,
        so ITS watermark must not advance: the next tick has to see its rows
        as new and retry. Sweep state is per (tenant, agent) scope, so the
        unlocked scope's watermark still advances independently -- a locked
        scope must not strand its neighbors, and a neighbor's failure posture
        must not be defeated by every-tick retries."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path, agent_id=None)
        _seed_compactable_span(tenant.db_path, agent_id="agent-b")
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        # Hold agent-b's per-(tenant, agent) lock with an in-flight job so
        # the tick's schedule attempt for that scope returns None.
        held = service.schedule_system_dream(
            "tenant-a", agent_id="agent-b", phases=(DreamPhase.SUMMARIZE,)
        )
        self.assertIsNotNone(held)

        report = await sweeper.tick(now=NOW)
        self.assertEqual(report.summarize_scheduled, 1)
        self.assertEqual(report.skipped_locked, 1)

        gate.set()
        await self._drain_background(service)

        # The unlocked shared scope advanced; the locked scope did not.
        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(
            state.last_summarize_watermark,
            self._scope_watermark(tenant.db_path, None),
        )
        state_b = self.catalog.dream_sweep_state("tenant-a", "agent-b")
        self.assertEqual(state_b.last_summarize_watermark, 0)

        # Lock released: the next tick retries only the locked scope.
        report_two = await sweeper.tick(now=NOW + timedelta(minutes=30))
        await self._drain_background(service)
        self.assertEqual(report_two.summarize_scheduled, 1)
        state_b = self.catalog.dream_sweep_state("tenant-a", "agent-b")
        self.assertEqual(
            state_b.last_summarize_watermark,
            self._scope_watermark(tenant.db_path, "agent-b"),
        )

    async def test_locked_scope_keeps_its_own_dream_stamp_unset(self) -> None:
        """A due dream skipped on a locked scope is not stamped for THAT
        scope -- it retries next tick instead of waiting a full interval --
        while an unlocked scope's completed chain stamps independently."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path, agent_id=None)
        _seed_compactable_span(tenant.db_path, agent_id="agent-b")
        gate = threading.Event()
        ports = DreamPhasePorts(
            model_group="fake",
            embed=_fake_embed,
            summary_agent_factory=lambda *_a, **_k: _GatedAgent(gate, "a fake summary"),
            extraction_agent_factory=lambda *_a, **_k: _FakeAgent([]),
        )
        service = self._service(ports)
        sweeper = self._sweeper(service)

        held = service.schedule_system_dream(
            "tenant-a", agent_id="agent-b", phases=(DreamPhase.SUMMARIZE,)
        )
        self.assertIsNotNone(held)

        report = await sweeper.tick(now=NOW)
        self.assertEqual(report.dreams_scheduled, 1)
        self.assertEqual(report.skipped_locked, 1)

        gate.set()
        await self._drain_background(service)

        state = self.catalog.dream_sweep_state("tenant-a", None)
        self.assertEqual(state.last_dream_completed_at, NOW.isoformat())
        state_b = self.catalog.dream_sweep_state("tenant-a", "agent-b")
        self.assertIsNone(state_b.last_dream_completed_at)

        # Lock released: only the skipped scope's dream is still due.
        later = NOW + timedelta(minutes=30)
        self.clock_now = later
        report_two = await sweeper.tick(now=later)
        await self._drain_background(service)
        self.assertEqual(report_two.dreams_scheduled, 1)
        state_b = self.catalog.dream_sweep_state("tenant-a", "agent-b")
        self.assertEqual(state_b.last_dream_completed_at, later.isoformat())

    async def test_summarize_only_ports_never_schedule_full_dreams(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        _seed_compactable_span(tenant.db_path)
        service = self._service(_summary_ports())
        sweeper = self._sweeper(service)

        report = await sweeper.tick(now=NOW)
        await self._drain_background(service)

        self.assertEqual(report.dreams_scheduled, 0)


class SweeperLifespanTests(unittest.TestCase):
    def test_app_lifespan_runs_and_stops_the_sweeper(self) -> None:
        from fastapi.testclient import TestClient

        from vexic.hosted_http import create_app

        class _SweeperDouble:
            def __init__(self) -> None:
                self.started = False
                self.stopped = False

            async def run(self, stop: asyncio.Event) -> None:
                self.started = True
                await stop.wait()
                self.stopped = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = HostedMemoryService(
                HostedTenantCatalog(root),
                HostedApiKeyStore(root),
                telemetry=None,
            )
            double = _SweeperDouble()
            app = create_app(service, sweeper=double)

            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertTrue(double.started)

            self.assertTrue(double.stopped)


class SweeperObservabilityTests(unittest.TestCase):
    def test_control_plane_app_enables_sweeper_info_logging(self) -> None:
        # The deployed process is the uvicorn worker built through the
        # control-plane create_app factory; the entrypoint parent execs
        # uvicorn, so logging must be configured here or the sweeper's
        # "Dream sweep tick" INFO telemetry never reaches stdout.
        import logging

        from vexic.hosted_control_plane_http import (
            create_app as create_control_plane_app,
        )

        root_logger = logging.getLogger()
        previous_level = root_logger.level
        self.addCleanup(root_logger.setLevel, previous_level)
        root_logger.setLevel(logging.WARNING)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = HostedMemoryService(
                HostedTenantCatalog(Path(temp_dir)),
                HostedApiKeyStore(Path(temp_dir)),
                telemetry=None,
            )
            create_control_plane_app(service, control_plane_tokens=("token",))

        self.assertTrue(
            logging.getLogger("vexic.hosted_sweeper").isEnabledFor(logging.INFO)
        )


class SweeperConfigTests(unittest.TestCase):
    def test_defaults_enabled_with_documented_cadence(self) -> None:
        config = sweeper_config_from_env({})

        self.assertIsNotNone(config)
        self.assertEqual(config.tick_seconds, 1800)
        self.assertEqual(config.dream_interval_seconds, 86_400)

    def test_off_switch_disables_the_sweeper(self) -> None:
        for value in ("off", "0", "false", "OFF"):
            with self.subTest(value=value):
                self.assertIsNone(
                    sweeper_config_from_env({"VEXIC_DREAM_SWEEPER": value})
                )

    def test_non_positive_cadences_fail_loud(self) -> None:
        # A zero/negative tick would tight-loop the tenant scan; a
        # non-positive dream interval would make full dreams due every tick.
        for env in (
            {"VEXIC_DREAM_SWEEP_TICK_SECONDS": "0"},
            {"VEXIC_DREAM_SWEEP_TICK_SECONDS": "-5"},
            {"VEXIC_DREAM_INTERVAL_SECONDS": "0"},
        ):
            with self.subTest(env=env):
                with self.assertRaises(ValueError):
                    sweeper_config_from_env(env)

    def test_intervals_are_env_tunable(self) -> None:
        config = sweeper_config_from_env(
            {
                "VEXIC_DREAM_SWEEP_TICK_SECONDS": "600",
                "VEXIC_DREAM_INTERVAL_SECONDS": "43200",
            }
        )

        self.assertEqual(config.tick_seconds, 600)
        self.assertEqual(config.dream_interval_seconds, 43_200)


if __name__ == "__main__":
    unittest.main()
