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
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import DreamPhase
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
        self.temp_dir.cleanup()

    def _service(self, ports: DreamPhasePorts) -> HostedMemoryService:
        return HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=ports,
        )

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

        report_two = await sweeper.tick(now=NOW + timedelta(hours=1))
        await self._drain_background(service)
        self.assertEqual(report_two.dreams_scheduled, 0)

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
