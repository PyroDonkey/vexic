"""In-server per-tenant dream sweeper (ADR 0030).

The hosted service schedules its own memory consolidation: every tick it
sweeps active tenants, scheduling summarize sweeps when a tenant has new
transcript rows and a full Light -> REM -> Deep chain when the tenant's
nightly dream interval has elapsed. The heavy machinery already exists at the
trigger seam (`HostedMemoryService.schedule_system_dream`): pre-bound
capability containment, per-(tenant, agent) in-flight dedup, worker-thread
event-loop isolation, and the per-tenant daily span budget all apply
unchanged. The sweeper is deliberately a thin, stateless-per-tick loop —
ripeness is recomputed downstream, so a lost tick or restart loses nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from vexic.contract import DreamPhase
from vexic.hosted import HostedMemoryService
from vexic.ports import HostPortNotConfigured
from vexic.storage import distinct_agent_ids, max_message_id

logger = logging.getLogger(__name__)

_OFF_VALUES = frozenset({"off", "0", "false", "no"})
FULL_DREAM_PHASES = (DreamPhase.LIGHT, DreamPhase.REM, DreamPhase.DEEP)


@dataclass(frozen=True)
class DreamSweeperConfig:
    # How often the sweep loop wakes up and walks the tenant list.
    tick_seconds: int = 1800
    # Minimum gap between full Light -> REM -> Deep chains per tenant.
    dream_interval_seconds: int = 86_400
    # Pause between tenants within one tick so a post-3am ripeness pile-up
    # does not become a thundering herd of model calls.
    stagger_seconds: float = 2.0


def sweeper_config_from_env(env: Mapping[str, str]) -> DreamSweeperConfig | None:
    """Build the sweeper config from process env; None means disabled.

    `VEXIC_DREAM_SWEEPER=off` is the kill switch. Cadences are tunable via
    `VEXIC_DREAM_SWEEP_TICK_SECONDS` and `VEXIC_DREAM_INTERVAL_SECONDS`.
    """
    if env.get("VEXIC_DREAM_SWEEPER", "on").strip().lower() in _OFF_VALUES:
        return None
    defaults = DreamSweeperConfig()
    return DreamSweeperConfig(
        tick_seconds=int(
            env.get("VEXIC_DREAM_SWEEP_TICK_SECONDS", defaults.tick_seconds)
        ),
        dream_interval_seconds=int(
            env.get("VEXIC_DREAM_INTERVAL_SECONDS", defaults.dream_interval_seconds)
        ),
    )


@dataclass
class SweepTickReport:
    tenants_seen: int = 0
    summarize_scheduled: int = 0
    dreams_scheduled: int = 0
    skipped_disabled: int = 0
    skipped_no_new_messages: int = 0
    errors: int = 0
    _details: list[str] = field(default_factory=list)


class DreamSweeper:
    def __init__(
        self,
        service: HostedMemoryService,
        config: DreamSweeperConfig,
    ) -> None:
        self._service = service
        self._config = config

    async def run(self, stop: asyncio.Event) -> None:
        """Tick until `stop` is set. Each tick is fully self-contained; a
        failing tick logs (content-free) and the loop keeps going."""
        while not stop.is_set():
            try:
                report = await self.tick()
                logger.info(
                    "Dream sweep tick: %d tenants, %d summarize, %d dreams, "
                    "%d disabled, %d idle, %d errors.",
                    report.tenants_seen,
                    report.summarize_scheduled,
                    report.dreams_scheduled,
                    report.skipped_disabled,
                    report.skipped_no_new_messages,
                    report.errors,
                )
            except Exception:
                logger.exception("Dream sweep tick failed.")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._config.tick_seconds
                )
            except asyncio.TimeoutError:
                continue

    async def tick(self, *, now: datetime | None = None) -> SweepTickReport:
        now = now or datetime.now(timezone.utc)
        report = SweepTickReport()
        catalog = self._service.catalog
        tenant_ids = await asyncio.to_thread(catalog.list_active_tenant_ids)
        for index, tenant_id in enumerate(tenant_ids):
            if index and self._config.stagger_seconds:
                await asyncio.sleep(self._config.stagger_seconds)
            report.tenants_seen += 1
            try:
                await self._sweep_tenant(tenant_id, now=now, report=report)
            except Exception:
                report.errors += 1
                # Content-free by design: tenant memory text must never
                # reach shared server logs.
                logger.exception("Dream sweep failed for one tenant; continuing.")
        return report

    async def _sweep_tenant(
        self,
        tenant_id: str,
        *,
        now: datetime,
        report: SweepTickReport,
    ) -> None:
        catalog = self._service.catalog
        if not await asyncio.to_thread(catalog.dream_scheduling_enabled, tenant_id):
            report.skipped_disabled += 1
            return
        tenant = await asyncio.to_thread(catalog.get_tenant, tenant_id)
        state = await asyncio.to_thread(catalog.dream_sweep_state, tenant_id)

        dream_due = self._dream_capable() and self._dream_is_due(state, now)
        watermark = await asyncio.to_thread(max_message_id, tenant.db_path)
        has_new_messages = watermark > state.last_summarize_watermark
        if not has_new_messages and not dream_due:
            report.skipped_no_new_messages += 1
            return

        agent_ids = await asyncio.to_thread(distinct_agent_ids, tenant.db_path)
        if not agent_ids:
            agent_ids = [None]

        # One job per scope per tick: a due dream runs the full chain with a
        # trailing summarize (the per-(tenant, agent) in-flight lock would
        # otherwise make a same-tick summarize job and dream job collide).
        summarize = self._summarize_capable()
        if dream_due:
            phases = FULL_DREAM_PHASES + ((DreamPhase.SUMMARIZE,) if summarize else ())
        elif summarize:
            phases = (DreamPhase.SUMMARIZE,)
        else:
            logger.warning("Summarize sweep skipped: dream ports not configured.")
            return

        scheduled: list[asyncio.Task[None]] = []
        for agent_id in agent_ids:
            try:
                task = self._service.schedule_system_dream(
                    tenant_id,
                    agent_id=agent_id,
                    phases=phases,
                )
            except HostPortNotConfigured:
                # Fail closed and content-free; other tenants still sweep.
                logger.warning("Dream sweep skipped: dream ports not configured.")
                return
            if task is not None:
                scheduled.append(task)
        if not scheduled:
            return

        if DreamPhase.SUMMARIZE in phases:
            report.summarize_scheduled += len(scheduled)
            await asyncio.to_thread(
                catalog.record_summarize_watermark, tenant_id, watermark
            )
        if dream_due:
            report.dreams_scheduled += 1
            self._record_dream_completion_after(tenant_id, scheduled, now)

    def _dream_capable(self) -> bool:
        ports = self._service.dream_phase_ports
        return ports is not None and ports.extraction_agent_factory is not None

    def _summarize_capable(self) -> bool:
        ports = self._service.dream_phase_ports
        return ports is not None and ports.summary_agent_factory is not None

    def _dream_is_due(self, state: object, now: datetime) -> bool:
        last = getattr(state, "last_dream_completed_at", None)
        if not last:
            return True
        try:
            last_at = datetime.fromisoformat(str(last))
        except ValueError:
            return True
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        elapsed = (now - last_at).total_seconds()
        return elapsed >= self._config.dream_interval_seconds

    def _record_dream_completion_after(
        self,
        tenant_id: str,
        tasks: list["asyncio.Task[None]"],
        now: datetime,
    ) -> None:
        """Stamp the tenant's dream completion once every scope's chain ends.

        The stamp is written even when a phase inside the chain failed: the
        chain ran and its job events carry the error detail, and re-dreaming
        every tick on a persistently failing tenant would burn model spend
        without an operator in the loop.
        """
        catalog = self._service.catalog

        async def _await_and_record() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.to_thread(
                    catalog.record_dream_completed, tenant_id, now.isoformat()
                )
            except Exception:
                logger.exception("Recording dream completion failed.")

        recorder = asyncio.create_task(_await_and_record())
        self._service._background_tasks.add(recorder)
        recorder.add_done_callback(self._service._background_tasks.discard)
