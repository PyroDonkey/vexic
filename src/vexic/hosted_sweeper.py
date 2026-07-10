"""In-server per-tenant dream sweeper (ADR 0030).

The hosted service schedules its own memory consolidation: every tick it
sweeps active tenants, scheduling summarize sweeps for agent scopes with new
transcript rows and a full Light -> REM -> Deep -> Summarize chain when a
scope's dream interval has elapsed. The heavy machinery already exists at the
trigger seam (`HostedMemoryService.schedule_system_dream`): pre-bound
capability containment, per-(tenant, agent) in-flight dedup, worker-thread
event-loop isolation, and the per-tenant daily span budget all apply
unchanged. The sweeper is deliberately a thin, stateless-per-tick loop --
sweep bookkeeping is per (tenant, agent) scope and only advances after a
scope's scheduled job actually finishes, so a lost tick or restart loses
nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from vexic.contract import DreamPhase
from vexic.hosted import HostedMemoryService
from vexic.ports import HostPortNotConfigured
from vexic.storage import agent_watermarks

logger = logging.getLogger(__name__)

_OFF_VALUES = frozenset({"off", "0", "false", "no"})
FULL_DREAM_PHASES = (DreamPhase.LIGHT, DreamPhase.REM, DreamPhase.DEEP)


@dataclass(frozen=True)
class DreamSweeperConfig:
    # How often the sweep loop wakes up and walks the tenant list.
    tick_seconds: int = 1800
    # Minimum gap between full Light -> REM -> Deep chains per scope.
    dream_interval_seconds: int = 86_400
    # Pause between tenants within one tick so a post-3am ripeness pile-up
    # does not become a thundering herd of model calls.
    stagger_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive.")
        if self.dream_interval_seconds <= 0:
            raise ValueError("dream_interval_seconds must be positive.")
        if self.stagger_seconds < 0:
            raise ValueError("stagger_seconds must not be negative.")


def sweeper_config_from_env(env: Mapping[str, str]) -> DreamSweeperConfig | None:
    """Build the sweeper config from process env; None means disabled.

    `VEXIC_DREAM_SWEEPER=off` is the kill switch. Cadences are tunable via
    `VEXIC_DREAM_SWEEP_TICK_SECONDS` and `VEXIC_DREAM_INTERVAL_SECONDS`;
    non-positive values fail loud at startup rather than producing a tight
    scan loop or an every-tick dream.
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
    skipped_locked: int = 0
    errors: int = 0


class DreamSweeper:
    def __init__(
        self,
        service: HostedMemoryService,
        config: DreamSweeperConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._config = config
        # Injectable so tests can pin dream-completion stamps; production
        # always stamps real wall-clock time.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, stop: asyncio.Event) -> None:
        """Tick until `stop` is set. Each tick is fully self-contained; a
        failing tick logs (content-free) and the loop keeps going."""
        while not stop.is_set():
            try:
                report = await self.tick()
                logger.info(
                    "Dream sweep tick: %d tenants, %d summarize, %d dreams, "
                    "%d disabled, %d idle, %d locked, %d errors.",
                    report.tenants_seen,
                    report.summarize_scheduled,
                    report.dreams_scheduled,
                    report.skipped_disabled,
                    report.skipped_no_new_messages,
                    report.skipped_locked,
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
        now = now or self._clock()
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
        watermarks = await asyncio.to_thread(agent_watermarks, tenant.db_path)
        if not watermarks:
            report.skipped_no_new_messages += 1
            return

        summarize = self._summarize_capable()
        dream_capable = self._dream_capable()
        scheduled_any = False
        scope_was_locked = False
        for agent_id, watermark in watermarks:
            state = await asyncio.to_thread(
                catalog.dream_sweep_state, tenant_id, agent_id
            )
            has_new_messages = watermark > state.last_summarize_watermark
            dream_due = dream_capable and self._dream_is_due(state, now)

            # One job per scope per tick: a due dream runs the full chain with
            # a trailing summarize (the per-(tenant, agent) in-flight lock
            # would otherwise make a same-tick summarize job and dream job
            # collide).
            if dream_due:
                phases = FULL_DREAM_PHASES + (
                    (DreamPhase.SUMMARIZE,) if summarize else ()
                )
            elif has_new_messages and summarize:
                phases = (DreamPhase.SUMMARIZE,)
            elif has_new_messages:
                logger.warning(
                    "Summarize sweep skipped: dream ports not configured."
                )
                continue
            else:
                continue

            try:
                task = self._service.schedule_system_dream(
                    tenant_id,
                    agent_id=agent_id,
                    phases=phases,
                )
            except HostPortNotConfigured:
                # Fail closed and content-free; other scopes/tenants still
                # sweep.
                logger.warning("Dream sweep skipped: dream ports not configured.")
                return
            if task is None:
                # A job for this scope is already in flight; its own recorder
                # advances the scope's state, so leave it untouched and let
                # the next tick re-evaluate.
                report.skipped_locked += 1
                scope_was_locked = True
                continue

            scheduled_any = True
            if DreamPhase.SUMMARIZE in phases:
                report.summarize_scheduled += 1
            if dream_due:
                report.dreams_scheduled += 1
            self._record_scope_state_after(
                tenant_id,
                agent_id,
                task,
                watermark=watermark if DreamPhase.SUMMARIZE in phases else None,
                dream_completed=dream_due,
            )

        if not scheduled_any and not scope_was_locked:
            report.skipped_no_new_messages += 1

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

    def _record_scope_state_after(
        self,
        tenant_id: str,
        agent_id: str | None,
        task: "asyncio.Task[None]",
        *,
        watermark: int | None,
        dream_completed: bool,
    ) -> None:
        """Advance one scope's sweep state once its job finishes running.

        Deliberate posture (test-pinned): a chain that RAN but failed a phase
        in-band still advances state -- the failure detail is durable in the
        per-phase job events, and re-running every tick on a persistently
        failing tenant would burn model spend without an operator in the
        loop. A CANCELLED job did not finish running, so state is left
        untouched and the next tick retries. The dream stamp records actual
        completion time, not tick-start time, so the next dream is due a
        full interval after the chain finished. Catalog writes are monotonic,
        so a stale recorder can never rewind a newer watermark or stamp.
        """
        catalog = self._service.catalog

        async def _await_and_record() -> None:
            results = await asyncio.gather(task, return_exceptions=True)
            if any(isinstance(result, asyncio.CancelledError) for result in results):
                return
            try:
                if watermark is not None:
                    await asyncio.to_thread(
                        catalog.record_summarize_watermark,
                        tenant_id,
                        agent_id,
                        watermark,
                    )
                if dream_completed:
                    await asyncio.to_thread(
                        catalog.record_dream_completed,
                        tenant_id,
                        agent_id,
                        self._clock().isoformat(),
                    )
            except Exception:
                logger.exception("Recording sweep state failed.")

        recorder = asyncio.create_task(_await_and_record())
        self._service._background_tasks.add(recorder)
        recorder.add_done_callback(self._service._background_tasks.discard)
