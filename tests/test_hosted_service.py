from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    AppendTranscriptRequest,
    DreamPhase,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    RunDreamPhaseRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import (
    HostedBackgroundJobRunner,
    HostedInMemoryRateLimiter,
    HostedMemoryService,
    HostedRateLimitRule,
    HostedRateLimitExceeded,
)
from vexic_hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.ports import HostPortNotConfigured
from vexic.storage import single_message_adapter


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

    def test_hosted_api_exposes_contract_operation_names(self) -> None:
        for method_name in (
            "append_transcript",
            "ingest_source_transcript",
            "search_transcript",
            "expand_history",
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

    async def test_telemetry_is_stored_per_tenant_database(self) -> None:
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
