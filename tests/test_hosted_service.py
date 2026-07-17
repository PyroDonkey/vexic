from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.embeddings import EMBEDDING_DIM
from vexic.contract import (
    AppendTranscriptRequest,
    DeleteScopeRequest,
    DreamPhase,
    FreshContextRequest,
    MemoryCapability,
    MemoryScope,
    MemoryScopeSelector,
    Principal,
    PrincipalType,
    RedactionContext,
    RunDreamPhaseRequest,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import (
    HostedAuditEvent,
    HostedBackgroundJobRunner,
    HostedInMemoryRateLimiter,
    HostedJobEvent,
    HostedMemoryService,
    HostedRateLimitRule,
    HostedRateLimitExceeded,
    HostedUsageEvent,
)
from vexic.models import ContradictionJudgment, FactCandidate
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.ports import DreamPhasePorts, HostPortNotConfigured
from vexic.storage import save_messages, single_message_adapter


def _unit_vector(first: float = 1.0) -> list[float]:
    return [first] + [0.0] * (EMBEDDING_DIM - 1)


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
        session_id="default",
        agent_id=agent_id,
        principal=Principal(
            principal_id="caller-supplied",
            principal_type=PrincipalType.HUMAN,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities=capabilities,
    )


class _CountingTenantCatalog:
    def __init__(self, catalog: HostedTenantCatalog) -> None:
        self.catalog = catalog
        self.get_tenant_calls = 0

    def get_tenant(self, tenant_id: str):
        self.get_tenant_calls += 1
        return self.catalog.get_tenant(tenant_id)


class _FailingJobTelemetry:
    def __init__(self, catalog: HostedTenantCatalog) -> None:
        self.catalog = catalog

    def record_audit_event(self, event: HostedAuditEvent) -> None:
        self.catalog.record_audit_event(event)

    def record_usage_event(self, event: HostedUsageEvent) -> None:
        self.catalog.record_usage_event(event)

    def record_job_event(self, event: HostedJobEvent) -> None:
        raise RuntimeError("job telemetry unavailable")


class _FailingJobUsageTelemetry:
    def __init__(self, catalog: HostedTenantCatalog) -> None:
        self.catalog = catalog

    def record_audit_event(self, event: HostedAuditEvent) -> None:
        self.catalog.record_audit_event(event)

    def record_usage_event(self, event: HostedUsageEvent) -> None:
        if event.kind == "job":
            raise RuntimeError("job usage telemetry unavailable")
        self.catalog.record_usage_event(event)

    def record_job_event(self, event: HostedJobEvent) -> None:
        self.catalog.record_job_event(event)


class HostedMemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(root)
        self.keys = HostedApiKeyStore()
        self.service = HostedMemoryService(self.catalog, self.keys, telemetry=self.catalog)
        self.jobs = HostedBackgroundJobRunner(self.service)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_background_job_runner_requires_durable_telemetry(self) -> None:
        service = HostedMemoryService(self.catalog, self.keys)

        with self.assertRaisesRegex(ValueError, "requires durable telemetry"):
            HostedBackgroundJobRunner(service)

    def test_hosted_api_exposes_contract_operation_names(self) -> None:
        for method_name in (
            "append_transcript",
            "ingest_source_transcript",
            "search_transcript",
            "expand_history",
            "fresh_context",
            "search_long_term",
            "record_retrieval_event",
            "retire_fact",
            "run_dream_phase",
            "export_scope",
            "replay_scope",
            "rebuild",
            "delete_scope",
        ):
            with self.subTest(method_name=method_name):
                self.assertTrue(callable(getattr(self.service, method_name, None)))

    async def test_api_key_routes_to_authenticated_tenant_database(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="hosted cedar memory")])
        )
        append = await self.service.append_transcript(
            api_key.raw_key,
            AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[message_json],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )
        self.assertEqual(len(append.message_ids), 1)

        result = await self.service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar",
            ),
        )

        self.assertEqual([hit.body for hit in result.hits], ["User: hosted cedar memory"])

    async def test_fresh_context_dispatches_to_local_service_with_bound_scope(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.FRESH_CONTEXT},
            project_ids={"project-a"},
        )
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="fresh context cedar")])
        )
        await self.service.append_transcript(
            api_key.raw_key,
            AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[message_json],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )

        result = await self.service.fresh_context(
            api_key.raw_key,
            FreshContextRequest(
                scope=_scope(capabilities={MemoryCapability.FRESH_CONTEXT}),
                redaction=RedactionContext(forbidden_values=()),
            ),
        )

        self.assertEqual([hit.body for hit in result.recent], ["User: fresh context cedar"])

    async def test_fresh_context_requires_capability(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE},
            project_ids={"project-a"},
        )

        with self.assertRaises(PermissionError):
            await self.service.fresh_context(
                api_key.raw_key,
                FreshContextRequest(
                    scope=_scope(capabilities={MemoryCapability.FRESH_CONTEXT}),
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

    async def test_fresh_context_has_rate_limit_rule(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.FRESH_CONTEXT},
            project_ids={"project-a"},
        )
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(
                operation_rules={
                    "fresh_context": HostedRateLimitRule(limit=1, window_seconds=60),
                },
            ),
        )
        request = FreshContextRequest(
            scope=_scope(capabilities={MemoryCapability.FRESH_CONTEXT}),
            redaction=RedactionContext(forbidden_values=()),
        )

        await service.fresh_context(api_key.raw_key, request)
        with self.assertRaises(HostedRateLimitExceeded):
            await service.fresh_context(api_key.raw_key, request)

    async def test_catalog_reload_preserves_routing_projects_and_isolation(self) -> None:
        tenant_a = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        tenant_b = self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        key_a = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-a", "project-b"},
        )
        key_b = self.keys.create_key(
            tenant_id="tenant-b",
            principal_id="agent-b",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-b"},
        )

        await self.service.append_transcript(
            key_a.raw_key,
            AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="tenant a reload cedar")])
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )
        await self.service.append_transcript(
            key_b.raw_key,
            AppendTranscriptRequest(
                scope=_scope(
                    tenant_id="tenant-b",
                    project_id="project-b",
                    capabilities={MemoryCapability.WRITE},
                ),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="tenant b reload cedar")])
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )

        reloaded_catalog = HostedTenantCatalog(Path(self.temp_dir.name))
        self.assertEqual(reloaded_catalog.get_tenant("tenant-a").db_path, tenant_a.db_path)
        self.assertEqual(reloaded_catalog.get_tenant("tenant-b").db_path, tenant_b.db_path)
        self.assertEqual(
            reloaded_catalog.get_tenant("tenant-a").project_ids,
            frozenset({"project-a"}),
        )
        merged = reloaded_catalog.provision_tenant(
            "tenant-a",
            project_ids={"project-c"},
        )
        self.assertEqual(merged.db_path, tenant_a.db_path)
        self.assertEqual(merged.project_ids, frozenset({"project-a", "project-c"}))
        reloaded_service = HostedMemoryService(
            reloaded_catalog,
            self.keys,
            telemetry=reloaded_catalog,
        )

        result_a = await reloaded_service.search_transcript(
            key_a.raw_key,
            SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar",
            ),
        )
        result_b = await reloaded_service.search_transcript(
            key_b.raw_key,
            SearchTranscriptRequest(
                scope=_scope(
                    tenant_id="tenant-b",
                    project_id="project-b",
                    capabilities={MemoryCapability.SEARCH},
                ),
                query="cedar",
            ),
        )

        self.assertEqual([hit.body for hit in result_a.hits], ["User: tenant a reload cedar"])
        self.assertEqual([hit.body for hit in result_b.hits], ["User: tenant b reload cedar"])
        with self.assertRaisesRegex(PermissionError, "project_id is not provisioned"):
            await reloaded_service.search_transcript(
                key_a.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        project_id="project-b",
                        capabilities={MemoryCapability.SEARCH},
                    ),
                    query="cedar",
                ),
            )

    async def test_durable_api_key_store_reloads_scope_and_revocation_without_raw_key(self) -> None:
        root = Path(self.temp_dir.name)
        self.catalog.provision_tenant(
            "tenant-a",
            project_ids={"project-a", "project-b"},
        )
        self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        keys = HostedApiKeyStore(root)
        api_key = keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-a"},
            agent_ids={"memory-agent-a"},
        )
        raw_key_prefix = f"vx_{api_key.key_id}_"
        self.assertTrue(api_key.raw_key.startswith(raw_key_prefix))
        raw_key_secret = api_key.raw_key[len(raw_key_prefix) :]

        reloaded_keys = HostedApiKeyStore(root)
        auth = reloaded_keys.authenticate(api_key.raw_key)
        self.assertEqual(auth.key_id, api_key.key_id)
        self.assertEqual(auth.tenant_id, "tenant-a")
        self.assertEqual(auth.principal.principal_id, "agent-a")
        self.assertEqual(
            auth.capabilities,
            frozenset({MemoryCapability.WRITE, MemoryCapability.SEARCH}),
        )
        self.assertEqual(auth.project_ids, frozenset({"project-a"}))
        self.assertEqual(auth.agent_ids, frozenset({"memory-agent-a"}))

        service = HostedMemoryService(self.catalog, reloaded_keys, telemetry=self.catalog)
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="durable hosted cedar")])
        )
        await service.append_transcript(
            api_key.raw_key,
            AppendTranscriptRequest(
                scope=_scope(
                    capabilities={MemoryCapability.WRITE},
                    agent_id="memory-agent-a",
                ),
                messages_json=[message_json],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )
        result = await service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(
                    capabilities={MemoryCapability.SEARCH},
                    agent_id="memory-agent-a",
                ),
                query="cedar",
            ),
        )
        self.assertEqual([hit.body for hit in result.hits], ["User: durable hosted cedar"])

        with self.assertRaises(PermissionError):
            await service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        tenant_id="tenant-b",
                        project_id="project-b",
                        capabilities={MemoryCapability.SEARCH},
                        agent_id="memory-agent-a",
                    ),
                    query="cedar",
                ),
            )
        with self.assertRaises(PermissionError):
            await service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        project_id="project-b",
                        capabilities={MemoryCapability.SEARCH},
                        agent_id="memory-agent-a",
                    ),
                    query="cedar",
                ),
            )
        with self.assertRaises(PermissionError):
            await service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        capabilities={MemoryCapability.SEARCH},
                        agent_id="memory-agent-b",
                    ),
                    query="cedar",
                ),
            )
        with self.assertRaises(PermissionError):
            await service.run_dream_phase(
                api_key.raw_key,
                RunDreamPhaseRequest(
                    scope=_scope(
                        capabilities={MemoryCapability.ADMIN_REBUILD},
                        agent_id="memory-agent-a",
                    ),
                    phase=DreamPhase.LIGHT,
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

        control_db = root / "control-plane.db"
        control_db_bytes = control_db.read_bytes()
        self.assertNotIn(api_key.raw_key.encode("utf-8"), control_db_bytes)
        self.assertNotIn(raw_key_secret.encode("utf-8"), control_db_bytes)
        with closing(sqlite3.connect(control_db)) as conn:
            row = conn.execute(
                """
                SELECT
                    key_id, key_hash, tenant_id, principal_id, capabilities,
                    project_ids, agent_ids, revoked_at, revoked_by
                FROM hosted_api_keys
                WHERE key_id = ?
                """,
                (api_key.key_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], api_key.key_id)
        self.assertEqual(len(row[1]), 64)
        self.assertNotEqual(row[1], api_key.raw_key)
        self.assertEqual(row[2], "tenant-a")
        self.assertEqual(row[3], "agent-a")
        self.assertEqual(
            json.loads(row[4]),
            sorted([MemoryCapability.WRITE.value, MemoryCapability.SEARCH.value]),
        )
        self.assertEqual(json.loads(row[5]), ["project-a"])
        self.assertEqual(json.loads(row[6]), ["memory-agent-a"])
        self.assertIsNone(row[7])
        self.assertIsNone(row[8])

        ledger_text = repr(self.catalog.audit_events("tenant-a")) + repr(
            self.catalog.usage_events("tenant-a")
        )
        self.assertNotIn(api_key.raw_key, ledger_text)
        self.assertNotIn(raw_key_secret, ledger_text)

        reloaded_keys.revoke_key(api_key.key_id, revoked_by="operator-a")
        revoked_keys = HostedApiKeyStore(root)
        revoked_service = HostedMemoryService(self.catalog, revoked_keys)
        with self.assertRaises(PermissionError):
            revoked_keys.authenticate(api_key.raw_key)
        with self.assertRaises(PermissionError):
            await revoked_service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        capabilities={MemoryCapability.SEARCH},
                        agent_id="memory-agent-a",
                    ),
                    query="cedar",
                ),
            )
        control_db_bytes = control_db.read_bytes()
        self.assertNotIn(api_key.raw_key.encode("utf-8"), control_db_bytes)
        self.assertNotIn(raw_key_secret.encode("utf-8"), control_db_bytes)
        with closing(sqlite3.connect(control_db)) as conn:
            row = conn.execute(
                """
                SELECT key_id, tenant_id, principal_id, revoked_at, revoked_by
                FROM hosted_api_keys
                WHERE key_id = ?
                """,
                (api_key.key_id,),
            ).fetchone()
        self.assertEqual(row[0], api_key.key_id)
        self.assertEqual(row[1], "tenant-a")
        self.assertEqual(row[2], "agent-a")
        self.assertIsNotNone(row[3])
        self.assertEqual(row[4], "operator-a")
        self.assertNotIn(api_key.raw_key, repr(row))
        self.assertNotIn(raw_key_secret, repr(row))

    def test_control_plane_key_creation_failure_does_not_leave_live_key(self) -> None:
        root = Path(self.temp_dir.name)
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        keys = HostedApiKeyStore(root)
        original_connect = keys._connect_control

        class _FailMetadataInsertConnection:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self._conn = conn

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if "INSERT INTO hosted_api_key_metadata" in sql:
                    raise sqlite3.IntegrityError("metadata write failed")
                return self._conn.execute(sql, params)

            def commit(self) -> None:
                self._conn.commit()

            def close(self) -> None:
                self._conn.close()

            def __getattr__(self, name: str) -> object:
                return getattr(self._conn, name)

        def failing_connect() -> _FailMetadataInsertConnection:
            return _FailMetadataInsertConnection(original_connect())

        raw_key = "vx_deadbeefcafebabe_deterministic-secret"
        with (
            patch.object(keys, "_connect_control", side_effect=failing_connect),
            patch("vexic.hosted_local.secrets.token_hex", return_value="deadbeefcafebabe"),
            patch("vexic.hosted_local.secrets.token_urlsafe", return_value="deterministic-secret"),
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "metadata write failed"):
                keys.create_control_plane_key(
                    tenant_id="tenant-a",
                    project_id="project-a",
                    name="Worker",
                )

        with self.assertRaisesRegex(PermissionError, "Invalid hosted API key."):
            keys.authenticate(raw_key)

    def test_durable_api_key_store_rejects_corrupt_key_rows(self) -> None:
        root = Path(self.temp_dir.name)
        corruptions = (
            ("capabilities", json.dumps(["not-a-capability"])),
            ("project_ids", "not-json"),
        )
        for column_name, corrupt_value in corruptions:
            with self.subTest(column_name=column_name):
                keys = HostedApiKeyStore(root)
                api_key = keys.create_key(
                    tenant_id="tenant-a",
                    principal_id="agent-a",
                    capabilities={MemoryCapability.SEARCH},
                )
                with closing(sqlite3.connect(root / "control-plane.db")) as conn:
                    conn.execute(
                        f"""
                        UPDATE hosted_api_keys
                        SET {column_name} = ?
                        WHERE key_id = ?
                        """,
                        (corrupt_value, api_key.key_id),
                    )
                    conn.commit()

                with self.assertRaisesRegex(
                    PermissionError,
                    "Invalid hosted API key.",
                ):
                    HostedApiKeyStore(root).authenticate(api_key.raw_key)

    def test_durable_control_plane_database_is_owner_read_write_only(self) -> None:
        root = Path(self.temp_dir.name) / "durable-control"
        control_db = root / "control-plane.db"

        with patch("os.chmod", wraps=os.chmod) as chmod:
            HostedTenantCatalog(root)
            HostedApiKeyStore(root)

        chmod_control_calls = [
            call_args
            for call_args in chmod.call_args_list
            if call_args.args == (control_db, 0o600)
        ]
        self.assertGreaterEqual(len(chmod_control_calls), 2)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(control_db.stat().st_mode), 0o600)

    async def test_successful_request_records_sanitized_audit_and_usage(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

        await self.service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar",
            ),
        )

        audit_events = self.catalog.audit_events("tenant-a")
        usage_events = self.catalog.usage_events("tenant-a")
        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0].operation, "search_transcript")
        self.assertEqual(audit_events[0].tenant_id, "tenant-a")
        self.assertEqual(audit_events[0].principal_id, "agent-a")
        self.assertEqual(audit_events[0].status, "ok")
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0].kind, "request")
        self.assertEqual(usage_events[0].operation, "search_transcript")

        ledger_text = repr(audit_events) + repr(usage_events)
        self.assertNotIn(api_key.raw_key, ledger_text)
        self.assertNotIn("cedar", ledger_text)

    async def test_reloaded_catalog_reads_sanitized_request_ledgers_from_control_plane(
        self,
    ) -> None:
        root = Path(self.temp_dir.name)
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="persisted transcript pine")])
        )

        await self.service.append_transcript(
            api_key.raw_key,
            AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[message_json],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )
        await self.service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="pine query text",
            ),
        )

        reloaded_catalog = HostedTenantCatalog(root)
        audit_events = reloaded_catalog.audit_events("tenant-a")
        usage_events = reloaded_catalog.usage_events("tenant-a")

        self.assertEqual(
            [event.operation for event in audit_events],
            ["append_transcript", "search_transcript"],
        )
        self.assertEqual([event.status for event in audit_events], ["ok", "ok"])
        self.assertEqual(
            [event.operation for event in usage_events],
            ["append_transcript", "search_transcript"],
        )
        self.assertEqual([event.status for event in usage_events], ["ok", "ok"])

        control_plane_bytes = (root / "control-plane.db").read_bytes()
        self.assertIn(b"search_transcript", control_plane_bytes)
        ledger_text = repr(audit_events) + repr(usage_events)
        for forbidden in (
            api_key.raw_key,
            "pine query text",
            "persisted transcript pine",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, ledger_text)
                self.assertNotIn(forbidden.encode("utf-8"), control_plane_bytes)

    async def test_invalid_api_key_records_sanitized_null_tenant_ledgers_on_reload(
        self,
    ) -> None:
        root = Path(self.temp_dir.name)
        bad_key = "vx_badkey_bad-secret-value"

        with self.assertRaises(PermissionError):
            await self.service.search_transcript(
                bad_key,
                SearchTranscriptRequest(
                    scope=_scope(capabilities={MemoryCapability.SEARCH}),
                    query="raw invalid-key query text",
                ),
            )

        reloaded_catalog = HostedTenantCatalog(root)
        audit_events = reloaded_catalog.audit_events(None)
        usage_events = reloaded_catalog.usage_events(None)

        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0].operation, "search_transcript")
        self.assertIsNone(audit_events[0].tenant_id)
        self.assertIsNone(audit_events[0].principal_id)
        self.assertEqual(audit_events[0].status, "error")
        self.assertEqual(audit_events[0].error_type, "PermissionError")
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0].kind, "request")
        self.assertEqual(usage_events[0].status, "error")
        self.assertEqual(usage_events[0].error_type, "PermissionError")

        control_plane_bytes = (root / "control-plane.db").read_bytes()
        ledger_text = repr(audit_events) + repr(usage_events)
        for forbidden in (bad_key, "raw invalid-key query text"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, ledger_text)
                self.assertNotIn(forbidden.encode("utf-8"), control_plane_bytes)

    def test_usage_counter_fields_survive_catalog_reload(self) -> None:
        root = Path(self.temp_dir.name)
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        self.catalog.record_usage_event(
            HostedUsageEvent(
                kind="request",
                operation="run_dream_phase",
                tenant_id="tenant-a",
                principal_id="agent-a",
                status="ok",
                recorded_at="2026-06-23T00:00:00Z",
                model_requests=2,
                input_tokens=300,
                output_tokens=125,
                total_tokens=425,
                estimated_cost_micros=9876,
            )
        )

        [usage_event] = HostedTenantCatalog(root).usage_events("tenant-a")

        self.assertEqual(usage_event.model_requests, 2)
        self.assertEqual(usage_event.input_tokens, 300)
        self.assertEqual(usage_event.output_tokens, 125)
        self.assertEqual(usage_event.total_tokens, 425)
        self.assertEqual(usage_event.estimated_cost_micros, 9876)

    def test_usage_events_can_filter_by_project_and_recorded_at(self) -> None:
        self.catalog.provision_tenant(
            "tenant-a",
            project_ids={"project-a", "project-b"},
        )
        for event in (
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="agent-a",
                status="ok",
                recorded_at="2026-06-01T00:00:00.123456Z",
                project_id="project-a",
            ),
            HostedUsageEvent(
                kind="request",
                operation="search_transcript",
                tenant_id="tenant-a",
                principal_id="agent-a",
                status="ok",
                recorded_at="2026-06-10T00:00:00Z",
                project_id="project-a",
            ),
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="agent-a",
                status="ok",
                recorded_at="2026-05-31T23:59:59Z",
                project_id="project-a",
            ),
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="agent-a",
                status="ok",
                recorded_at="2026-06-10T00:00:00Z",
                project_id="project-b",
            ),
        ):
            self.catalog.record_usage_event(event)

        usage_events = self.catalog.usage_events(
            "tenant-a",
            project_id="project-a",
            recorded_at_gte="2026-06-01T00:00:00Z",
            recorded_at_lt="2026-07-01T00:00:00Z",
        )

        self.assertEqual(
            [event.recorded_at for event in usage_events],
            ["2026-06-01T00:00:00.123456Z", "2026-06-10T00:00:00Z"],
        )
        self.assertEqual([event.project_id for event in usage_events], ["project-a", "project-a"])

    def test_usage_events_project_time_window_uses_julianday_index(self) -> None:
        root = Path(self.temp_dir.name)
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        self.catalog.record_usage_event(
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="agent-a",
                status="ok",
                recorded_at="2026-06-01T00:00:00.123456Z",
                project_id="project-a",
            )
        )

        with closing(sqlite3.connect(root / "control-plane.db")) as conn:
            plan_rows = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT kind, operation, tenant_id, principal_id, status, recorded_at,
                       model_requests, input_tokens, output_tokens, total_tokens,
                       estimated_cost_micros, error_type, project_id
                FROM hosted_usage_events
                WHERE tenant_id = ?
                  AND project_id = ?
                  AND julianday(recorded_at) >= julianday(?)
                  AND julianday(recorded_at) < julianday(?)
                ORDER BY id
                """,
                (
                    "tenant-a",
                    "project-a",
                    "2026-06-01T00:00:00Z",
                    "2026-07-01T00:00:00Z",
                ),
            ).fetchall()

        plan = " ".join(str(row) for row in plan_rows)
        self.assertIn("idx_hosted_usage_events_tenant_project_recorded_at_jd", plan)

    async def test_telemetry_is_filtered_per_tenant_in_control_plane(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        key_a = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )
        key_b = self.keys.create_key(
            tenant_id="tenant-b",
            principal_id="agent-b",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-b"},
        )

        await self.service.search_transcript(
            key_a.raw_key,
            SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar",
            ),
        )
        await self.service.search_transcript(
            key_b.raw_key,
            SearchTranscriptRequest(
                scope=_scope(
                    tenant_id="tenant-b",
                    project_id="project-b",
                    capabilities={MemoryCapability.SEARCH},
                ),
                query="cedar",
            ),
        )

        self.assertEqual([event.tenant_id for event in self.catalog.audit_events("tenant-a")], ["tenant-a"])
        self.assertEqual([event.tenant_id for event in self.catalog.audit_events("tenant-b")], ["tenant-b"])

    async def test_tenant_scoped_api_key_accepts_scope_without_project_id(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
        )

        result = await self.service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(
                    project_id=None,
                    capabilities={MemoryCapability.SEARCH},
                ),
                query="cedar",
            ),
        )

        self.assertEqual(result.hits, [])

    async def test_hosted_request_reuses_catalog_tenant_lookup(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        counting_catalog = _CountingTenantCatalog(self.catalog)
        service = HostedMemoryService(counting_catalog, self.keys)
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

        result = await service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar",
            ),
        )

        self.assertEqual(result.hits, [])
        self.assertEqual(counting_catalog.get_tenant_calls, 1)

    async def test_project_scoped_api_key_requires_project_id(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

        with self.assertRaisesRegex(
            PermissionError,
            "project_id is required for project-scoped API key",
        ):
            await self.service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        project_id=None,
                        capabilities={MemoryCapability.SEARCH},
                    ),
                    query="cedar",
                ),
            )

        audit_events = self.catalog.audit_events("tenant-a")
        usage_events = self.catalog.usage_events("tenant-a")
        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0].operation, "search_transcript")
        self.assertEqual(audit_events[0].tenant_id, "tenant-a")
        self.assertEqual(audit_events[0].principal_id, "agent-a")
        self.assertEqual(audit_events[0].status, "error")
        self.assertEqual(audit_events[0].error_type, "PermissionError")
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0].kind, "request")
        self.assertEqual(usage_events[0].operation, "search_transcript")
        self.assertEqual(usage_events[0].tenant_id, "tenant-a")
        self.assertEqual(usage_events[0].principal_id, "agent-a")
        self.assertEqual(usage_events[0].status, "error")
        self.assertEqual(usage_events[0].error_type, "PermissionError")
        self.assertNotIn(api_key.raw_key, repr(audit_events) + repr(usage_events))
        self.assertNotIn("cedar", repr(audit_events) + repr(usage_events))

    async def test_delete_scope_binds_target_scope_project_to_api_key(self) -> None:
        # A project-A lifecycle key must not tombstone project-B (or a
        # None/wildcard project) in the same tenant via target_scope.
        self.catalog.provision_tenant(
            "tenant-a", project_ids={"project-a", "project-b"}
        )
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_LIFECYCLE},
            project_ids={"project-a"},
        )

        def _delete_request(target_project_id: str | None) -> DeleteScopeRequest:
            return DeleteScopeRequest(
                scope=_scope(
                    project_id="project-a",
                    capabilities={MemoryCapability.ADMIN_LIFECYCLE},
                ),
                target_scope=MemoryScopeSelector(
                    tenant_id="tenant-a",
                    project_id=target_project_id,
                ),
                reason="regression",
                redaction=RedactionContext(forbidden_values=()),
            )

        with self.assertRaisesRegex(
            PermissionError,
            "Target scope project_id is not allowed for API key",
        ):
            await self.service.delete_scope(
                api_key.raw_key, _delete_request("project-b")
            )

        with self.assertRaisesRegex(
            PermissionError,
            "Target scope project_id is required for project-scoped API key",
        ):
            await self.service.delete_scope(
                api_key.raw_key, _delete_request(None)
            )

        result = await self.service.delete_scope(
            api_key.raw_key, _delete_request("project-a")
        )
        self.assertEqual(
            result.tombstone.target_scope.project_id, "project-a"
        )

    async def test_api_key_rejects_tenant_switch_and_capability_escalation(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

        with self.assertRaises(PermissionError):
            await self.service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        tenant_id="tenant-b",
                        project_id="project-b",
                        capabilities={MemoryCapability.SEARCH},
                    ),
                    query="cedar",
                ),
            )

        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="blocked write")])
        )
        with self.assertRaises(PermissionError):
            await self.service.append_transcript(
                api_key.raw_key,
                AppendTranscriptRequest(
                    scope=_scope(capabilities={MemoryCapability.WRITE}),
                    messages_json=[message_json],
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

    async def test_api_key_restricts_agent_scope_without_principal_fallback(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="runtime-agent",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-a"},
            agent_ids={"memory-agent-a"},
        )
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="agent scoped cedar")])
        )

        await self.service.append_transcript(
            api_key.raw_key,
            AppendTranscriptRequest(
                scope=_scope(
                    capabilities={MemoryCapability.WRITE},
                    agent_id="memory-agent-a",
                ),
                messages_json=[message_json],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )
        result = await self.service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(
                    capabilities={MemoryCapability.SEARCH},
                    agent_id="memory-agent-a",
                ),
                query="cedar",
            ),
        )

        self.assertEqual([hit.body for hit in result.hits], ["User: agent scoped cedar"])
        for widened_agent_id in ("runtime-agent", "memory-agent-b", None):
            with self.subTest(widened_agent_id=widened_agent_id):
                with self.assertRaises(PermissionError):
                    await self.service.search_transcript(
                        api_key.raw_key,
                        SearchTranscriptRequest(
                            scope=_scope(
                                capabilities={MemoryCapability.SEARCH},
                                agent_id=widened_agent_id,
                            ),
                            query="cedar",
                        ),
                    )

    async def test_empty_project_scope_denies_project_access(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
        )

        with self.assertRaises(PermissionError):
            await self.service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(capabilities={MemoryCapability.SEARCH}),
                    query="cedar",
                ),
            )

    async def test_revoked_api_key_is_rejected(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

        self.keys.revoke_key(api_key.key_id)

        with self.assertRaises(PermissionError):
            await self.service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(capabilities={MemoryCapability.SEARCH}),
                    query="cedar",
                ),
            )

    async def test_key_bound_to_retired_project_is_rejected_at_bind(self) -> None:
        """Retiring a control project cuts live access for its keys.

        Enforcement is binding-level: every data-plane route passes through
        ``_bind_request``, whose project check reads the retirement-filtered
        ``HostedTenant.project_ids``. The credential layer stays untouched —
        the key is not revoked, so un-retiring restores access.
        """
        self.catalog.provision_tenant("tenant-a")
        project = self.catalog.create_control_project("tenant-a", name="Alpha")
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={project.project_id},
        )

        self.catalog.retire_control_project("tenant-a", project.project_id)

        with self.assertRaises(PermissionError):
            await self.service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(
                        project_id=project.project_id,
                        capabilities={MemoryCapability.SEARCH},
                    ),
                    query="cedar",
                ),
            )

    async def test_retire_tenant_cuts_data_plane_access(self) -> None:
        """Retiring a tenant cuts live access for previously valid keys.

        Pins the ``active = 1`` predicate in ``get_tenant`` as the contract
        that makes ``retire_tenant`` an access cut (ADR 0028 addendum), not an
        audit marker.
        """
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

        self.catalog.retire_tenant("tenant-a")

        with self.assertRaisesRegex(PermissionError, "Unknown hosted tenant"):
            await self.service.search_transcript(
                api_key.raw_key,
                SearchTranscriptRequest(
                    scope=_scope(capabilities={MemoryCapability.SEARCH}),
                    query="cedar",
                ),
            )

    async def test_redaction_failure_records_no_payload_and_persists_nothing(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="cedar-secret")])
        )

        with self.assertRaises(ValueError):
            await self.service.append_transcript(
                api_key.raw_key,
                AppendTranscriptRequest(
                    scope=_scope(capabilities={MemoryCapability.WRITE}),
                    messages_json=[message_json],
                    redaction=RedactionContext(forbidden_values=("cedar-secret",)),
                ),
            )

        result = await self.service.search_transcript(
            api_key.raw_key,
            SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar-secret",
            ),
        )
        self.assertEqual(result.hits, [])
        audit_events = self.catalog.audit_events("tenant-a")
        usage_events = self.catalog.usage_events("tenant-a")
        self.assertEqual(audit_events[0].status, "error")
        self.assertNotIn(
            "cedar-secret",
            repr(audit_events) + repr(usage_events),
        )

    async def test_rate_limit_rejection_records_sanitized_audit_and_usage(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(
                default_rule=HostedRateLimitRule(limit=1, window_seconds=60),
            ),
        )
        request = SearchTranscriptRequest(
            scope=_scope(capabilities={MemoryCapability.SEARCH}),
            query="cedar",
        )

        await service.search_transcript(api_key.raw_key, request)
        with self.assertRaises(HostedRateLimitExceeded):
            await service.search_transcript(api_key.raw_key, request)

        audit_events = self.catalog.audit_events("tenant-a")
        usage_events = self.catalog.usage_events("tenant-a")
        self.assertEqual([event.status for event in audit_events], ["ok", "rate_limited"])
        self.assertEqual(audit_events[-1].error_type, "HostedRateLimitExceeded")
        self.assertEqual(usage_events[-1].error_type, "HostedRateLimitExceeded")
        self.assertNotIn(api_key.raw_key, repr(audit_events) + repr(usage_events))
        self.assertNotIn("cedar", repr(audit_events) + repr(usage_events))

    async def test_rate_limit_window_allows_later_request(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )
        now = 0.0
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            rate_limiter=HostedInMemoryRateLimiter(
                default_rule=HostedRateLimitRule(limit=1, window_seconds=10),
                clock=lambda: now,
            ),
        )
        request = SearchTranscriptRequest(
            scope=_scope(capabilities={MemoryCapability.SEARCH}),
            query="cedar",
        )

        await service.search_transcript(api_key.raw_key, request)
        with self.assertRaises(HostedRateLimitExceeded):
            await service.search_transcript(api_key.raw_key, request)
        now = 11.0

        result = await service.search_transcript(api_key.raw_key, request)

        self.assertEqual(result.hits, [])

    async def test_expensive_operation_quota_blocks_before_dream_host_port(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD},
            project_ids={"project-a"},
        )
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(
                operation_rules={
                    "run_dream_phase": HostedRateLimitRule(limit=1, window_seconds=60),
                },
            ),
        )
        request = RunDreamPhaseRequest(
            scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
            phase=DreamPhase.LIGHT,
            redaction=RedactionContext(forbidden_values=()),
        )

        with self.assertRaises(HostPortNotConfigured):
            await service.run_dream_phase(api_key.raw_key, request)
        with self.assertRaises(HostedRateLimitExceeded):
            await service.run_dream_phase(api_key.raw_key, request)

        audit_events = self.catalog.audit_events("tenant-a")
        self.assertEqual([event.status for event in audit_events], ["error", "rate_limited"])
        self.assertEqual(audit_events[0].error_type, "HostPortNotConfigured")
        self.assertEqual(audit_events[1].error_type, "HostedRateLimitExceeded")

    def test_rate_limiter_honors_limit_under_concurrent_calls(self) -> None:
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
        )
        auth = self.keys.authenticate(api_key.raw_key)
        limiter = HostedInMemoryRateLimiter(
            default_rule=HostedRateLimitRule(limit=5, window_seconds=60),
        )

        def attempt(_: int) -> bool:
            try:
                limiter.check("search_transcript", auth)
            except HostedRateLimitExceeded:
                return False
            return True

        with ThreadPoolExecutor(max_workers=10) as pool:
            allowed = list(pool.map(attempt, range(20)))

        self.assertEqual(sum(allowed), 5)

    def test_rate_limiter_bucket_cap_returns_retry_after(self) -> None:
        first_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
        )
        second_key = self.keys.create_key(
            tenant_id="tenant-b",
            principal_id="agent-b",
            capabilities={MemoryCapability.SEARCH},
        )
        limiter = HostedInMemoryRateLimiter(
            default_rule=HostedRateLimitRule(limit=10, window_seconds=60),
            max_buckets=1,
            clock=lambda: 0.0,
        )

        limiter.check("search_transcript", self.keys.authenticate(first_key.raw_key))
        with self.assertRaises(HostedRateLimitExceeded) as caught:
            limiter.check("search_transcript", self.keys.authenticate(second_key.raw_key))

        self.assertGreaterEqual(caught.exception.retry_after_seconds, 1)

    def test_rate_limiter_defers_prune_until_oldest_bucket_expires(self) -> None:
        prune_calls = 0
        now = 0.0

        class CountingRateLimiter(HostedInMemoryRateLimiter):
            def _prune(self, current_time: float) -> None:
                nonlocal prune_calls
                prune_calls += 1
                super()._prune(current_time)

        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.SEARCH},
        )
        limiter = CountingRateLimiter(
            default_rule=HostedRateLimitRule(limit=10, window_seconds=10),
            clock=lambda: now,
        )
        auth = self.keys.authenticate(api_key.raw_key)

        limiter.check("search_transcript", auth)
        now = 1.0
        limiter.check("search_transcript", auth)

        self.assertEqual(prune_calls, 0)

        now = 10.0
        limiter.check("search_transcript", auth)

        self.assertEqual(prune_calls, 1)

    def test_tenant_database_path_is_catalog_mapped_not_tenant_interpolated(self) -> None:
        tenant = self.catalog.provision_tenant("../tenant-a", project_ids={"project-a"})

        self.assertEqual(tenant.db_path.parent, Path(self.temp_dir.name))
        self.assertNotIn("..", tenant.db_path.name)
        self.assertNotIn("tenant-a", tenant.db_path.name)

    def test_project_can_be_provisioned_for_existing_tenant(self) -> None:
        self.catalog.provision_tenant("tenant-a")
        tenant = self.catalog.provision_project("tenant-a", "project-a")

        self.assertEqual(tenant.project_ids, frozenset({"project-a"}))

    def test_repeated_tenant_provisioning_merges_projects(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-b"})

        self.assertEqual(tenant.project_ids, frozenset({"project-a", "project-b"}))

    def test_customer_account_tenant_can_be_resolved_without_provisioning(self) -> None:
        root = Path(self.temp_dir.name)

        self.assertIsNone(self.catalog.resolve_customer_tenant("org_missing"))
        with closing(sqlite3.connect(root / "control-plane.db")) as conn:
            tenants = conn.execute("SELECT tenant_id FROM tenants").fetchall()
            mappings = conn.execute(
                "SELECT clerk_org_id, tenant_id FROM customer_account_mappings"
            ).fetchall()

        self.assertEqual(tenants, [])
        self.assertEqual(mappings, [])
        self.assertEqual(list(root.glob("customer-*.db")), [])

        tenant_id = self.catalog.provision_customer_account("org_123")

        self.assertEqual(self.catalog.resolve_customer_tenant("org_123"), tenant_id)
        with self.assertRaisesRegex(ValueError, "clerk_org_id must not be blank"):
            self.catalog.resolve_customer_tenant(" ")

    def test_resolve_customer_tenant_skips_half_provisioned_tenant(self) -> None:
        # Simulate `provision_customer_account` interrupted after committing
        # the mapping + inactive tenant row but before `provision_tenant`
        # finished customer-db init: the read path must not resolve it.
        control_db = Path(self.temp_dir.name) / "control-plane.db"
        with closing(sqlite3.connect(control_db)) as conn:
            conn.execute(
                """
                INSERT INTO tenants (tenant_id, db_filename, active)
                VALUES ('tenant_half', 'customer-half.db', 0)
                """
            )
            conn.execute(
                """
                INSERT INTO customer_account_mappings (clerk_org_id, tenant_id)
                VALUES ('org_half', 'tenant_half')
                """
            )
            conn.commit()

        self.assertIsNone(self.catalog.resolve_customer_tenant("org_half"))

        # The write path heals the interrupted provisioning, after which the
        # tenant resolves.
        tenant_id = self.catalog.provision_customer_account("org_half")

        self.assertEqual(tenant_id, "tenant_half")
        self.assertEqual(self.catalog.resolve_customer_tenant("org_half"), "tenant_half")

    def test_customer_account_provisioning_handles_competing_mapping_claim(self) -> None:
        original = self.catalog.provision_tenant
        control_db = Path(self.temp_dir.name) / "control-plane.db"

        def simulate_competing_claim(
            tenant_id: str,
            *,
            project_ids: set[str] | frozenset[str] = frozenset(),
        ):
            tenant = original(tenant_id, project_ids=project_ids)
            with closing(sqlite3.connect(control_db)) as conn:
                row = conn.execute(
                    """
                    SELECT tenant_id
                    FROM customer_account_mappings
                    WHERE clerk_org_id = ?
                    """,
                    ("org_123",),
                ).fetchone()
                if row is None:
                    original("tenant-racer")
                    conn.execute(
                        """
                        INSERT INTO customer_account_mappings (clerk_org_id, tenant_id)
                        VALUES (?, ?)
                        """,
                        ("org_123", "tenant-racer"),
                    )
                    conn.commit()
            return tenant

        with patch.object(self.catalog, "provision_tenant", side_effect=simulate_competing_claim):
            tenant_id = self.catalog.provision_customer_account("org_123")

        with closing(sqlite3.connect(control_db)) as conn:
            rows = conn.execute(
                """
                SELECT tenant_id
                FROM customer_account_mappings
                WHERE clerk_org_id = ?
                """,
                ("org_123",),
            ).fetchall()

        self.assertEqual(rows, [(tenant_id,)])
        self.assertEqual(self.catalog.get_tenant(tenant_id).tenant_id, tenant_id)

    def test_inactive_tenant_can_be_provisioned_again_with_existing_database(self) -> None:
        original = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        with closing(sqlite3.connect(Path(self.temp_dir.name) / "control-plane.db")) as conn:
            conn.execute("UPDATE tenants SET active = 0 WHERE tenant_id = ?", ("tenant-a",))
            conn.commit()

        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-b"})

        self.assertEqual(tenant.db_path, original.db_path)
        self.assertEqual(tenant.project_ids, frozenset({"project-a", "project-b"}))
        self.assertEqual(self.catalog.get_tenant("tenant-a"), tenant)

    def test_failed_tenant_initialization_retries_existing_database_path(self) -> None:
        created_paths: list[Path] = []

        def fail_once(service: object) -> None:
            db_path = Path(getattr(service, "db_path"))
            created_paths.append(db_path)
            db_path.touch()
            raise RuntimeError("customer db init failed")

        with patch("vexic.hosted_local.LocalMemoryService.init_schema", fail_once):
            with self.assertRaisesRegex(RuntimeError, "customer db init failed"):
                self.catalog.provision_tenant("tenant-a")

        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})

        self.assertEqual(tenant.db_path, created_paths[0])
        self.assertEqual(tenant.project_ids, frozenset({"project-a"}))
        self.assertEqual(self.catalog.get_tenant("tenant-a"), tenant)
        self.assertEqual(
            sorted(path.name for path in Path(self.temp_dir.name).glob("customer-*.db")),
            [created_paths[0].name],
        )

    async def test_hosted_dream_worker_runs_light_rem_deep_with_fake_ports(
        self,
    ) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        self.catalog.provision_tenant("tenant-b", project_ids={"project-b"})
        key_a = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={
                MemoryCapability.ADMIN_REBUILD,
                MemoryCapability.SEARCH,
                MemoryCapability.WRITE,
            },
            project_ids={"project-a"},
        )
        key_b = self.keys.create_key(
            tenant_id="tenant-b",
            principal_id="agent-b",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-b"},
        )

        class ExtractionAgent:
            async def run(self, transcript: str) -> object:
                message_id = int(re.search(r"message_id=(\d+)", transcript).group(1))
                return SimpleNamespace(
                    output=[
                        FactCandidate(
                            fact_text="Ryan prefers compact hosted reports.",
                            subject="Ryan",
                            category="preference",
                            importance=7,
                            confidence=0.9,
                            source_message_ids=[message_id],
                        )
                    ],
                    usage=lambda: SimpleNamespace(
                        requests=1,
                        input_tokens=11,
                        output_tokens=7,
                        total_tokens=18,
                    ),
                )

        class ContradictionAgent:
            async def run(self, prompt: str) -> object:
                return SimpleNamespace(
                    output=ContradictionJudgment(
                        contradicts=False,
                        confidence=0.9,
                    ),
                    usage=lambda: SimpleNamespace(
                        requests=1,
                        input_tokens=4,
                        output_tokens=1,
                        total_tokens=5,
                    ),
                )

        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=DreamPhasePorts(
                model_group="fake",
                embed=lambda texts: [_unit_vector() for _ in texts],
                extraction_agent_factory=lambda *_args, **_kwargs: ExtractionAgent(),
                contradiction_agent_factory=lambda *_args, **_kwargs: ContradictionAgent(),
            ),
        )
        jobs = HostedBackgroundJobRunner(service)
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="compact hosted reports")])
        )

        await service.append_transcript(
            key_a.raw_key,
            AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[message_json],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )
        for phase in (DreamPhase.LIGHT, DreamPhase.REM, DreamPhase.DEEP):
            result = await jobs.run_dream_phase(
                key_a.raw_key,
                RunDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                    phase=phase,
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )
            self.assertEqual(result.status, "ok")

        result_a = await service.search_long_term(
            key_a.raw_key,
            SearchLongTermRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="compact reports",
            ),
        )
        result_b = await service.search_long_term(
            key_b.raw_key,
            SearchLongTermRequest(
                scope=_scope(
                    tenant_id="tenant-b",
                    project_id="project-b",
                    capabilities={MemoryCapability.SEARCH},
                ),
                query="compact reports",
            ),
        )

        self.assertEqual(
            [fact.fact_text for fact in result_a.facts],
            ["Ryan prefers compact hosted reports."],
        )
        self.assertEqual(result_b.facts, [])
        self.assertEqual([event.status for event in jobs.job_events], ["running", "ok"] * 3)
        job_usage = [
            event
            for event in self.catalog.usage_events("tenant-a")
            if event.kind == "job"
        ]
        self.assertEqual([event.status for event in job_usage], ["ok", "ok", "ok"])
        self.assertEqual(job_usage[0].model_requests, 1)
        self.assertEqual(job_usage[0].input_tokens, 11)
        self.assertEqual(job_usage[0].output_tokens, 7)
        self.assertEqual(job_usage[0].total_tokens, 18)
        # The REM phase is a local centrality heuristic: no model calls, so its
        # job usage event reports all zeros.
        self.assertEqual(job_usage[1].model_requests, 0)
        self.assertEqual(job_usage[1].input_tokens, 0)
        self.assertEqual(job_usage[1].output_tokens, 0)
        self.assertEqual(job_usage[1].total_tokens, 0)

    async def test_hosted_dream_worker_runs_summarize_phase_with_fake_summary_agent(
        self,
    ) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD},
            project_ids={"project-a"},
        )

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        save_messages(
            tenant.db_path,
            [ModelRequest(parts=[UserPromptPart(content="first summarize span")])],
            session_id="default",
            timestamp=start.isoformat(),
        )
        save_messages(
            tenant.db_path,
            [ModelRequest(parts=[UserPromptPart(content="second summarize span")])],
            session_id="default",
            # A > 2h gap creates a compaction boundary so the summarize
            # phase's leaf pass has a span to summarize.
            timestamp=(start + timedelta(hours=3)).isoformat(),
        )

        class SummaryAgent:
            async def run(self, prompt: str) -> object:
                return SimpleNamespace(
                    output="a fake summary",
                    usage=lambda: SimpleNamespace(
                        requests=1,
                        input_tokens=6,
                        output_tokens=4,
                        total_tokens=10,
                    ),
                )

        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=DreamPhasePorts(
                model_group="fake",
                summary_agent_factory=lambda *_args, **_kwargs: SummaryAgent(),
            ),
        )
        jobs = HostedBackgroundJobRunner(service)

        result = await jobs.run_dream_phase(
            api_key.raw_key,
            RunDreamPhaseRequest(
                scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                phase=DreamPhase.SUMMARIZE,
                redaction=RedactionContext(forbidden_values=()),
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual([event.status for event in jobs.job_events], ["running", "ok"])
        self.assertEqual([event.phase for event in jobs.job_events], ["summarize", "summarize"])
        job_usage = [
            event
            for event in self.catalog.usage_events("tenant-a")
            if event.kind == "job"
        ]
        self.assertEqual(job_usage[-1].status, "ok")
        self.assertEqual(job_usage[-1].model_requests, 1)
        self.assertEqual(job_usage[-1].total_tokens, 10)

    async def test_hosted_summarize_partial_session_failure_reports_partial_status(
        self,
    ) -> None:
        """A session that fails to summarize must surface as a 'partial' job,
        not an 'ok' one -- swallowed per-session errors are how a SUMMARIZE
        run silently under-reports usage."""
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD},
            project_ids={"project-a"},
        )

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        save_messages(
            tenant.db_path,
            [ModelRequest(parts=[UserPromptPart(content="healthy summarize span")])],
            session_id="good-session",
            timestamp=start.isoformat(),
        )
        save_messages(
            tenant.db_path,
            [ModelRequest(parts=[UserPromptPart(content="poison marker span")])],
            session_id="bad-session",
            timestamp=start.isoformat(),
        )

        class SummaryAgent:
            async def run(self, prompt: str) -> object:
                if "poison marker" in prompt:
                    raise RuntimeError("boom")
                return SimpleNamespace(
                    output="a fake summary",
                    usage=lambda: SimpleNamespace(
                        requests=1,
                        input_tokens=6,
                        output_tokens=4,
                        total_tokens=10,
                    ),
                )

        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=DreamPhasePorts(
                model_group="fake",
                summary_agent_factory=lambda *_args, **_kwargs: SummaryAgent(),
            ),
        )
        jobs = HostedBackgroundJobRunner(service)

        result = await jobs.run_dream_phase(
            api_key.raw_key,
            RunDreamPhaseRequest(
                scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                phase=DreamPhase.SUMMARIZE,
                redaction=RedactionContext(forbidden_values=()),
            ),
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(
            [event.status for event in jobs.job_events], ["running", "partial"]
        )
        job_usage = [
            event
            for event in self.catalog.usage_events("tenant-a")
            if event.kind == "job"
        ]
        self.assertEqual(job_usage[-1].status, "partial")
        # Usage from the session that did summarize is still recorded.
        self.assertEqual(job_usage[-1].total_tokens, 10)

    async def test_hosted_dream_worker_redaction_failure_does_not_call_model(
        self,
    ) -> None:
        tenant = self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD, MemoryCapability.WRITE},
            project_ids={"project-a"},
        )
        agent_calls = 0

        class ExtractionAgent:
            async def run(self, transcript: str) -> object:
                nonlocal agent_calls
                agent_calls += 1
                return SimpleNamespace(
                    output=[
                        FactCandidate(
                            fact_text="Ryan prefers redacted reports.",
                            subject="Ryan",
                            category="preference",
                            importance=7,
                            confidence=0.9,
                            source_message_ids=[1],
                        )
                    ],
                    usage=lambda: SimpleNamespace(
                        requests=1,
                        input_tokens=1,
                        output_tokens=1,
                        total_tokens=2,
                    ),
                )

        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=DreamPhasePorts(
                model_group="fake",
                embed=lambda texts: [_unit_vector() for _ in texts],
                extraction_agent_factory=lambda *_args, **_kwargs: ExtractionAgent(),
            ),
        )
        jobs = HostedBackgroundJobRunner(service)

        await service.append_transcript(
            api_key.raw_key,
            AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="cedar-secret")])
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            ),
        )

        with self.assertRaises(ValueError):
            await jobs.run_dream_phase(
                api_key.raw_key,
                RunDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                    phase=DreamPhase.LIGHT,
                    redaction=RedactionContext(forbidden_values=("cedar-secret",)),
                ),
            )

        with closing(sqlite3.connect(tenant.db_path)) as conn:
            candidate_count = conn.execute(
                "SELECT COUNT(*) FROM memory_candidates"
            ).fetchone()[0]

        self.assertEqual(agent_calls, 0)
        self.assertEqual(candidate_count, 0)
        self.assertEqual([event.status for event in jobs.job_events], ["running", "error"])
        ledger_text = (
            repr(jobs.job_events)
            + repr(self.catalog.audit_events("tenant-a"))
            + repr(self.catalog.usage_events("tenant-a"))
        )
        self.assertNotIn("cedar-secret", ledger_text)

    async def test_dream_job_fails_closed_without_host_port(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD},
            project_ids={"project-a"},
        )

        with self.assertRaises(HostPortNotConfigured):
            await self.jobs.run_dream_phase(
                api_key.raw_key,
                RunDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                    phase=DreamPhase.LIGHT,
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

        self.assertEqual([event.status for event in self.jobs.job_events], ["running", "error"])
        self.assertEqual(self.jobs.job_events[-1].error_type, "HostPortNotConfigured")
        audit_events = self.catalog.audit_events("tenant-a")
        usage_events = self.catalog.usage_events("tenant-a")
        self.assertEqual(audit_events[-1].error_type, "HostPortNotConfigured")
        self.assertEqual(usage_events[-2].error_type, "HostPortNotConfigured")
        self.assertEqual(usage_events[-1].kind, "job")
        self.assertEqual(usage_events[-1].error_type, "HostPortNotConfigured")

    async def test_job_telemetry_failure_does_not_mask_dream_job_error(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD},
            project_ids={"project-a"},
        )
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=_FailingJobTelemetry(self.catalog),
        )
        runner = HostedBackgroundJobRunner(service)

        with self.assertRaises(HostPortNotConfigured):
            await runner.run_dream_phase(
                api_key.raw_key,
                RunDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                    phase=DreamPhase.LIGHT,
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

        self.assertEqual([event.status for event in runner.job_events], ["running", "error"])
        self.assertEqual(runner.job_events[-1].error_type, "HostPortNotConfigured")
        ledger_text = (
            repr(runner.job_events)
            + repr(self.catalog.audit_events("tenant-a"))
            + repr(self.catalog.usage_events("tenant-a"))
        )
        self.assertNotIn(api_key.raw_key, ledger_text)
        self.assertNotIn("Dream phase host port is not configured", ledger_text)

    async def test_job_usage_telemetry_failure_does_not_mask_dream_job_error(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD},
            project_ids={"project-a"},
        )
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=_FailingJobUsageTelemetry(self.catalog),
        )
        runner = HostedBackgroundJobRunner(service)

        with self.assertRaises(HostPortNotConfigured):
            await runner.run_dream_phase(
                api_key.raw_key,
                RunDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                    phase=DreamPhase.LIGHT,
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

        self.assertEqual([event.status for event in runner.job_events], ["running", "error"])
        self.assertEqual(runner.job_events[-1].error_type, "HostPortNotConfigured")

    async def test_dream_job_failure_lifecycle_persists_across_catalog_reload(
        self,
    ) -> None:
        root = Path(self.temp_dir.name)
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD},
            project_ids={"project-a"},
        )

        with self.assertRaises(HostPortNotConfigured):
            await self.jobs.run_dream_phase(
                api_key.raw_key,
                RunDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                    phase=DreamPhase.LIGHT,
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

        reloaded_catalog = HostedTenantCatalog(root)
        job_events = reloaded_catalog.job_events("tenant-a")
        usage_events = reloaded_catalog.usage_events("tenant-a")

        self.assertEqual([event.status for event in job_events], ["running", "error"])
        self.assertEqual({event.job_id for event in job_events}, {job_events[0].job_id})
        for event in job_events:
            with self.subTest(status=event.status):
                self.assertEqual(event.operation, "run_dream_phase")
                self.assertEqual(event.tenant_id, "tenant-a")
                self.assertEqual(event.principal_id, "agent-a")
                self.assertEqual(event.phase, DreamPhase.LIGHT.value)
                self.assertTrue(event.recorded_at.endswith("Z"))
        self.assertIsNone(job_events[0].error_type)
        self.assertEqual(job_events[1].error_type, "HostPortNotConfigured")
        self.assertEqual(usage_events[-1].kind, "job")
        self.assertEqual(usage_events[-1].status, "error")
        self.assertEqual(usage_events[-1].error_type, "HostPortNotConfigured")

        ledger_bytes = (root / "control-plane.db").read_bytes()
        ledger_text = repr(job_events) + repr(usage_events)
        self.assertNotIn(api_key.raw_key, ledger_text)
        self.assertNotIn(api_key.raw_key.encode("utf-8"), ledger_bytes)
        self.assertNotIn(b"Dream phase host port is not configured", ledger_bytes)


class HostedProvisioningRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_provision_tenant_converges_when_rival_commits_first(self) -> None:
        """Two provisioners racing on the same tenant_id must converge onto the
        winner's committed row instead of failing with IntegrityError."""
        control_db = self.root / "control-plane.db"
        real_allocate = self.catalog._allocate_db_filename

        def rival_commits_then_allocate(conn: sqlite3.Connection) -> str:
            # Simulate a concurrent provisioner winning the race between this
            # call's existence check and its INSERT.
            with closing(sqlite3.connect(control_db)) as rival:
                rival.execute(
                    """
                    INSERT INTO tenants (tenant_id, db_filename, active)
                    VALUES (?, ?, 0)
                    """,
                    ("tenant-race", "customer-rival.db"),
                )
                rival.commit()
            return real_allocate(conn)

        with patch.object(
            self.catalog,
            "_allocate_db_filename",
            side_effect=rival_commits_then_allocate,
        ):
            tenant = self.catalog.provision_tenant("tenant-race")

        self.assertEqual(tenant.tenant_id, "tenant-race")
        # The loser adopted the winner's row: one tenants row, the rival's
        # db_filename, activated and initialized.
        with closing(sqlite3.connect(control_db)) as conn:
            rows = conn.execute(
                "SELECT db_filename, active FROM tenants WHERE tenant_id = ?",
                ("tenant-race",),
            ).fetchall()
        self.assertEqual(rows, [("customer-rival.db", 1)])
        self.assertTrue((self.root / "customer-rival.db").exists())


class _FaultInjectingConnection:
    """Proxy that fails any statement containing one of the given fragments."""

    def __init__(self, conn: sqlite3.Connection, fail_fragments: tuple[str, ...]) -> None:
        self._conn = conn
        self._fail_fragments = fail_fragments

    def execute(self, sql: str, *args: object) -> object:
        for fragment in self._fail_fragments:
            if fragment in sql:
                raise sqlite3.OperationalError(f"injected fault: {fragment}")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


class HostedControlPlaneKeyCompensationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.keys = HostedApiKeyStore(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _fault_injected_keys(self, fail_fragments: tuple[str, ...]) -> None:
        real_connect = type(self.keys)._connect_control

        def connect_with_faults(store: HostedApiKeyStore) -> _FaultInjectingConnection:
            return _FaultInjectingConnection(real_connect(store), fail_fragments)

        patcher = patch.object(
            type(self.keys), "_connect_control", connect_with_faults
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_metadata_failure_revokes_the_minted_key(self) -> None:
        """Compensation: a failed metadata write must not leave a live key."""
        self._fault_injected_keys(("INSERT INTO hosted_api_key_metadata",))

        with self.assertRaises(sqlite3.OperationalError):
            self.keys.create_control_plane_key(
                tenant_id="tenant-a", project_id="proj-a", name="console key"
            )

        with closing(sqlite3.connect(self.root / "control-plane.db")) as conn:
            rows = conn.execute(
                "SELECT revoked_at, revoked_by FROM hosted_api_keys"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][0])
        self.assertEqual(rows[0][1], "control-plane-metadata-failure")

    def test_failed_compensation_names_the_orphaned_key(self) -> None:
        """If the compensating revoke also fails, the error must name the
        orphaned live key for manual revocation and keep the original
        metadata failure as the cause -- never a bare revoke error that
        hides the fail-open credential."""
        self._fault_injected_keys(
            (
                "INSERT INTO hosted_api_key_metadata",
                "UPDATE hosted_api_keys",
            )
        )

        with self.assertRaises(RuntimeError) as caught:
            self.keys.create_control_plane_key(
                tenant_id="tenant-a", project_id="proj-a", name="console key"
            )

        message = str(caught.exception)
        with closing(sqlite3.connect(self.root / "control-plane.db")) as conn:
            (key_id,) = conn.execute("SELECT key_id FROM hosted_api_keys").fetchone()
        self.assertIn(key_id, message)
        self.assertIn("revoke", message)
        self.assertIn("manually", message)
        self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
        self.assertIn("hosted_api_key_metadata", str(caught.exception.__cause__))


class HostedUsageEventCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.catalog = HostedTenantCatalog(Path(self.temp_dir.name))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _event(operation: str) -> HostedUsageEvent:
        return HostedUsageEvent(
            kind="request",
            operation=operation,
            tenant_id="tenant-a",
            principal_id="agent-a",
            status="ok",
            recorded_at="2026-07-16T00:00:00Z",
        )

    def test_usage_events_after_durable_id_cutoff(self) -> None:
        """A durable MAX(id) cutoff isolates the events recorded after it,
        independent of list positions that concurrent writers or pruning
        could shift."""
        self.catalog.record_usage_event(self._event("before-1"))
        self.catalog.record_usage_event(self._event("before-2"))

        cutoff = self.catalog.last_usage_event_id("tenant-a")
        self.catalog.record_usage_event(self._event("after-1"))
        self.catalog.record_usage_event(self._event("after-2"))

        events = self.catalog.usage_events("tenant-a", after_id=cutoff)
        self.assertEqual([event.operation for event in events], ["after-1", "after-2"])

    def test_last_usage_event_id_is_zero_for_untracked_tenant(self) -> None:
        self.assertEqual(self.catalog.last_usage_event_id("tenant-a"), 0)
