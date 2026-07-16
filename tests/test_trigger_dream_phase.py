"""Tests for POST /v1/trigger_dream_phase + its pre-bound async runner.

Design per plan D1-D3 / ADR 0025. The critical, audit-driven property
under test throughout: a trigger-only API key (holding
`MemoryCapability.DREAM_TRIGGER` but NOT `ADMIN_REBUILD`) must be able to
schedule -- and have the background job actually complete -- a summarize
sweep. If the hosted dispatch ever re-enters `_call`/`_bind_request` for the
minted `RunDreamPhaseRequest`, the capability intersection there strips the
server-minted `ADMIN_REBUILD` and the background job 403s; these tests would
then fail with the job event ending in `"error"` instead of `"ok"`.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import vexic.hosted as vexic_hosted
from vexic.contract import (
    DreamPhase,
    FreshContextRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    TriggerDreamPhaseRequest,
    TrustBoundary,
)
from vexic.hosted import (
    HostedInMemoryRateLimiter,
    HostedMemoryService,
    HostedRateLimitExceeded,
    HostedRateLimitRule,
)
from vexic.hosted_http import create_app
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.ports import DreamPhasePorts, HostPortNotConfigured
from vexic.storage import save_messages
from pydantic_ai.messages import ModelRequest, UserPromptPart


def _scope(
    *,
    tenant_id: str = "tenant-a",
    project_id: str | None = "project-a",
    agent_id: str | None = None,
    capabilities: set[MemoryCapability],
) -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant_id,
        project_id=project_id,
        agent_id=agent_id,
        principal=Principal(principal_id="caller-supplied", principal_type=PrincipalType.HUMAN),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities=capabilities,
    )


def _seed_compactable_span(db_path, *, agent_id: str | None = None) -> None:
    """Write two message spans separated by a >2h gap.

    Mirrors the summarize-phase fixture elsewhere in the suite: the gap
    creates a compaction boundary so the leaf pass has a span to summarize.
    """
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


class _FakeSummaryAgent:
    """AgentFactory-compatible fake; optionally gated by a threading.Event."""

    def __init__(self, *, gate: threading.Event | None = None) -> None:
        self._gate = gate

    async def run(self, prompt: str) -> object:
        if self._gate is not None:
            # Block the WORKER thread's own event loop (not the serving
            # loop) until the test releases the gate.
            self._gate.wait(timeout=5)
        return SimpleNamespace(
            output="a fake summary",
            usage=lambda: SimpleNamespace(
                requests=1, input_tokens=6, output_tokens=4, total_tokens=10
            ),
        )


def _summary_row_count(db_path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]


class TriggerDreamPhaseServiceTests(unittest.IsolatedAsyncioTestCase):
    """Service-level tests exercising the real pre-bound async runner.

    These call `HostedMemoryService.trigger_dream_phase` directly (rather
    than through the HTTP route) so the background `asyncio.create_task` can
    be awaited deterministically via `service._background_tasks` -- FastAPI's
    TestClient tears down its event loop between bare (non-`with`) requests,
    which would cancel any scheduled background task before it could run.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(self.root)
        self.keys = HostedApiKeyStore(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_service(self, *, ports: DreamPhasePorts | None, **kwargs) -> HostedMemoryService:
        return HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=ports,
            **kwargs,
        )

    def _fake_ports(self, *, gate: threading.Event | None = None) -> DreamPhasePorts:
        return DreamPhasePorts(
            model_group="fake",
            summary_agent_factory=lambda *_a, **_k: _FakeSummaryAgent(gate=gate),
        )

    async def _await_only_task(self, service: HostedMemoryService) -> None:
        tasks = list(service._background_tasks)
        self.assertEqual(len(tasks), 1)
        await tasks[0]

    async def test_trigger_key_without_admin_rebuild_succeeds_end_to_end(self) -> None:
        """Regression test for the v1/v2 audit blocker: pre-bound routing."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={
                MemoryCapability.WRITE,
                MemoryCapability.SEARCH,
                MemoryCapability.DREAM_TRIGGER,
            },
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant.db_path)
        service = self._make_service(ports=self._fake_ports())

        result = await service.trigger_dream_phase(
            api_key.raw_key,
            TriggerDreamPhaseRequest(
                scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
                phase=DreamPhase.SUMMARIZE,
            ),
        )

        self.assertEqual(result.status, "scheduled")
        await self._await_only_task(service)

        self.assertGreater(_summary_row_count(tenant.db_path), 0)
        self.assertEqual(
            [event.status for event in service.dream_trigger_job_events],
            ["running", "ok"],
        )
        self.assertEqual(
            [event.phase for event in service.dream_trigger_job_events],
            ["summarize", "summarize"],
        )
        job_usage = [e for e in self.catalog.usage_events("tenant-a") if e.kind == "job"]
        self.assertEqual(job_usage[-1].status, "ok")
        self.assertEqual(job_usage[-1].total_tokens, 10)

    async def test_retire_tenant_between_trigger_and_run_blocks_worker(self) -> None:
        """A retire landing after scheduling still cuts access (ADR 0028).

        The minted dream job deliberately bypasses ``_bind_request`` at
        execution time (ADR 0025), so the worker must re-check retirement
        itself before touching tenant memory.
        """
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant.db_path)
        service = self._make_service(ports=self._fake_ports())

        result = await service.trigger_dream_phase(
            api_key.raw_key,
            TriggerDreamPhaseRequest(
                scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
                phase=DreamPhase.SUMMARIZE,
            ),
        )
        self.assertEqual(result.status, "scheduled")

        self.catalog.retire_tenant("tenant-a")
        await self._await_only_task(service)

        self.assertEqual(_summary_row_count(tenant.db_path), 0)
        self.assertEqual(
            [event.status for event in service.dream_trigger_job_events],
            ["running", "error"],
        )

    async def test_retire_project_between_trigger_and_run_blocks_worker(self) -> None:
        """Retiring the bound project after scheduling also blocks the worker."""
        tenant = self.catalog.provision_tenant("tenant-a")
        project = self.catalog.create_control_project("tenant-a", name="Alpha")
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={project.project_id},
        )
        _seed_compactable_span(tenant.db_path)
        service = self._make_service(ports=self._fake_ports())

        result = await service.trigger_dream_phase(
            api_key.raw_key,
            TriggerDreamPhaseRequest(
                scope=_scope(
                    project_id=project.project_id,
                    capabilities={MemoryCapability.DREAM_TRIGGER},
                ),
                phase=DreamPhase.SUMMARIZE,
            ),
        )
        self.assertEqual(result.status, "scheduled")

        self.catalog.retire_control_project("tenant-a", project.project_id)
        await self._await_only_task(service)

        self.assertEqual(_summary_row_count(tenant.db_path), 0)
        self.assertEqual(
            [event.status for event in service.dream_trigger_job_events],
            ["running", "error"],
        )

    async def test_queued_job_runs_on_repointed_database(self) -> None:
        """A queued job must execute against the LIVE storage target.

        `activate_replacement_database` bumps `generation` so pre-repoint
        handles stop being authoritative; a job scheduled before the repoint
        must not write summaries into the abandoned database.
        """
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant.db_path)
        artifact = self.root / "artifact.vexic"
        replacement_db = self.root / "replacement.db"
        export_canonical_migration(
            str(tenant.db_path), str(artifact),
            tenant_id="tenant-a", project_id="project-a",
        )
        import_canonical_migration(
            str(artifact), str(replacement_db),
            tenant_id="tenant-a", project_id="project-a",
        )
        service = self._make_service(ports=self._fake_ports())

        result = await service.trigger_dream_phase(
            api_key.raw_key,
            TriggerDreamPhaseRequest(
                scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
                phase=DreamPhase.SUMMARIZE,
            ),
        )
        self.assertEqual(result.status, "scheduled")

        self.catalog.activate_replacement_database("tenant-a", replacement_db)
        await self._await_only_task(service)

        self.assertGreater(_summary_row_count(replacement_db), 0)
        self.assertEqual(_summary_row_count(tenant.db_path), 0)

    async def test_rate_bucket_consumed_exactly_once_per_trigger(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant.db_path)
        service = self._make_service(
            ports=self._fake_ports(),
            rate_limiter=HostedInMemoryRateLimiter(
                operation_rules={"run_dream_phase": HostedRateLimitRule(limit=1, window_seconds=60)},
            ),
        )
        request = TriggerDreamPhaseRequest(
            scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
            phase=DreamPhase.SUMMARIZE,
        )

        # If the internal routing double-counted the bucket (e.g. by
        # re-entering `_call` for the minted RunDreamPhaseRequest), even this
        # FIRST call would already exceed a limit of 1.
        first = await service.trigger_dream_phase(api_key.raw_key, request)
        self.assertEqual(first.status, "scheduled")

        with self.assertRaises(HostedRateLimitExceeded):
            await service.trigger_dream_phase(api_key.raw_key, request)

        await self._await_only_task(service)

    async def test_already_running_dedup_returns_skipped(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        service = self._make_service(ports=self._fake_ports(gate=gate))
        request = TriggerDreamPhaseRequest(
            scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
            phase=DreamPhase.SUMMARIZE,
        )

        first = await service.trigger_dream_phase(api_key.raw_key, request)
        second = await service.trigger_dream_phase(api_key.raw_key, request)

        self.assertEqual(first.status, "scheduled")
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.reason, "already_running")

        gate.set()
        await self._await_only_task(service)

    async def test_lock_is_released_when_scheduling_raises_after_acquire(self) -> None:
        """Regression test: a post-acquire exception must not wedge the lock.

        Before the fix, nothing between `_acquire_dream_trigger_lock` and
        `asyncio.create_task` was guarded by try/except, so an exception
        raised while minting the background request (e.g. a future
        validation change) would leak the in-flight lock forever: the first
        trigger 500s, and every subsequent trigger for that (tenant, agent)
        returns `skipped`/`already_running` until process restart.
        """
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant.db_path)
        service = self._make_service(ports=self._fake_ports())
        request = TriggerDreamPhaseRequest(
            scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
            phase=DreamPhase.SUMMARIZE,
        )

        real_run_dream_phase_request = vexic_hosted.RunDreamPhaseRequest
        call_count = {"n": 0}

        def _boom_once(*args: object, **kwargs: object) -> object:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom while minting request")
            return real_run_dream_phase_request(*args, **kwargs)

        with unittest.mock.patch.object(
            vexic_hosted, "RunDreamPhaseRequest", side_effect=_boom_once
        ):
            with self.assertRaises(RuntimeError):
                await service.trigger_dream_phase(api_key.raw_key, request)

        # The lock must have been released by the failed attempt: a
        # subsequent trigger schedules normally instead of being skipped.
        second = await service.trigger_dream_phase(api_key.raw_key, request)
        self.assertEqual(second.status, "scheduled")

        await self._await_only_task(service)

    async def test_missing_summary_agent_factory_fails_closed_synchronously(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        )
        service = self._make_service(ports=None)

        with self.assertRaises(HostPortNotConfigured):
            await service.trigger_dream_phase(
                api_key.raw_key,
                TriggerDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
                    phase=DreamPhase.SUMMARIZE,
                ),
            )

        # Fails BEFORE scheduling: no background task, no in-flight lock held.
        self.assertEqual(len(service._background_tasks), 0)

    async def test_tenant_isolation_trigger_only_touches_triggering_tenant(self) -> None:
        tenant_a = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        tenant_b = self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        api_key_a = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant_a.db_path)
        _seed_compactable_span(tenant_b.db_path)
        service = self._make_service(ports=self._fake_ports())

        result = await service.trigger_dream_phase(
            api_key_a.raw_key,
            TriggerDreamPhaseRequest(
                scope=_scope(tenant_id="tenant-a", capabilities={MemoryCapability.DREAM_TRIGGER}),
                phase=DreamPhase.SUMMARIZE,
            ),
        )
        self.assertEqual(result.status, "scheduled")
        await self._await_only_task(service)

        self.assertGreater(_summary_row_count(tenant_a.db_path), 0)
        self.assertEqual(_summary_row_count(tenant_b.db_path), 0)

    async def test_event_loop_stays_responsive_during_slow_phase(self) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER, MemoryCapability.FRESH_CONTEXT},
            project_ids={"project-a"},
        )
        _seed_compactable_span(tenant.db_path)
        gate = threading.Event()
        service = self._make_service(ports=self._fake_ports(gate=gate))

        result = await service.trigger_dream_phase(
            api_key.raw_key,
            TriggerDreamPhaseRequest(
                scope=_scope(capabilities={MemoryCapability.DREAM_TRIGGER}),
                phase=DreamPhase.SUMMARIZE,
            ),
        )
        self.assertEqual(result.status, "scheduled")

        # The dream trigger's fake agent is blocked on `gate` inside its own
        # worker-thread event loop. If the phase ran on the SERVING loop
        # instead, this unrelated fresh_context call would stall behind it.
        fresh_result = await asyncio.wait_for(
            service.fresh_context(
                api_key.raw_key,
                FreshContextRequest(
                    scope=_scope(
                        capabilities={MemoryCapability.FRESH_CONTEXT},
                    ).model_copy(update={"session_id": "default"}),
                    redaction=RedactionContext(forbidden_values=()),
                ),
            ),
            timeout=2.0,
        )
        self.assertIsNotNone(fresh_result)

        gate.set()
        await self._await_only_task(service)


class TriggerDreamPhaseHttpTests(unittest.TestCase):
    """HTTP-route tests for the synchronously-decidable response shapes.

    (401/403/400/503 -- none of these schedule a background task, so a plain
    `TestClient(app)` without the `with`-block portal is sufficient.)
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(self.root)
        self.keys = HostedApiKeyStore(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _api_key(self, *, capabilities: set[MemoryCapability]) -> str:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        return self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities=capabilities,
            project_ids={"project-a"},
        ).raw_key

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "X-Vexic-Project-Id": "project-a",
        }

    def _client(self, *, ports: DreamPhasePorts | None) -> TestClient:
        service = HostedMemoryService(
            self.catalog, self.keys, telemetry=self.catalog, dream_phase_ports=ports
        )
        return TestClient(create_app(service))

    def test_requires_api_key(self) -> None:
        client = self._client(ports=None)

        response = client.post("/v1/trigger_dream_phase", json={"phase": "summarize"})

        self.assertEqual(response.status_code, 401)

    def test_rejects_key_without_dream_trigger_capability(self) -> None:
        client = self._client(ports=None)
        api_key = self._api_key(capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH})

        response = client.post(
            "/v1/trigger_dream_phase",
            headers=self._headers(api_key),
            json={"phase": "summarize"},
        )

        self.assertEqual(response.status_code, 403)

    def test_rejects_non_summarize_phase(self) -> None:
        client = self._client(ports=None)
        api_key = self._api_key(capabilities={MemoryCapability.DREAM_TRIGGER})

        response = client.post(
            "/v1/trigger_dream_phase",
            headers=self._headers(api_key),
            json={"phase": "light"},
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_build_summary_agent_returns_503_synchronously(self) -> None:
        client = self._client(ports=DreamPhasePorts(model_group="fake"))
        api_key = self._api_key(capabilities={MemoryCapability.DREAM_TRIGGER})

        response = client.post(
            "/v1/trigger_dream_phase",
            headers=self._headers(api_key),
            json={"phase": "summarize"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "host_port_not_configured")

    def test_no_ports_at_all_returns_503_synchronously(self) -> None:
        client = self._client(ports=None)
        api_key = self._api_key(capabilities={MemoryCapability.DREAM_TRIGGER})

        response = client.post(
            "/v1/trigger_dream_phase",
            headers=self._headers(api_key),
            json={"phase": "summarize"},
        )

        self.assertEqual(response.status_code, 503)

    def test_scheduled_response_is_202(self) -> None:
        client = self._client(
            ports=DreamPhasePorts(
                model_group="fake",
                summary_agent_factory=lambda *_a, **_k: _FakeSummaryAgent(),
            )
        )
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.DREAM_TRIGGER},
            project_ids={"project-a"},
        ).raw_key

        response = client.post(
            "/v1/trigger_dream_phase",
            headers=self._headers(api_key),
            json={"phase": "summarize"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
