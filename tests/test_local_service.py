import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    ExpandHistoryRequest,
    MemoryCapability,
    MemoryCategory,
    MemoryScope,
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
    commit_deep_cycle,
    commit_dream_cycle,
    save_messages,
    search_messages,
    single_message_adapter,
)
from vexic.storage.promotion import PromotionDecision


def _unit_vector(first: float) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = first
    return vector


def _scope(*, session_id: str = "default") -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-a",
        session_id=session_id,
        principal=Principal(
            principal_id="test-operator",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities={MemoryCapability.SEARCH},
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
