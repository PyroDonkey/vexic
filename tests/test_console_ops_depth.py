from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vexic.contract import (
    FreshContextRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    TrustBoundary,
)
from vexic.hosted import (
    HostedInMemoryRateLimiter,
    HostedJobEvent,
    HostedMemoryService,
    HostedUsageEvent,
)
from vexic.hosted_control_plane_http import create_app as create_control_plane_app
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


class ConsoleOpsDepthHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(root)
        self.keys = HostedApiKeyStore(root)
        self.service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(),
        )
        self.client = TestClient(
            create_control_plane_app(
                self.service,
                control_plane_tokens=("console-secret",),
            )
        )

    def _control_auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer console-secret"}

    def _create_project(self, org: str = "org_123", name: str = "Alpha") -> dict:
        response = self.client.post(
            f"/control/v1/clerk-orgs/{org}/projects",
            headers=self._control_auth(),
            json={"name": name},
        )
        assert response.status_code == 201, response.text
        return response.json()["project"]

    def _create_key(self, org: str, project_id: str, name: str = "key-a") -> dict:
        response = self.client.post(
            f"/control/v1/clerk-orgs/{org}/projects/{project_id}/keys",
            headers=self._control_auth(),
            json={"name": name},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _control_db(self) -> sqlite3.Connection:
        return sqlite3.connect(Path(self.temp_dir.name) / "control-plane.db")


class LastUsedAtTests(ConsoleOpsDepthHarness):
    def test_authenticate_records_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        self.keys.authenticate(created["rawKey"])

        with contextlib.closing(self._control_db()) as conn:
            row = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()
        self.assertIsNotNone(row[0])

    def test_last_used_at_write_is_throttled_to_one_minute(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        self.keys.authenticate(created["rawKey"])
        with contextlib.closing(self._control_db()) as conn:
            first = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()[0]

        self.keys.authenticate(created["rawKey"])
        with contextlib.closing(self._control_db()) as conn:
            second = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()[0]

        self.assertEqual(first, second)

    def test_failed_authentication_does_not_record_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        with self.assertRaises(PermissionError):
            self.keys.authenticate(f"vx_{created['key']['id']}_wrong-secret")

        with contextlib.closing(self._control_db()) as conn:
            row = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()
        self.assertIsNone(row[0])


class KeyListLifecycleTests(ConsoleOpsDepthHarness):
    def test_key_list_includes_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])
        self.keys.authenticate(created["rawKey"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        key = response.json()["keys"][0]
        self.assertIn("lastUsedAt", key)
        self.assertIsNotNone(key["lastUsedAt"])

    def test_key_list_excludes_revoked_by_default_and_includes_on_request(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])
        key_id = created["key"]["id"]
        revoke = self.client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys/{key_id}/revoke",
            headers=self._control_auth(),
        )
        self.assertEqual(revoke.status_code, 204)

        default = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
        )
        self.assertEqual(default.json()["keys"], [])

        included = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys?include=revoked",
            headers=self._control_auth(),
        )
        keys = included.json()["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["id"], key_id)
        self.assertIsNotNone(keys[0]["revokedAt"])
        for forbidden in ("keyHash", "key_hash", "rawKey"):
            self.assertNotIn(forbidden, keys[0])


class UsageKeyAttributionTests(ConsoleOpsDepthHarness):
    def test_usage_events_carry_key_id(self) -> None:
        self.catalog.record_usage_event(
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="shared",
                status="ok",
                recorded_at="2026-07-01T00:00:00Z",
                project_id="proj_a",
                key_id="key_abc",
            )
        )

        events = self.catalog.usage_events("tenant-a")

        self.assertEqual(events[0].key_id, "key_abc")

    def test_usage_events_without_key_id_load_as_none(self) -> None:
        self.catalog.record_usage_event(
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="shared",
                status="ok",
                recorded_at="2026-07-01T00:00:00Z",
            )
        )

        events = self.catalog.usage_events("tenant-a")

        self.assertIsNone(events[0].key_id)


class UsageAnalyticsEndpointTests(ConsoleOpsDepthHarness):
    def _seed_usage(self, project_id: str) -> tuple[str, str]:
        from datetime import UTC, datetime, timedelta

        day1 = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00Z")
        day2 = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT09:00:00Z")
        rows = [
            ("append_transcript", day1, "key_a"),
            ("append_transcript", day1, "key_a"),
            ("search_long_term", day1, "key_b"),
            ("search_transcript", day2, "key_b"),
            ("expand_history", day2, None),
        ]
        for operation, recorded_at, key_id in rows:
            self.catalog.record_usage_event(
                HostedUsageEvent(
                    kind="request",
                    operation=operation,
                    tenant_id=self.tenant_id,
                    principal_id="shared",
                    status="ok",
                    recorded_at=recorded_at,
                    project_id=project_id,
                    key_id=key_id,
                )
            )
        return day1[:10], day2[:10]

    def _provisioned_project(self) -> dict:
        project = self._create_project()
        tenant = self.client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        self.tenant_id = tenant["tenantId"]
        return project

    def test_daily_granularity_returns_bucketed_rows(self) -> None:
        project = self._provisioned_project()
        day1, day2 = self._seed_usage(project["id"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage"
            "?granularity=day&days=30",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        daily = response.json()["usage"]["daily"]
        by_date = {row["date"]: row for row in daily}
        self.assertEqual(by_date[day1]["writes"], 2)
        self.assertEqual(by_date[day1]["retrievals"], 1)
        self.assertEqual(by_date[day2]["retrievals"], 1)
        self.assertEqual(by_date[day2]["other"], 1)

    def test_usage_without_granularity_has_no_daily_array(self) -> None:
        project = self._provisioned_project()

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage",
            headers=self._control_auth(),
        )

        self.assertNotIn("daily", response.json()["usage"])

    def test_by_key_endpoint_aggregates_per_key(self) -> None:
        project = self._provisioned_project()
        day1, day2 = self._seed_usage(project["id"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage/by-key"
            "?days=30",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        by_key = {row["keyId"]: row["requests"] for row in response.json()["byKey"]}
        self.assertEqual(by_key["key_a"], 2)
        self.assertEqual(by_key["key_b"], 2)
        self.assertEqual(by_key[None], 1)

    def test_by_key_sorts_unattributed_bucket_last_on_tied_counts(self) -> None:
        project = self._provisioned_project()
        from datetime import UTC, datetime, timedelta

        day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00Z")
        for key_id in ("key_z", None):
            self.catalog.record_usage_event(
                HostedUsageEvent(
                    kind="request",
                    operation="append_transcript",
                    tenant_id=self.tenant_id,
                    principal_id="shared",
                    status="ok",
                    recorded_at=day,
                    project_id=project["id"],
                    key_id=key_id,
                )
            )

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage/by-key"
            "?days=30",
            headers=self._control_auth(),
        )

        rows = response.json()["byKey"]
        self.assertEqual([row["keyId"] for row in rows], ["key_z", None])

    def test_by_key_requires_control_credential(self) -> None:
        project = self._provisioned_project()

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage/by-key",
        )

        self.assertEqual(response.status_code, 401)


class JobEventProjectAttributionTests(ConsoleOpsDepthHarness):
    def _record(self, job_id: str, status: str, project_id: str | None, recorded_at: str) -> None:
        self.catalog.record_job_event(
            HostedJobEvent(
                job_id=job_id,
                operation="run_dream_phase",
                tenant_id="tenant-a",
                principal_id="shared",
                status=status,
                recorded_at=recorded_at,
                phase="light",
                project_id=project_id,
            )
        )

    def test_job_events_filter_by_project(self) -> None:
        self._record("job1", "ok", "proj_a", "2026-07-01T00:00:00Z")
        self._record("job2", "ok", "proj_b", "2026-07-01T01:00:00Z")
        self._record("job3", "ok", None, "2026-07-01T02:00:00Z")

        events = self.catalog.job_events("tenant-a", project_id="proj_a")

        self.assertEqual([event.job_id for event in events], ["job1"])

    def test_job_events_limit_returns_newest_first(self) -> None:
        for index in range(5):
            self._record(f"job{index}", "ok", "proj_a", f"2026-07-01T0{index}:00:00Z")

        events = self.catalog.job_events("tenant-a", project_id="proj_a", limit=2)

        self.assertEqual([event.job_id for event in events], ["job4", "job3"])

    def test_job_events_default_behavior_unchanged(self) -> None:
        self._record("job1", "ok", "proj_a", "2026-07-01T00:00:00Z")
        self._record("job2", "ok", "proj_b", "2026-07-01T01:00:00Z")

        events = self.catalog.job_events("tenant-a")

        self.assertEqual([event.job_id for event in events], ["job1", "job2"])


class JobsEndpointTests(ConsoleOpsDepthHarness):
    def _seed_jobs(self, project_id: str) -> None:
        for job_id, status, error_type in (
            ("job1", "running", None),
            ("job1", "ok", None),
            ("job2", "running", None),
            ("job2", "error", "HostPortNotConfigured"),
        ):
            self.catalog.record_job_event(
                HostedJobEvent(
                    job_id=job_id,
                    operation="run_dream_phase",
                    tenant_id=self.tenant_id,
                    principal_id="shared",
                    status=status,
                    recorded_at="2026-07-01T00:00:00Z",
                    phase="light",
                    error_type=error_type,
                    project_id=project_id,
                )
            )

    def _provisioned_project(self) -> dict:
        project = self._create_project()
        tenant = self.client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        self.tenant_id = tenant["tenantId"]
        return project

    def test_jobs_endpoint_returns_project_events_without_error_detail(self) -> None:
        project = self._provisioned_project()
        self._seed_jobs(project["id"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/jobs?limit=50",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        jobs = response.json()["jobs"]
        self.assertEqual(len(jobs), 4)
        statuses = {job["status"] for job in jobs}
        self.assertIn("error", statuses)
        for job in jobs:
            self.assertNotIn("errorType", job)
            self.assertNotIn("error_type", job)
            self.assertEqual(
                set(job), {"jobId", "operation", "phase", "status", "recordedAt"}
            )

    def test_jobs_endpoint_requires_credential_and_known_project(self) -> None:
        project = self._provisioned_project()

        unauthorized = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/jobs",
        )
        self.assertEqual(unauthorized.status_code, 401)

        missing = self.client.get(
            "/control/v1/clerk-orgs/org_123/projects/proj_missing/jobs",
            headers=self._control_auth(),
        )
        self.assertEqual(missing.status_code, 404)

    def test_jobs_endpoint_is_tenant_isolated(self) -> None:
        project = self._provisioned_project()
        self._seed_jobs(project["id"])
        other_project = self._create_project(org="org_other", name="Other")

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_other/projects/{other_project['id']}/jobs",
            headers=self._control_auth(),
        )

        self.assertEqual(response.json()["jobs"], [])


class SharedScopeAgentBindingTests(ConsoleOpsDepthHarness):
    """Regression: a shared/null-agent scope key must NOT act as an agent wildcard.

    "shared" means the null-agent (agent_id=None) scope ONLY. A shared-scope
    key is minted with agent_ids={None}, so the membership check in
    HostedMemoryService._bind_request rejects any request carrying an explicit
    agent_id and admits only agent_id=None.
    """

    def _shared_scope_request(self, project_id: str, agent_id: str | None):
        return FreshContextRequest(
            scope=MemoryScope(
                tenant_id=self._tenant_id,
                project_id=project_id,
                session_id="default",
                agent_id=agent_id,
                principal=Principal(
                    principal_id="caller-supplied",
                    principal_type=PrincipalType.HUMAN,
                ),
                trust_boundary=TrustBoundary.LOCAL_TRUSTED,
                capabilities={MemoryCapability.FRESH_CONTEXT},
            ),
            redaction=RedactionContext(forbidden_values=()),
        )

    def _authenticate_shared_key(self, project_id: str):
        created = self._create_key("org_123", project_id, name="shared-key")
        auth = self.keys.authenticate(created["rawKey"])
        self._tenant_id = auth.tenant_id
        return auth

    def test_shared_scope_key_persists_null_agent_id(self) -> None:
        project = self._create_project()
        auth = self._authenticate_shared_key(project["id"])

        # {None} (the shared/null-agent scope), never an empty wildcard set.
        self.assertEqual(auth.agent_ids, frozenset({None}))

        with contextlib.closing(self._control_db()) as conn:
            row = conn.execute(
                "SELECT agent_ids FROM hosted_api_keys WHERE key_id = ?",
                (auth.key_id,),
            ).fetchone()
        # None must survive the JSON round-trip through storage.
        self.assertEqual(json.loads(row[0]), [None])

    def test_shared_scope_key_rejects_explicit_agent_id(self) -> None:
        project = self._create_project()
        auth = self._authenticate_shared_key(project["id"])

        request = self._shared_scope_request(project["id"], agent_id="agent-x")
        with self.assertRaises(PermissionError):
            self.service._bind_request(
                auth, request, MemoryCapability.FRESH_CONTEXT
            )

    def test_shared_scope_key_admits_null_agent(self) -> None:
        project = self._create_project()
        auth = self._authenticate_shared_key(project["id"])

        request = self._shared_scope_request(project["id"], agent_id=None)
        # Authorization must pass for the null-agent scope (no PermissionError).
        bound, _tenant = self.service._bind_request(
            auth, request, MemoryCapability.FRESH_CONTEXT
        )
        self.assertIsNone(bound.scope.agent_id)

    def test_setup_token_exchange_shared_scope_rejects_explicit_agent_id(self) -> None:
        project = self._create_project()
        # Resolve the tenant id the console binds to this org.
        tenant_id = self._authenticate_shared_key(project["id"]).tenant_id

        provisioned, _record = self.keys.create_setup_token(
            tenant_id=tenant_id,
            project_id=project["id"],
            agent_scope="shared",
        )
        exchange = self.keys.exchange_setup_token(provisioned.raw_token)
        auth = self.keys.authenticate(exchange.provisioned.raw_key)
        self._tenant_id = auth.tenant_id

        self.assertEqual(auth.agent_ids, frozenset({None}))

        request = self._shared_scope_request(project["id"], agent_id="agent-x")
        with self.assertRaises(PermissionError):
            self.service._bind_request(
                auth, request, MemoryCapability.FRESH_CONTEXT
            )


if __name__ == "__main__":
    unittest.main()
