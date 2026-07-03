import inspect
import unittest
from typing import get_type_hints

from pydantic import ValidationError

from vexic.contract import (
    CONTRACT_VERSION,
    AppendTranscriptRequest,
    CandidateNote,
    ContractVersion,
    DeleteScopeRequest,
    DreamPhase,
    EgressKind,
    ExpandHistoryRequest,
    IngestSourceTranscriptRequest,
    LifecycleAction,
    LongTermFact,
    MemoryCapability,
    MemoryCategory,
    MemoryScope,
    MemoryScopeSelector,
    MemoryService,
    Principal,
    PrincipalType,
    PurgeScopeRequest,
    RedactionContext,
    RedactionRequiredRequest,
    RetireFactRequest,
    SearchTranscriptRequest,
    SearchLongTermRequest,
    SourceTranscriptMessage,
    TombstoneRecord,
    TrustBoundary,
    require_capability,
)


class MemoryContractScopeTests(unittest.TestCase):
    def _scope(
        self,
        *,
        capabilities: set[MemoryCapability] | None = None,
    ) -> MemoryScope:
        return MemoryScope(
            tenant_id="tenant-a",
            principal=Principal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
            ),
            trust_boundary=TrustBoundary.LOCAL_TRUSTED,
            capabilities=capabilities or set(),
        )

    def test_scope_requires_nonblank_tenant_id(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryScope(
                tenant_id=" ",
                principal=Principal(
                    principal_id="operator-1",
                    principal_type=PrincipalType.OPERATOR,
                ),
                trust_boundary=TrustBoundary.LOCAL_TRUSTED,
                capabilities=set(),
            )

    def test_optional_scope_fields_are_nullable_but_nonblank_when_present(self) -> None:
        scope = self._scope().model_copy(
            update={
                "project_id": None,
                "user_id": None,
                "session_id": None,
                "agent_id": None,
            }
        )
        payload = scope.model_dump(mode="json")
        self.assertIsNone(payload["project_id"])
        self.assertIsNone(payload["user_id"])
        self.assertIsNone(payload["session_id"])
        self.assertIsNone(payload["agent_id"])

        with self.assertRaises(ValidationError):
            MemoryScope(
                tenant_id="tenant-a",
                project_id="",
                principal=Principal(
                    principal_id="operator-1",
                    principal_type=PrincipalType.OPERATOR,
                ),
                trust_boundary=TrustBoundary.LOCAL_TRUSTED,
                capabilities=set(),
            )

        with self.assertRaises(ValidationError):
            MemoryScope(
                tenant_id="tenant-a",
                agent_id=" ",
                principal=Principal(
                    principal_id="operator-1",
                    principal_type=PrincipalType.OPERATOR,
                ),
                trust_boundary=TrustBoundary.LOCAL_TRUSTED,
                capabilities=set(),
            )

    def test_agent_scope_round_trips_without_using_principal_identity(self) -> None:
        scope = self._scope(capabilities={MemoryCapability.SEARCH}).model_copy(
            update={
                "session_id": "session-a",
                "agent_id": "agent-memory-a",
                "principal": Principal(
                    principal_id="runtime-agent-1",
                    principal_type=PrincipalType.AGENT,
                ),
            }
        )
        request = SearchTranscriptRequest(scope=scope, query="cedar")

        round_tripped = SearchTranscriptRequest.model_validate_json(
            request.model_dump_json()
        )

        self.assertEqual(round_tripped.scope.agent_id, "agent-memory-a")
        self.assertEqual(round_tripped.scope.principal.principal_id, "runtime-agent-1")
        self.assertNotEqual(
            round_tripped.scope.agent_id,
            round_tripped.scope.principal.principal_id,
        )

    def test_scope_selector_includes_agent_identifier_only(self) -> None:
        selector = MemoryScopeSelector(
            tenant_id="tenant-a",
            session_id="session-a",
            agent_id="agent-memory-a",
        )

        round_tripped = MemoryScopeSelector.model_validate_json(
            selector.model_dump_json()
        )

        self.assertEqual(round_tripped.agent_id, "agent-memory-a")
        self.assertNotIn("principal", round_tripped.model_dump(mode="json"))

        with self.assertRaises(ValidationError):
            MemoryScopeSelector(tenant_id="tenant-a", agent_id="")

    def test_contract_models_round_trip_as_json_safe_payloads(self) -> None:
        request = SearchLongTermRequest(
            scope=self._scope(capabilities={MemoryCapability.SEARCH}),
            query="Ryan preferences",
            limit=5,
        )

        round_tripped = SearchLongTermRequest.model_validate_json(
            request.model_dump_json()
        )

        self.assertEqual(round_tripped.contract_version, CONTRACT_VERSION)
        self.assertEqual(round_tripped.scope.tenant_id, "tenant-a")
        self.assertEqual(round_tripped.query, "Ryan preferences")

    def test_delete_scope_target_is_identifier_selector_not_actor_scope(self) -> None:
        actor_scope = self._scope(
            capabilities={MemoryCapability.ADMIN_LIFECYCLE},
        ).model_copy(update={"correlation_id": "delete-request-1"})
        target_scope = MemoryScopeSelector(
            tenant_id="tenant-a",
            project_id="project-a",
            session_id="session-a",
        )

        request = DeleteScopeRequest(
            scope=actor_scope,
            target_scope=target_scope,
            reason="tenant-requested deletion",
            redaction=RedactionContext(forbidden_values=()),
        )

        self.assertIs(request.scope.principal, actor_scope.principal)
        self.assertEqual(
            request.scope.capabilities,
            {MemoryCapability.ADMIN_LIFECYCLE},
        )
        self.assertEqual(request.target_scope.session_id, "session-a")
        self.assertNotIn("principal", request.target_scope.model_dump(mode="json"))
        self.assertNotIn("capabilities", request.target_scope.model_dump(mode="json"))

    def test_scope_selector_rejects_actor_auth_and_audit_metadata(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryScopeSelector.model_validate(
                {
                    "tenant_id": "tenant-a",
                    "session_id": "session-a",
                    "principal": {
                        "principal_id": "operator-1",
                        "principal_type": "operator",
                    },
                    "trust_boundary": "local_trusted",
                    "capabilities": ["memory:admin:lifecycle"],
                    "correlation_id": "delete-request-1",
                }
            )

        with self.assertRaises(ValidationError):
            DeleteScopeRequest(
                scope=self._scope(capabilities={MemoryCapability.ADMIN_LIFECYCLE}),
                target_scope={
                    "tenant_id": "tenant-a",
                    "session_id": "session-a",
                    "principal": {
                        "principal_id": "operator-1",
                        "principal_type": "operator",
                    },
                    "trust_boundary": "local_trusted",
                    "capabilities": ["memory:admin:lifecycle"],
                    "correlation_id": "delete-request-1",
                },
                reason="tenant-requested deletion",
                redaction=RedactionContext(forbidden_values=()),
            )

    def test_delete_scope_target_tenant_must_match_actor_scope_tenant(self) -> None:
        with self.assertRaises(ValidationError):
            DeleteScopeRequest(
                scope=self._scope(capabilities={MemoryCapability.ADMIN_LIFECYCLE}),
                target_scope=MemoryScopeSelector(
                    tenant_id="tenant-b",
                    session_id="session-a",
                ),
                reason="tenant-requested deletion",
                redaction=RedactionContext(forbidden_values=()),
            )

    def test_scope_selector_optional_fields_are_nullable_but_nonblank_when_present(
        self,
    ) -> None:
        selector = MemoryScopeSelector(
            tenant_id="tenant-a",
            project_id=None,
            user_id=None,
            session_id=None,
        )
        payload = selector.model_dump(mode="json")
        self.assertIsNone(payload["project_id"])
        self.assertIsNone(payload["user_id"])
        self.assertIsNone(payload["session_id"])

        with self.assertRaises(ValidationError):
            MemoryScopeSelector(
                tenant_id="tenant-a",
                session_id=" ",
            )


class MemoryContractCapabilityTests(unittest.TestCase):
    def _scope(self, capabilities: set[MemoryCapability]) -> MemoryScope:
        return MemoryScope(
            tenant_id="tenant-a",
            principal=Principal(
                principal_id="service-1",
                principal_type=PrincipalType.SERVICE,
            ),
            trust_boundary=TrustBoundary.NETWORKED,
            capabilities=capabilities,
        )

    def test_privileged_egress_requires_matching_capability(self) -> None:
        request = ExpandHistoryRequest(
            scope=self._scope({MemoryCapability.READ}).model_copy(
                update={"session_id": "session-a"}
            ),
            first_message_id=1,
            last_message_id=3,
            redaction=RedactionContext(forbidden_values=("secret",)),
        )

        with self.assertRaises(PermissionError):
            require_capability(request.scope, request.required_capability)

        permitted = request.model_copy(
            update={
                "scope": self._scope(
                    {MemoryCapability.READ, MemoryCapability.EXPAND_HISTORY}
                ).model_copy(update={"session_id": "session-a"})
            }
        )
        require_capability(permitted.scope, permitted.required_capability)

    def test_transcript_search_requires_search_capability(self) -> None:
        request = SearchTranscriptRequest(
            scope=self._scope({MemoryCapability.READ}).model_copy(
                update={"session_id": "session-a"}
            ),
            query="Ryan preferences",
        )

        with self.assertRaises(PermissionError):
            require_capability(request.scope, request.required_capability)

        permitted = request.model_copy(
            update={
                "scope": self._scope({MemoryCapability.SEARCH}).model_copy(
                    update={"session_id": "session-a"}
                )
            }
        )
        require_capability(permitted.scope, permitted.required_capability)

    def test_write_and_egress_requests_require_redaction_context(self) -> None:
        scope = self._scope({MemoryCapability.WRITE}).model_copy(
            update={"session_id": "session-a"}
        )
        with self.assertRaises(ValidationError):
            AppendTranscriptRequest(
                scope=scope,
                messages_json=["{}"],
            )

        with self.assertRaises(ValidationError):
            IngestSourceTranscriptRequest(
                scope=scope,
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-a",
                        source_message_id="uuid-1",
                        message_json="{}",
                    )
                ],
            )

        request = AppendTranscriptRequest(
            scope=scope,
            messages_json=["{}"],
            redaction=RedactionContext(forbidden_values=()),
        )

        self.assertIsInstance(request, RedactionRequiredRequest)
        self.assertIsInstance(
            IngestSourceTranscriptRequest(
                scope=scope,
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-a",
                        source_message_id="uuid-1",
                        message_json="{}",
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            ),
            RedactionRequiredRequest,
        )

    def test_transcript_and_expand_operations_require_session_scope(self) -> None:
        scope = self._scope({MemoryCapability.WRITE})

        with self.assertRaises(ValidationError):
            AppendTranscriptRequest(
                scope=scope,
                messages_json=["{}"],
                redaction=RedactionContext(forbidden_values=()),
            )

        with self.assertRaises(ValidationError):
            IngestSourceTranscriptRequest(
                scope=scope,
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-a",
                        source_message_id="uuid-1",
                        message_json="{}",
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            )

        with self.assertRaises(ValidationError):
            ExpandHistoryRequest(
                scope=scope,
                first_message_id=1,
                last_message_id=3,
                redaction=RedactionContext(forbidden_values=()),
            )


class MemoryContractModelTests(unittest.TestCase):
    def test_fact_and_candidate_models_preserve_glass_box_metadata(self) -> None:
        fact = LongTermFact(
            fact_id=10,
            fact_text="Ryan prefers terse implementation notes.",
            subject="Ryan",
            category=MemoryCategory.PREFERENCE,
            importance=7,
            confidence=0.86,
            source_message_ids=[1, 2],
            editable=True,
            created_at="2026-06-18T18:00:00Z",
            retrieved_count=3,
            used_count=2,
        )
        note = CandidateNote(
            candidate_id=11,
            fact_text="Ryan is evaluating memory contracts.",
            category=MemoryCategory.CONTEXT,
            source_message_ids=[3],
            created_at="2026-06-18T18:01:00Z",
        )

        self.assertEqual(fact.category, MemoryCategory.PREFERENCE)
        self.assertEqual(fact.source_message_ids, [1, 2])
        self.assertTrue(fact.editable)
        self.assertEqual(note.category, MemoryCategory.CONTEXT)
        self.assertEqual(note.source_message_ids, [3])

    def test_lifecycle_contract_distinguishes_retire_tombstone_and_deferred_purge(self) -> None:
        scope = MemoryScope(
            tenant_id="tenant-a",
            principal=Principal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
            ),
            trust_boundary=TrustBoundary.LOCAL_TRUSTED,
            capabilities={MemoryCapability.ADMIN_LIFECYCLE},
        )
        retire = RetireFactRequest(
            scope=scope,
            fact_id=8,
            superseded_by_fact_id=9,
        )
        delete = DeleteScopeRequest(
            scope=scope,
            target_scope=MemoryScopeSelector(
                tenant_id="tenant-a",
                session_id="session-a",
            ),
            reason="tenant-requested lifecycle test",
            redaction=RedactionContext(forbidden_values=()),
        )
        tombstone = TombstoneRecord(
            tombstone_id="tombstone-1",
            target_scope=delete.target_scope,
            created_by=scope.principal,
            reason=delete.reason,
            retrieval_blocked=True,
            export_blocked=True,
            replay_blocked=True,
            rebuild_blocked=True,
            physical_purge_deferred=True,
        )

        self.assertIsNone(retire.redaction)
        self.assertTrue(tombstone.physical_purge_deferred)
        self.assertTrue(tombstone.rebuild_blocked)
        round_tripped = TombstoneRecord.model_validate_json(
            tombstone.model_dump_json()
        )
        self.assertIsInstance(round_tripped.target_scope, MemoryScopeSelector)
        self.assertEqual(round_tripped.target_scope.session_id, "session-a")

    def test_dream_phase_contract_uses_existing_light_rem_deep_names(self) -> None:
        self.assertEqual(
            {phase.value for phase in DreamPhase},
            {"light", "rem", "deep"},
        )

    def _lifecycle_scope(self) -> MemoryScope:
        return MemoryScope(
            tenant_id="tenant-a",
            session_id="session-a",
            principal=Principal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
            ),
            trust_boundary=TrustBoundary.LOCAL_TRUSTED,
            capabilities={MemoryCapability.ADMIN_LIFECYCLE},
        )

    def test_purge_scope_request_mirrors_delete_scope_lifecycle_shape(self) -> None:
        request = PurgeScopeRequest(
            scope=self._lifecycle_scope(),
            target_scope=MemoryScopeSelector(
                tenant_id="tenant-a",
                session_id="session-a",
            ),
            reason="tenant-requested erasure",
            redaction=RedactionContext(forbidden_values=()),
        )

        self.assertIs(
            request.required_capability, MemoryCapability.ADMIN_LIFECYCLE
        )
        self.assertFalse(request.dry_run)
        self.assertEqual(LifecycleAction.PURGE.value, "purge")

    def test_purge_scope_target_tenant_must_match_actor_scope_tenant(self) -> None:
        with self.assertRaises(ValidationError):
            PurgeScopeRequest(
                scope=self._lifecycle_scope(),
                target_scope=MemoryScopeSelector(
                    tenant_id="tenant-b",
                    session_id="session-a",
                ),
                reason="tenant-requested erasure",
                redaction=RedactionContext(forbidden_values=()),
            )


class MemoryContractProtocolTests(unittest.TestCase):
    def test_protocol_methods_accept_scope_and_return_typed_results(self) -> None:
        for method_name in (
            "append_transcript",
            "ingest_source_transcript",
            "search_transcript",
            "expand_history",
            "search_long_term",
            "run_dream_phase",
            "export_scope",
            "delete_scope",
            "purge_scope",
        ):
            method = getattr(MemoryService, method_name)
            signature = inspect.signature(method)
            parameters = list(signature.parameters.values())
            self.assertEqual(parameters[1].name, "request")
            hints = get_type_hints(method)
            self.assertIn("return", hints)

    def test_operation_versions_are_explicit_literals(self) -> None:
        self.assertEqual(CONTRACT_VERSION, "0.1.0")
        self.assertEqual(ContractVersion.V0_1.value, CONTRACT_VERSION)
        self.assertEqual(EgressKind.EXPAND_HISTORY.value, "expand_history")


if __name__ == "__main__":
    unittest.main()
