import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from vexic.contract import (
    AppendTranscriptRequest,
    ExpandHistoryRequest,
    DeleteScopeRequest,
    DreamPhase,
    ExportScopeRequest,
    IngestSourceTranscriptRequest,
    RecordRetrievalEventRequest,
    RebuildRequest,
    ReplayScopeRequest,
    RetireFactRequest,
    RetrievalEvent,
    RunDreamPhaseRequest,
    SourceTranscriptMessage,
    MemoryCapability,
    MemoryCategory,
    MemoryScope,
    MemoryScopeSelector,
    Principal,
    PrincipalType,
    RedactionContext,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.embeddings import EMBEDDING_DIM
from vexic.models import FactCandidate
from vexic.ports import HostPortNotConfigured
from vexic.storage import (
    SourceTranscriptInput,
    commit_deep_cycle,
    commit_dream_cycle,
    ingest_source_messages,
    init_db,
    save_messages,
    search_messages,
    single_message_adapter,
)
from vexic.storage.promotion import PromotionDecision


def _unit_vector(first: float) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = first
    return vector


def _scope(
    *,
    session_id: str = "default",
    agent_id: str | None = None,
    capabilities: set[MemoryCapability] | None = None,
) -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-a",
        session_id=session_id,
        agent_id=agent_id,
        principal=Principal(
            principal_id="test-operator",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities=capabilities or {MemoryCapability.SEARCH},
    )


class LocalMemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_search_transcript_uses_scope_session(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="default cedar detail")])],
            session_id="default",
        )
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="telegram cedar detail")])],
            session_id="telegram:42",
        )

        result = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(session_id="telegram:42"), query="cedar")
        )

        self.assertEqual(len(result.hits), 1)
        self.assertIn("telegram cedar detail", result.hits[0].body)

    async def test_transcript_operations_use_exact_agent_scope(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        async def append(agent_id: str | None, text: str) -> int:
            result = await service.append_transcript(
                AppendTranscriptRequest(
                    scope=_scope(
                        session_id="session-a",
                        agent_id=agent_id,
                        capabilities={MemoryCapability.WRITE},
                    ),
                    messages_json=[
                        single_message_adapter.dump_json(
                            ModelRequest(parts=[UserPromptPart(content=text)])
                        ).decode()
                    ],
                    redaction=RedactionContext(forbidden_values=()),
                )
            )
            return result.message_ids[0]

        agent_a_id = await append("agent-a", "cedar agent a detail")
        await append("agent-b", "cedar agent b detail")
        await append(None, "cedar shared detail")

        agent_a = await service.search_transcript(
            SearchTranscriptRequest(
                scope=_scope(session_id="session-a", agent_id="agent-a"),
                query="cedar",
            )
        )
        shared = await service.search_transcript(
            SearchTranscriptRequest(
                scope=_scope(session_id="session-a", agent_id=None),
                query="cedar",
            )
        )
        expanded = await service.expand_history(
            ExpandHistoryRequest(
                scope=_scope(
                    session_id="session-a",
                    agent_id="agent-a",
                    capabilities={MemoryCapability.EXPAND_HISTORY},
                ),
                first_message_id=1,
                last_message_id=3,
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual([hit.message_id for hit in agent_a.hits], [agent_a_id])
        self.assertIn("agent a", agent_a.hits[0].body)
        self.assertEqual(len(shared.hits), 1)
        self.assertIn("shared", shared.hits[0].body)
        self.assertIn("agent a", expanded.text)
        self.assertNotIn("agent b", expanded.text)
        self.assertNotIn("shared", expanded.text)

    async def test_search_transcript_honors_limit_above_storage_default(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        save_messages(
            self.db_path,
            [
                ModelRequest(parts=[UserPromptPart(content=f"cedar detail {index}")])
                for index in range(7)
            ],
            session_id="default",
        )

        result = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="cedar", limit=7)
        )

        self.assertEqual(len(result.hits), 7)

    async def test_search_messages_rejects_non_positive_limit(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        with self.assertRaisesRegex(ValueError, "limit must be at least 1"):
            search_messages(self.db_path, "cedar", limit=0)

    async def test_search_long_term_without_embedder_fails_closed(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        with self.assertRaisesRegex(HostPortNotConfigured, "Embeddings"):
            await service.search_long_term(
                SearchLongTermRequest(scope=_scope(), query="compact reports")
            )

    async def test_search_long_term_returns_contract_facts(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(
            db_path=self.db_path,
            tenant_id="tenant-a",
            embed=lambda texts: [_unit_vector(1.0) for _ in texts],
        )
        service.init_schema()
        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text="Ryan prefers compact reports.",
                    subject="Ryan",
                    category="preference",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        commit_deep_cycle(
            self.db_path,
            [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
            started_at="2026-06-01T00:01:00+00:00",
            finished_at="2026-06-01T00:01:01+00:00",
        )

        result = await service.search_long_term(
            SearchLongTermRequest(scope=_scope(), query="compact reports")
        )

        self.assertEqual(len(result.facts), 1)
        self.assertEqual(result.facts[0].category, MemoryCategory.PREFERENCE)
        self.assertEqual(result.facts[0].fact_text, "Ryan prefers compact reports.")

    async def test_search_long_term_rejects_wrong_embedder_result_count(self) -> None:
        from vexic.service import LocalMemoryService

        cases = [
            [],
            [_unit_vector(1.0), _unit_vector(1.0)],
        ]

        for embeddings in cases:
            with self.subTest(count=len(embeddings)):
                service = LocalMemoryService(
                    db_path=self.db_path,
                    tenant_id="tenant-a",
                    embed=lambda texts, embeddings=embeddings: embeddings,
                )
                service.init_schema()

                with self.assertRaisesRegex(ValueError, "exactly one embedding"):
                    await service.search_long_term(
                        SearchLongTermRequest(scope=_scope(), query="compact reports")
                    )

    async def test_v0_1_deferred_protocol_operations_raise_not_implemented_only(
        self,
    ) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        deferred_scope = _scope().model_copy(update={"tenant_id": "other-tenant"})
        redaction = RedactionContext(forbidden_values=())
        requests = (
            (
                "record_retrieval_event",
                RecordRetrievalEventRequest(
                    scope=deferred_scope,
                    event=RetrievalEvent(
                        event_id=0,
                        referent_id=1,
                        session_id="default",
                        query="compact reports",
                        retrieved_at="2026-06-20T00:00:00Z",
                        used=True,
                    ),
                    redaction=redaction,
                ),
            ),
            ("retire_fact", RetireFactRequest(scope=deferred_scope, fact_id=1)),
            (
                "run_dream_phase",
                RunDreamPhaseRequest(
                    scope=deferred_scope,
                    phase=DreamPhase.LIGHT,
                    redaction=redaction,
                ),
            ),
            (
                "export_scope",
                ExportScopeRequest(scope=deferred_scope, redaction=redaction),
            ),
            (
                "replay_scope",
                ReplayScopeRequest(scope=deferred_scope, redaction=redaction),
            ),
            ("rebuild", RebuildRequest(scope=deferred_scope, redaction=redaction)),
            (
                "delete_scope",
                DeleteScopeRequest(
                    scope=deferred_scope,
                    target_scope=MemoryScopeSelector(tenant_id="other-tenant"),
                    reason="test deletion",
                    redaction=redaction,
                ),
            ),
        )

        for method_name, request in requests:
            with self.subTest(method_name=method_name):
                with self.assertRaises(NotImplementedError):
                    await getattr(service, method_name)(request)

    async def test_tombstone_specific_scope_blocks_broader_request_scope(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="cedar preference")])],
            session_id="default",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO scope_tombstones
                        (target_tenant_id, target_project_id, target_session_id,
                         created_by_principal_id, created_by_principal_type, reason)
                    VALUES ('tenant-a', 'project-a', 'default', 'operator', 'operator',
                            'test deletion')
                    """
                )

        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.search_transcript(
                SearchTranscriptRequest(
                    scope=_scope().model_copy(update={"project_id": None}),
                    query="cedar",
                )
            )

    async def test_tenant_scope_must_match_opened_sqlite_context(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        with self.assertRaises(PermissionError):
            await service.search_transcript(
                SearchTranscriptRequest(
                    scope=_scope().model_copy(update={"tenant_id": "tenant-b"}),
                    query="cedar",
                )
            )

    async def test_init_schema_preserves_coalescent_extension_table(self) -> None:
        from vexic.service import LocalMemoryService

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("CREATE TABLE background_tool_audit (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO background_tool_audit (id) VALUES (7)")
            conn.commit()

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT id FROM background_tool_audit").fetchone()
        self.assertEqual(row, (7,))

    async def test_write_operations_require_redaction_context(self) -> None:
        from vexic.contract import AppendTranscriptRequest
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        request = AppendTranscriptRequest(
            scope=_scope().model_copy(
                update={"capabilities": {MemoryCapability.WRITE}}
            ),
            messages_json=[
                single_message_adapter.dump_json(
                    ModelRequest(parts=[UserPromptPart(content="stored through service")])
                ).decode()
            ],
            redaction=RedactionContext(forbidden_values=()),
        )

        result = await service.append_transcript(request)

        self.assertEqual(result.message_ids, [1])

    async def test_ingest_source_transcript_records_ledgered_message(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        result = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    SourceTranscriptMessage(
                        source_host="Claude-Code",
                        source_session_id="session-1",
                        source_message_id="uuid-1",
                        message_json=single_message_adapter.dump_json(
                            ModelRequest(parts=[UserPromptPart(content="ledger cedar")])
                        ).decode(),
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual(result.items[0].status, "inserted")
        self.assertEqual(result.items[0].message_id, 1)
        self.assertEqual(result.items[0].source_host, "claude-code")
        self.assertEqual(len(search_messages(self.db_path, "cedar")), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            message_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(messages)")
            }
            ledger_row = conn.execute(
                """
                SELECT source_host, source_session_id, source_message_id, message_id
                FROM source_transcript_ledger
                """
            ).fetchone()

        self.assertNotIn("source_host", message_columns)
        self.assertEqual(ledger_row, ("claude-code", "session-1", "uuid-1", 1))

    async def test_ingest_source_transcript_accepts_assistant_text(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        result = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-1",
                        source_message_id="uuid-1",
                        message_json=single_message_adapter.dump_json(
                            ModelResponse(parts=[TextPart(content="assistant cedar")])
                        ).decode(),
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual(result.items[0].status, "inserted")
        self.assertIn("assistant cedar", search_messages(self.db_path, "cedar")[0].body)

    async def test_ingest_source_transcript_skips_duplicate_source_key(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="duplicate cedar")])
        ).decode()
        first_request = IngestSourceTranscriptRequest(
            scope=_scope().model_copy(update={"capabilities": {MemoryCapability.WRITE}}),
            messages=[
                SourceTranscriptMessage(
                    source_host="Claude-Code",
                    source_session_id="session-1",
                    source_message_id="uuid-1",
                    message_json=message_json,
                )
            ],
            redaction=RedactionContext(forbidden_values=()),
        )
        second_request = first_request.model_copy(
            update={
                "messages": [
                    first_request.messages[0].model_copy(
                        update={"source_host": "claude-code"}
                    )
                ]
            }
        )

        first = await service.ingest_source_transcript(first_request)
        second = await service.ingest_source_transcript(second_request)

        self.assertEqual(first.items[0].status, "inserted")
        self.assertEqual(second.items[0].status, "skipped")
        self.assertEqual(second.items[0].message_id, first.items[0].message_id)
        with closing(sqlite3.connect(self.db_path)) as conn:
            message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            ledger_count = conn.execute(
                "SELECT COUNT(*) FROM source_transcript_ledger"
            ).fetchone()[0]

        self.assertEqual(message_count, 1)
        self.assertEqual(fts_count, 1)
        self.assertEqual(ledger_count, 1)

    async def test_ingest_source_transcript_partial_retry_inserts_only_missing_rows(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        def item(source_message_id: str, content: str) -> SourceTranscriptMessage:
            return SourceTranscriptMessage(
                source_host="claude-code",
                source_session_id="session-1",
                source_message_id=source_message_id,
                message_json=single_message_adapter.dump_json(
                    ModelRequest(parts=[UserPromptPart(content=content)])
                ).decode(),
            )

        await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[item("uuid-1", "first cedar")],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        retry = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    item("uuid-1", "first cedar"),
                    item("uuid-2", "second cedar"),
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual([row.status for row in retry.items], ["skipped", "inserted"])
        self.assertEqual(retry.items[1].message_id, 2)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_transcript_ledger").fetchone()[0],
                2,
            )

    async def test_ingest_source_transcript_keeps_identical_text_from_distinct_source_ids(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="same cedar")])
        ).decode()

        result = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-1",
                        source_message_id="uuid-1",
                        message_json=message_json,
                    ),
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-1",
                        source_message_id="uuid-2",
                        message_json=message_json,
                    ),
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual([row.status for row in result.items], ["inserted", "inserted"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0], 2)

    async def test_ingest_source_transcript_warns_changed_content_for_existing_key(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        def request(content: str) -> IngestSourceTranscriptRequest:
            return IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-1",
                        source_message_id="uuid-1",
                        message_json=single_message_adapter.dump_json(
                            ModelRequest(parts=[UserPromptPart(content=content)])
                        ).decode(),
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            )

        await service.ingest_source_transcript(request("original cedar"))
        changed = await service.ingest_source_transcript(request("changed cedar"))

        self.assertEqual(changed.items[0].status, "skipped")
        self.assertEqual(changed.items[0].message_id, 1)
        self.assertIn("different content", changed.items[0].warning or "")
        hits = search_messages(self.db_path, "cedar")
        self.assertEqual(len(hits), 1)
        self.assertIn("original cedar", hits[0].body)

    async def test_ingest_source_transcript_skips_unreadable_existing_duplicate(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        def source_message(source_message_id: str, content: str) -> SourceTranscriptMessage:
            return SourceTranscriptMessage(
                source_host="claude-code",
                source_session_id="session-1",
                source_message_id=source_message_id,
                message_json=single_message_adapter.dump_json(
                    ModelRequest(parts=[UserPromptPart(content=content)])
                ).decode(),
            )

        await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[source_message("uuid-1", "original cedar")],
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE messages SET message_json = '{' WHERE id = 1")
            conn.commit()

        retry = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    source_message("uuid-1", "original cedar"),
                    source_message("uuid-2", "fresh cedar"),
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual([row.status for row in retry.items], ["skipped", "inserted"])
        self.assertEqual(retry.items[0].message_id, 1)
        self.assertEqual(
            retry.items[0].warning,
            "source key already ingested; existing content unreadable",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    async def test_ingest_source_transcript_rejects_polluted_rows_per_row(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        def source_message(source_message_id: str, msg: object) -> SourceTranscriptMessage:
            return SourceTranscriptMessage(
                source_host="claude-code",
                source_session_id="session-1",
                source_message_id=source_message_id,
                message_json=single_message_adapter.dump_json(msg).decode(),
            )

        result = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    source_message(
                        "system",
                        ModelRequest(parts=[SystemPromptPart(content="hidden")]),
                    ),
                    source_message(
                        "instructions",
                        ModelRequest(
                            parts=[UserPromptPart(content="visible")],
                            instructions="developer hint",
                        ),
                    ),
                    source_message(
                        "tool-call",
                        ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={})]),
                    ),
                    source_message(
                        "tool-return",
                        ModelRequest(
                            parts=[
                                ToolReturnPart(
                                    tool_name="lookup",
                                    content="result",
                                    tool_call_id="call-1",
                                )
                            ]
                        ),
                    ),
                    source_message(
                        "usage",
                        ModelResponse(
                            parts=[TextPart(content="assistant text")],
                            usage=RequestUsage(input_tokens=1, output_tokens=1),
                        ),
                    ),
                    source_message(
                        "secret",
                        ModelRequest(parts=[UserPromptPart(content="secret-token")]),
                    ),
                    source_message(
                        "clean",
                        ModelRequest(parts=[UserPromptPart(content="clean cedar")]),
                    ),
                ],
                redaction=RedactionContext(forbidden_values=("secret-token",)),
            )
        )

        self.assertEqual(
            [row.status for row in result.items],
            [
                "rejected",
                "rejected",
                "rejected",
                "rejected",
                "rejected",
                "rejected",
                "inserted",
            ],
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_transcript_ledger").fetchone()[0],
                1,
            )

    async def test_ingest_source_transcript_rejects_forbidden_source_keys(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        result = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-secret",
                        source_message_id="uuid-1",
                        message_json=single_message_adapter.dump_json(
                            ModelRequest(parts=[UserPromptPart(content="clean cedar")])
                        ).decode(),
                    )
                ],
                redaction=RedactionContext(forbidden_values=("secret",)),
            )
        )

        self.assertEqual(result.items[0].status, "rejected")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_transcript_ledger").fetchone()[0],
                0,
            )

    def test_ingest_source_messages_rejects_blank_source_identifiers(self) -> None:
        init_db(self.db_path)

        result = ingest_source_messages(
            self.db_path,
            [
                SourceTranscriptInput(
                    source_host="claude-code",
                    source_session_id="   ",
                    source_message_id="uuid-1",
                    message_json=single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="clean cedar")])
                    ).decode(),
                )
            ],
        )

        self.assertEqual(result[0].status, "rejected")
        self.assertEqual(result[0].reason, "source identifiers must not be blank")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_transcript_ledger").fetchone()[0],
                0,
            )

    async def test_ingest_source_transcript_rejects_invalid_json_per_row(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        result = await service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.WRITE}}
                ),
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-1",
                        source_message_id="bad-json",
                        message_json="{",
                    ),
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id="session-1",
                        source_message_id="clean",
                        message_json=single_message_adapter.dump_json(
                            ModelRequest(parts=[UserPromptPart(content="clean cedar")])
                        ).decode(),
                    ),
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual([row.status for row in result.items], ["rejected", "inserted"])
        self.assertEqual(result.items[0].reason, "invalid message_json")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_transcript_ledger").fetchone()[0],
                1,
            )

    async def test_expand_history_truncates_oversized_ranges(self) -> None:
        from vexic.service import EXPAND_HISTORY_MAX_ROWS, LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        last_message_id = EXPAND_HISTORY_MAX_ROWS + 1
        save_messages(
            self.db_path,
            [
                ModelRequest(parts=[UserPromptPart(content=f"history row {index}")])
                for index in range(last_message_id)
            ],
            session_id="default",
        )

        result = await service.expand_history(
            ExpandHistoryRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.EXPAND_HISTORY}}
                ),
                first_message_id=1,
                last_message_id=last_message_id,
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual(result.text, "")
        self.assertTrue(result.truncated)

    async def test_expand_history_uses_request_redaction_before_egress(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="cedar-secret detail")])],
            session_id="default",
        )

        with self.assertRaisesRegex(ValueError, "forbidden secret"):
            await service.expand_history(
                ExpandHistoryRequest(
                    scope=_scope().model_copy(
                        update={"capabilities": {MemoryCapability.EXPAND_HISTORY}}
                    ),
                    first_message_id=1,
                    last_message_id=1,
                    redaction=RedactionContext(forbidden_values=("cedar-secret",)),
                )
            )


if __name__ == "__main__":
    unittest.main()
