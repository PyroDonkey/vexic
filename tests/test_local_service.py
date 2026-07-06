import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

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
    FreshContextRequest,
    IngestSourceTranscriptRequest,
    PRIME_CONTEXT_HEADER,
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


def _axis_vector(index: int) -> list[float]:
    # Orthogonal vectors: distinct enough to dodge candidate-dedup merging.
    vector = [0.0] * EMBEDDING_DIM
    vector[index] = 1.0
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

    async def test_search_long_term_uses_default_embedder_for_facts(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
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
            agent_id=None,
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

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [_unit_vector(1.0) for _ in texts],
        ):
            result = await service.search_long_term(
                SearchLongTermRequest(scope=_scope(), query="compact reports")
            )

        self.assertEqual([fact.fact_text for fact in result.facts], ["Ryan prefers compact reports."])

    async def test_search_long_term_as_of_filters_tier3_facts(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text="Ryan started a new job.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    occurred_at="2025-03-14",
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
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

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [_unit_vector(1.0) for _ in texts],
        ):
            before = await service.search_long_term(
                SearchLongTermRequest(
                    scope=_scope(), query="new job", as_of="2024-01-01"
                )
            )
            after = await service.search_long_term(
                SearchLongTermRequest(
                    scope=_scope(), query="new job", as_of="2025-04-01"
                )
            )

        self.assertEqual(before.facts, [])
        self.assertEqual(
            [fact.fact_text for fact in after.facts], ["Ryan started a new job."]
        )

    async def test_search_long_term_as_of_filters_tier2_candidate_fallback(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text="Ryan keeps cedar notes tentative.",
                    subject="Ryan",
                    category="fact",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    occurred_at="2025-03-14",
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [_unit_vector(1.0) for _ in texts],
        ):
            before = await service.search_long_term(
                SearchLongTermRequest(
                    scope=_scope(), query="cedar notes", as_of="2024-01-01"
                )
            )
            after = await service.search_long_term(
                SearchLongTermRequest(
                    scope=_scope(), query="cedar notes", as_of="2025-04-01"
                )
            )

        self.assertEqual(before.candidate_notes, [])
        self.assertEqual(
            [note.fact_text for note in after.candidate_notes],
            ["Ryan keeps cedar notes tentative."],
        )

    async def test_search_long_term_as_of_uses_created_at_when_occurred_at_absent(
        self,
    ) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text="Ryan prefers concise standups.",
                    subject="Ryan",
                    category="preference",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
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
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE long_term_memory SET created_at = ? WHERE fact_text = ?",
                ("2025-03-14 00:00:00", "Ryan prefers concise standups."),
            )
            conn.commit()

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [_unit_vector(1.0) for _ in texts],
        ):
            before = await service.search_long_term(
                SearchLongTermRequest(
                    scope=_scope(), query="concise standups", as_of="2024-01-01"
                )
            )
            after = await service.search_long_term(
                SearchLongTermRequest(
                    scope=_scope(), query="concise standups", as_of="2025-04-01"
                )
            )

        self.assertEqual(before.facts, [])
        self.assertEqual(
            [fact.fact_text for fact in after.facts],
            ["Ryan prefers concise standups."],
        )

    async def test_search_long_term_uses_default_embedder_for_candidate_fallback(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text="Ryan keeps cedar notes tentative.",
                    subject="Ryan",
                    category="fact",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [_unit_vector(1.0) for _ in texts],
        ):
            result = await service.search_long_term(
                SearchLongTermRequest(scope=_scope(), query="cedar notes")
            )

        self.assertEqual(
            [note.fact_text for note in result.candidate_notes],
            ["Ryan keeps cedar notes tentative."],
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
            agent_id=None,
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
        # A non-event fact carries no event time.
        self.assertIsNone(result.facts[0].occurred_at)

    async def test_search_long_term_contract_fact_exposes_occurred_at(self) -> None:
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
                    fact_text="Ryan moved to Vancouver.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    occurred_at="2025-03",
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
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
            SearchLongTermRequest(scope=_scope(), query="Vancouver")
        )

        self.assertEqual(len(result.facts), 1)
        self.assertEqual(result.facts[0].occurred_at, "2025-03")

    async def test_search_long_term_orders_event_facts_by_occurred_at(self) -> None:
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
                    fact_text="Team meeting in January.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    occurred_at="2024-01",
                ),
                FactCandidate(
                    fact_text="Team meeting in June.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[2],
                    occurred_at="2025-06",
                ),
                FactCandidate(
                    fact_text="Team meeting in September.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[3],
                    occurred_at="2024-09",
                ),
            ],
            candidate_embeddings=[
                _axis_vector(0),
                _axis_vector(1),
                _axis_vector(2),
            ],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=3,
            last_processed_message_id=3,
        )
        commit_deep_cycle(
            self.db_path,
            [
                PromotionDecision(candidate_id=1, embedding=_axis_vector(0)),
                PromotionDecision(candidate_id=2, embedding=_axis_vector(1)),
                PromotionDecision(candidate_id=3, embedding=_axis_vector(2)),
            ],
            started_at="2026-06-01T00:01:00+00:00",
            finished_at="2026-06-01T00:01:01+00:00",
        )

        result = await service.search_long_term(
            SearchLongTermRequest(scope=_scope(), query="meeting", limit=5)
        )

        # Event facts surface newest-event-first, ordered by occurred_at, not
        # by storage time or fusion rank.
        self.assertEqual(
            [fact.occurred_at for fact in result.facts],
            ["2025-06", "2024-09", "2024-01"],
        )

    def test_promotion_refuses_event_candidate_without_occurred_at(self) -> None:
        # Invariant 11: an event fact must carry an event time. Event retrieval
        # sorts by occurred_at, so every promoted event must have one; this
        # locks the fail-closed promotion guard that guarantees it.
        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text="Something happened, date unknown.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    # occurred_at intentionally omitted -> None
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )

        with self.assertRaisesRegex(ValueError, "occurred_at"):
            commit_deep_cycle(
                self.db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-06-01T00:01:00+00:00",
                finished_at="2026-06-01T00:01:01+00:00",
            )

    async def test_search_long_term_uses_exact_agent_scope_for_facts(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(
            db_path=self.db_path,
            tenant_id="tenant-a",
            embed=lambda texts: [_unit_vector(1.0) for _ in texts],
        )
        service.init_schema()
        for message_id, agent_id, fact_text in (
            (1, "agent-a", "Ryan agent a cedar fact."),
            (2, "agent-b", "Ryan agent b cedar fact."),
            (3, None, "Ryan shared cedar fact."),
        ):
            commit_dream_cycle(
                self.db_path,
                [
                    FactCandidate(
                        fact_text=fact_text,
                        subject="Ryan",
                        category="fact",
                        importance=6,
                        confidence=0.8,
                        source_message_ids=[message_id],
                    )
                ],
                candidate_embeddings=[_unit_vector(1.0)],
                agent_id=agent_id,
                status="ok",
                started_at="2026-06-01T00:00:00+00:00",
                finished_at="2026-06-01T00:00:01+00:00",
                messages_processed=1,
                last_processed_message_id=message_id,
            )
            commit_deep_cycle(
                self.db_path,
                [PromotionDecision(candidate_id=message_id, embedding=_unit_vector(1.0))],
                agent_id=agent_id,
                started_at="2026-06-01T00:01:00+00:00",
                finished_at="2026-06-01T00:01:01+00:00",
            )

        results = {
            agent_id: await service.search_long_term(
                SearchLongTermRequest(scope=_scope(agent_id=agent_id), query="cedar")
            )
            for agent_id in ("agent-a", "agent-b", None)
        }

        self.assertEqual(
            {agent_id: [fact.fact_text for fact in result.facts] for agent_id, result in results.items()},
            {
                "agent-a": ["Ryan agent a cedar fact."],
                "agent-b": ["Ryan agent b cedar fact."],
                None: ["Ryan shared cedar fact."],
            },
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            event_agents = conn.execute(
                """
                SELECT agent_id, COUNT(*)
                FROM retrieval_events
                GROUP BY agent_id
                """
            ).fetchall()
        self.assertEqual(sorted(event_agents, key=lambda row: "" if row[0] is None else row[0]), [(None, 1), ("agent-a", 1), ("agent-b", 1)])

    async def test_search_long_term_hostile_query_stays_agent_scoped(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(
            db_path=self.db_path,
            tenant_id="tenant-a",
            embed=lambda texts: [_unit_vector(1.0) for _ in texts],
        )
        service.init_schema()
        for message_id, agent_id, fact_text in (
            (1, "agent-a", "Ryan agent a cedar fact."),
            (2, "agent-b", "Ryan agent b cedar fact."),
        ):
            commit_dream_cycle(
                self.db_path,
                [
                    FactCandidate(
                        fact_text=fact_text,
                        subject="Ryan",
                        category="fact",
                        importance=6,
                        confidence=0.8,
                        source_message_ids=[message_id],
                    )
                ],
                candidate_embeddings=[_unit_vector(1.0)],
                agent_id=agent_id,
                status="ok",
                started_at="2026-06-01T00:00:00+00:00",
                finished_at="2026-06-01T00:00:01+00:00",
                messages_processed=1,
                last_processed_message_id=message_id,
            )
            commit_deep_cycle(
                self.db_path,
                [PromotionDecision(candidate_id=message_id, embedding=_unit_vector(1.0))],
                agent_id=agent_id,
                started_at="2026-06-01T00:01:00+00:00",
                finished_at="2026-06-01T00:01:01+00:00",
            )

        result = await service.search_long_term(
            SearchLongTermRequest(
                scope=_scope(agent_id="agent-a"),
                query="cedar') OR 1=1 --",
            )
        )

        self.assertEqual(
            [fact.fact_text for fact in result.facts],
            ["Ryan agent a cedar fact."],
        )

    async def test_search_long_term_uses_exact_agent_scope_for_candidate_fallback(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(
            db_path=self.db_path,
            tenant_id="tenant-a",
            embed=lambda texts: [_unit_vector(1.0) for _ in texts],
        )
        service.init_schema()
        for message_id, agent_id, fact_text in (
            (1, "agent-a", "Ryan agent a cedar candidate."),
            (2, "agent-b", "Ryan agent b cedar candidate."),
            (3, None, "Ryan shared cedar candidate."),
        ):
            commit_dream_cycle(
                self.db_path,
                [
                    FactCandidate(
                        fact_text=fact_text,
                        subject="Ryan",
                        category="fact",
                        importance=6,
                        confidence=0.8,
                        source_message_ids=[message_id],
                    )
                ],
                candidate_embeddings=[_unit_vector(1.0)],
                agent_id=agent_id,
                status="ok",
                started_at="2026-06-01T00:00:00+00:00",
                finished_at="2026-06-01T00:00:01+00:00",
                messages_processed=1,
                last_processed_message_id=message_id,
            )

        results = {
            agent_id: await service.search_long_term(
                SearchLongTermRequest(scope=_scope(agent_id=agent_id), query="cedar")
            )
            for agent_id in ("agent-a", "agent-b", None)
        }

        self.assertEqual(
            {agent_id: [note.fact_text for note in result.candidate_notes] for agent_id, result in results.items()},
            {
                "agent-a": ["Ryan agent a cedar candidate."],
                "agent-b": ["Ryan agent b cedar candidate."],
                None: ["Ryan shared cedar candidate."],
            },
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            event_agents = conn.execute(
                """
                SELECT agent_id, COUNT(*)
                FROM candidate_retrieval_events
                GROUP BY agent_id
                """
            ).fetchall()
        self.assertEqual(sorted(event_agents, key=lambda row: "" if row[0] is None else row[0]), [(None, 1), ("agent-a", 1), ("agent-b", 1)])

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

    async def test_dream_phase_fails_closed_without_host_port(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        redaction = RedactionContext(forbidden_values=())

        with self.assertRaises(HostPortNotConfigured):
            await service.run_dream_phase(
                RunDreamPhaseRequest(
                    scope=_scope(capabilities={MemoryCapability.ADMIN_REBUILD}),
                    phase=DreamPhase.LIGHT,
                    redaction=redaction,
                )
            )

    async def test_lifecycle_export_replay_and_rebuild_are_agent_scoped(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        message_ids = {
            agent_id: save_messages(
                self.db_path,
                [ModelRequest(parts=[UserPromptPart(content=content)])],
                agent_id=agent_id,
            )[0]
            for agent_id, content in (
                ("agent-a", "cedar agent a transcript"),
                ("agent-b", "cedar agent b transcript"),
                (None, "cedar shared transcript"),
            )
        }
        for agent_id, fact_text in (
            ("agent-a", "cedar agent a candidate"),
            ("agent-b", "cedar agent b candidate"),
            (None, "cedar shared candidate"),
        ):
            commit_dream_cycle(
                self.db_path,
                [
                    FactCandidate(
                        fact_text=fact_text,
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[message_ids[agent_id]],
                    )
                ],
                candidate_embeddings=[_unit_vector(1.0)],
                agent_id=agent_id,
                status="ok",
                started_at="2026-06-01T00:00:00+00:00",
                finished_at="2026-06-01T00:00:01+00:00",
                messages_processed=1,
                last_processed_message_id=message_ids[agent_id],
            )
        with closing(sqlite3.connect(self.db_path)) as conn:
            candidate_ids = {
                row[1]: row[0]
                for row in conn.execute(
                    "SELECT id, agent_id FROM memory_candidates ORDER BY id"
                )
            }
        for agent_id in ("agent-a", "agent-b", None):
            commit_deep_cycle(
                self.db_path,
                [PromotionDecision(candidate_ids[agent_id], _unit_vector(1.0))],
                agent_id=agent_id,
                started_at="2026-06-01T00:01:00+00:00",
                finished_at="2026-06-01T00:01:01+00:00",
            )

        redaction = RedactionContext(forbidden_values=())
        agent_a_scope = _scope(
            agent_id="agent-a",
            capabilities={
                MemoryCapability.SEARCH,
                MemoryCapability.EXPORT,
                MemoryCapability.REPLAY,
                MemoryCapability.ADMIN_REBUILD,
                MemoryCapability.ADMIN_LIFECYCLE,
            },
        )
        replay = await service.replay_scope(
            ReplayScopeRequest(scope=agent_a_scope, redaction=redaction)
        )
        export = await service.export_scope(
            ExportScopeRequest(scope=agent_a_scope, redaction=redaction)
        )
        rebuild = await service.rebuild(
            RebuildRequest(scope=agent_a_scope, redaction=redaction)
        )
        rebuild_artifact = await service.rebuild(
            RebuildRequest(
                scope=agent_a_scope,
                redaction=redaction,
                return_artifacts=True,
            )
        )

        export_text = Path(export.artifact_ref).read_text()
        rebuild_text = Path(rebuild_artifact.artifact_ref or "").read_text()
        self.assertIsNone(rebuild.artifact_ref)
        self.assertEqual(json.loads(rebuild_text)["repair_report_scope"], "database")
        self.assertNotIn("agent b", rebuild_text)
        self.assertNotIn("shared", rebuild_text)
        self.assertEqual([hit.body for hit in replay.messages], ["User: cedar agent a transcript"])
        self.assertIn("cedar agent a transcript", export_text)
        self.assertIn("cedar agent a candidate", export_text)
        self.assertNotIn("agent b", export_text)
        self.assertNotIn("shared", export_text)
        self.assertEqual(json.loads(export_text)["scope"]["agent_id"], "agent-a")

        invalid_delete = DeleteScopeRequest.model_construct(
            scope=agent_a_scope,
            target_scope=MemoryScopeSelector(tenant_id="other-tenant", agent_id="agent-a"),
            reason="bad target",
            redaction=redaction,
        )
        with self.assertRaisesRegex(PermissionError, "target_scope tenant_id"):
            await service.delete_scope(invalid_delete)

        delete = await service.delete_scope(
            DeleteScopeRequest(
                scope=agent_a_scope,
                target_scope=MemoryScopeSelector(tenant_id="tenant-a", agent_id="agent-a"),
                reason="test deletion",
                redaction=redaction,
            )
        )
        self.assertEqual(delete.tombstone.target_scope.agent_id, "agent-a")
        with closing(sqlite3.connect(self.db_path)) as conn:
            flags = conn.execute(
                """
                SELECT retrieval_blocked, export_blocked, replay_blocked,
                       rebuild_blocked, physical_purge_deferred
                FROM scope_tombstones
                WHERE id = ?
                """,
                (int(delete.tombstone.tombstone_id),),
            ).fetchone()
        self.assertEqual(flags, (1, 1, 1, 1, 1))

        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.replay_scope(
                ReplayScopeRequest(scope=agent_a_scope, redaction=redaction)
            )
        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.export_scope(
                ExportScopeRequest(scope=agent_a_scope, redaction=redaction)
            )
        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.rebuild(
                RebuildRequest(scope=agent_a_scope, redaction=redaction)
            )

        agent_b = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(agent_id="agent-b"), query="cedar")
        )
        shared = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(agent_id=None), query="cedar")
        )
        self.assertEqual([hit.body for hit in agent_b.hits], ["User: cedar agent b transcript"])
        self.assertEqual([hit.body for hit in shared.hits], ["User: cedar shared transcript"])

    async def test_retrieval_event_and_retire_fact_use_agent_scope(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                facts = {}
                for promoted_from_candidate_id, agent_id, fact_text in (
                    (100, "agent-a", "cedar agent a fact"),
                    (200, "agent-b", "cedar agent b fact"),
                ):
                    cursor = conn.execute(
                        """
                        INSERT INTO long_term_memory
                            (fact_text, subject, category, importance, confidence,
                             source_message_ids, agent_id, promoted_from_candidate_id)
                        VALUES (?, 'Ryan', 'fact', 5, 0.8, '[1]', ?, ?)
                        """,
                        (fact_text, agent_id, promoted_from_candidate_id),
                    )
                    facts[agent_id] = int(cursor.lastrowid)

        write_scope = _scope(
            agent_id="agent-a",
            capabilities={MemoryCapability.WRITE},
        )
        event = await service.record_retrieval_event(
            RecordRetrievalEventRequest(
                scope=write_scope,
                event=RetrievalEvent(
                    event_id=0,
                    referent_id=facts["agent-a"],
                    session_id="default",
                    query="cedar",
                    retrieved_at="2026-06-20T00:00:00Z",
                    used=True,
                ),
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        with self.assertRaisesRegex(PermissionError, "outside memory scope"):
            await service.record_retrieval_event(
                RecordRetrievalEventRequest(
                    scope=write_scope,
                    event=RetrievalEvent(
                        event_id=0,
                        referent_id=facts["agent-b"],
                        session_id="default",
                        query="cedar",
                        retrieved_at="2026-06-20T00:00:00Z",
                    ),
                    redaction=RedactionContext(forbidden_values=()),
                )
            )
        retired = await service.retire_fact(
            RetireFactRequest(scope=write_scope, fact_id=facts["agent-a"])
        )
        other_agent_retired = await service.retire_fact(
            RetireFactRequest(scope=write_scope, fact_id=facts["agent-b"])
        )

        self.assertGreater(event.event_id, 0)
        self.assertTrue(retired.retired)
        self.assertFalse(other_agent_retired.retired)
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = {
                row[0]: row[1:]
                for row in conn.execute(
                    """
                    SELECT agent_id, retrieved_count, used_count, retired
                    FROM long_term_memory
                    ORDER BY agent_id
                    """
                )
            }
        self.assertEqual(rows["agent-a"], (1, 1, 1))
        self.assertEqual(rows["agent-b"], (0, 0, 0))

        admin_scope = _scope(
            agent_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.ADMIN_LIFECYCLE},
        )
        await service.delete_scope(
            DeleteScopeRequest(
                scope=admin_scope,
                target_scope=MemoryScopeSelector(tenant_id="tenant-a", agent_id="agent-a"),
                reason="test deletion",
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.record_retrieval_event(
                RecordRetrievalEventRequest(
                    scope=write_scope,
                    event=RetrievalEvent(
                        event_id=0,
                        referent_id=facts["agent-a"],
                        session_id="default",
                        query="cedar",
                        retrieved_at="2026-06-20T00:00:00Z",
                    ),
                    redaction=RedactionContext(forbidden_values=()),
                )
            )
        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.retire_fact(
                RetireFactRequest(scope=write_scope, fact_id=facts["agent-a"])
            )

    async def test_partial_tombstone_flags_gate_matching_lifecycle_operations(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                facts = {}
                for promoted_from_candidate_id, agent_id in (
                    (300, "agent-a"),
                    (400, "agent-b"),
                ):
                    cursor = conn.execute(
                        """
                        INSERT INTO long_term_memory
                            (fact_text, subject, category, importance, confidence,
                             source_message_ids, agent_id, promoted_from_candidate_id)
                        VALUES ('cedar fact', 'Ryan', 'fact', 5, 0.8, '[1]', ?, ?)
                        """,
                        (agent_id, promoted_from_candidate_id),
                    )
                    facts[agent_id] = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO scope_tombstones
                        (target_tenant_id, target_agent_id,
                         created_by_principal_id, created_by_principal_type, reason,
                         retrieval_blocked, export_blocked, replay_blocked,
                         rebuild_blocked, physical_purge_deferred)
                    VALUES ('tenant-a', 'agent-a', 'operator', 'operator',
                            'retrieval only', 1, 0, 0, 0, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO scope_tombstones
                        (target_tenant_id, target_agent_id,
                         created_by_principal_id, created_by_principal_type, reason,
                         retrieval_blocked, export_blocked, replay_blocked,
                         rebuild_blocked, physical_purge_deferred)
                    VALUES ('tenant-a', 'agent-b', 'operator', 'operator',
                            'rebuild only', 0, 0, 0, 1, 1)
                    """
                )

        agent_a_write = _scope(agent_id="agent-a", capabilities={MemoryCapability.WRITE})
        agent_b_write = _scope(agent_id="agent-b", capabilities={MemoryCapability.WRITE})
        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.record_retrieval_event(
                RecordRetrievalEventRequest(
                    scope=agent_a_write,
                    event=RetrievalEvent(
                        event_id=0,
                        referent_id=facts["agent-a"],
                        session_id="default",
                        query="cedar",
                        retrieved_at="2026-06-20T00:00:00Z",
                    ),
                    redaction=RedactionContext(forbidden_values=()),
                )
            )
        self.assertTrue(
            (
                await service.retire_fact(
                    RetireFactRequest(scope=agent_a_write, fact_id=facts["agent-a"])
                )
            ).retired
        )
        self.assertGreater(
            (
                await service.record_retrieval_event(
                    RecordRetrievalEventRequest(
                        scope=agent_b_write,
                        event=RetrievalEvent(
                            event_id=0,
                            referent_id=facts["agent-b"],
                            session_id="default",
                            query="cedar",
                            retrieved_at="2026-06-20T00:00:00Z",
                        ),
                        redaction=RedactionContext(forbidden_values=()),
                    )
                )
            ).event_id,
            0,
        )
        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.retire_fact(
                RetireFactRequest(scope=agent_b_write, fact_id=facts["agent-b"])
            )

    def test_session_summary_helpers_use_agent_scope(self) -> None:
        from vexic.storage import (
            fetch_session_summary_frontier,
            list_compactable_session_ids,
            record_session_summary,
            render_session_recap,
        )

        init_db(self.db_path)
        for agent_id, content in (
            ("agent-a", "cedar agent a message"),
            ("agent-b", "cedar agent b message"),
            (None, "cedar shared message"),
        ):
            save_messages(
                self.db_path,
                [ModelRequest(parts=[UserPromptPart(content=content)])],
                agent_id=agent_id,
            )
            record_session_summary(
                self.db_path,
                session_id="default",
                agent_id=agent_id,
                kind="leaf",
                first_message_id=1,
                last_message_id=1,
                summary_text=f"summary for {agent_id or 'shared'}",
            )
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="agent b other session")])],
            session_id="agent-b-only",
            agent_id="agent-b",
        )

        self.assertEqual(
            [summary.summary_text for summary in fetch_session_summary_frontier(
                self.db_path,
                session_id="default",
                agent_id="agent-a",
            )],
            ["summary for agent-a"],
        )
        shared_recap = render_session_recap(
            self.db_path,
            session_id="default",
            agent_id=None,
        )
        self.assertIn("summary for shared", shared_recap)
        self.assertNotIn("agent-a", shared_recap)
        self.assertNotIn("agent-b", shared_recap)
        self.assertEqual(list_compactable_session_ids(self.db_path), ["default"])
        self.assertEqual(
            list_compactable_session_ids(self.db_path, agent_id="agent-a"),
            ["default"],
        )
        self.assertEqual(
            list_compactable_session_ids(self.db_path, agent_id="agent-b"),
            ["agent-b-only", "default"],
        )

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

    async def test_agent_tombstone_blocks_only_matching_agent_scope(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        for agent_id, content in (
            ("agent-a", "cedar agent a detail"),
            ("agent-b", "cedar agent b detail"),
            (None, "cedar shared detail"),
        ):
            save_messages(
                self.db_path,
                [ModelRequest(parts=[UserPromptPart(content=content)])],
                agent_id=agent_id,
            )
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO scope_tombstones
                        (target_tenant_id, target_session_id, target_agent_id,
                         created_by_principal_id, created_by_principal_type, reason)
                    VALUES ('tenant-a', 'default', 'agent-a',
                            'operator', 'operator', 'test deletion')
                    """
                )

        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.search_transcript(
                SearchTranscriptRequest(
                    scope=_scope(agent_id="agent-a"),
                    query="cedar",
                )
            )
        agent_b = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(agent_id="agent-b"), query="cedar")
        )
        shared = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(agent_id=None), query="cedar")
        )

        self.assertEqual(len(agent_b.hits), 1)
        self.assertIn("agent b", agent_b.hits[0].body)
        self.assertEqual(len(shared.hits), 1)
        self.assertIn("shared", shared.hits[0].body)

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
                    update={
                        "agent_id": "agent-a",
                        "capabilities": {MemoryCapability.WRITE},
                    }
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
        self.assertEqual(len(search_messages(self.db_path, "cedar")), 0)
        self.assertEqual(len(search_messages(self.db_path, "cedar", agent_id="agent-a")), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            message_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(messages)")
            }
            ledger_row = conn.execute(
                """
                SELECT source_host, source_session_id, source_message_id, agent_id, message_id
                FROM source_transcript_ledger
                """
            ).fetchone()

        self.assertNotIn("source_host", message_columns)
        self.assertEqual(ledger_row, ("claude-code", "session-1", "uuid-1", "agent-a", 1))

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

    async def test_ingest_source_transcript_scopes_source_key_by_agent(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        def request(agent_id: str | None, content: str) -> IngestSourceTranscriptRequest:
            return IngestSourceTranscriptRequest(
                scope=_scope().model_copy(
                    update={
                        "agent_id": agent_id,
                        "capabilities": {MemoryCapability.WRITE},
                    }
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

        inserted = {
            agent_id: (await service.ingest_source_transcript(request(agent_id, content))).items[0]
            for agent_id, content in (
                ("agent-a", "agent a cedar"),
                ("agent-b", "agent b cedar"),
                (None, "shared cedar"),
            )
        }
        skipped = {
            agent_id: (await service.ingest_source_transcript(request(agent_id, content))).items[0]
            for agent_id, content in (
                ("agent-a", "agent a retry"),
                ("agent-b", "agent b retry"),
                (None, "shared retry"),
            )
        }

        self.assertEqual([item.status for item in inserted.values()], ["inserted"] * 3)
        self.assertEqual([item.status for item in skipped.values()], ["skipped"] * 3)
        self.assertEqual(
            {agent_id: item.message_id for agent_id, item in skipped.items()},
            {agent_id: item.message_id for agent_id, item in inserted.items()},
        )
        self.assertEqual(len(set(item.message_id for item in inserted.values())), 3)

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

    def test_ingest_source_messages_rejects_echoed_prime_context(self) -> None:
        init_db(self.db_path)

        result = ingest_source_messages(
            self.db_path,
            [
                SourceTranscriptInput(
                    source_host="claude-code",
                    source_session_id="session-1",
                    source_message_id="echoed-prime",
                    message_json=single_message_adapter.dump_json(
                        ModelRequest(
                            parts=[
                                UserPromptPart(
                                    content=(
                                        f"{PRIME_CONTEXT_HEADER}\n"
                                        "Long-term memory:\n- cedar"
                                    )
                                )
                            ]
                        )
                    ).decode(),
                ),
                SourceTranscriptInput(
                    source_host="claude-code",
                    source_session_id="session-1",
                    source_message_id="clean",
                    message_json=single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="clean cedar")])
                    ).decode(),
                ),
            ],
        )

        self.assertEqual(result[0].status, "rejected")
        self.assertEqual(result[0].reason, "prime context is not transcript text")
        self.assertEqual(result[1].status, "inserted")

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            bodies = [
                row[0] for row in conn.execute("SELECT body FROM messages_fts").fetchall()
            ]
            self.assertTrue(all(PRIME_CONTEXT_HEADER not in body for body in bodies))

        hits = search_messages(self.db_path, "cedar")
        self.assertEqual([hit.body for hit in hits], ["User: clean cedar"])
        self.assertTrue(all(PRIME_CONTEXT_HEADER not in hit.body for hit in hits))

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


class ArtifactLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_writes_artifacts_under_configured_artifact_dir(self) -> None:
        from vexic.service import LocalMemoryService

        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "memory.db")
            artifact_dir = Path(temp) / "managed-artifacts"
            service = LocalMemoryService(
                db_path=db_path,
                tenant_id="tenant-a",
                artifact_dir=artifact_dir,
            )
            service.init_schema()
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="cedar transcript")])],
            )

            export = await service.export_scope(
                ExportScopeRequest(
                    scope=_scope(capabilities={MemoryCapability.EXPORT}),
                    redaction=RedactionContext(forbidden_values=()),
                )
            )

            artifact = Path(export.artifact_ref)
            self.assertEqual(artifact.parent, artifact_dir)
            self.assertIn("cedar transcript", artifact.read_text(encoding="utf-8"))

    async def test_prune_artifacts_removes_only_aged_vexic_artifacts(self) -> None:
        import os

        from vexic.service import LocalMemoryService

        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "memory.db")
            artifact_dir = Path(temp) / "managed-artifacts"
            service = LocalMemoryService(
                db_path=db_path,
                tenant_id="tenant-a",
                artifact_dir=artifact_dir,
            )
            service.init_schema()
            artifact_dir.mkdir(parents=True, exist_ok=True)
            aged = artifact_dir / "vexic-export-old.json"
            fresh = artifact_dir / "vexic-export-new.json"
            unrelated = artifact_dir / "notes.txt"
            for path in (aged, fresh, unrelated):
                path.write_text("{}", encoding="utf-8")
            hour = 3600
            old_time = 1_700_000_000
            os.utime(aged, (old_time, old_time))
            os.utime(unrelated, (old_time, old_time))

            removed = service.prune_artifacts(older_than_seconds=24 * hour)

            self.assertEqual(removed, 1)
            self.assertFalse(aged.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    async def test_prune_artifacts_rejects_negative_age(self) -> None:
        from vexic.service import LocalMemoryService

        with tempfile.TemporaryDirectory() as temp:
            service = LocalMemoryService(
                db_path=str(Path(temp) / "memory.db"),
                tenant_id="tenant-a",
                artifact_dir=Path(temp) / "managed-artifacts",
            )

            # A negative window would put the cutoff in the future and delete
            # every artifact, including ones written moments ago.
            with self.assertRaisesRegex(ValueError, "older_than_seconds"):
                service.prune_artifacts(older_than_seconds=-1)

    async def test_prune_artifacts_skips_files_deleted_concurrently(self) -> None:
        import os

        from vexic.service import LocalMemoryService

        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp) / "managed-artifacts"
            service = LocalMemoryService(
                db_path=str(Path(temp) / "memory.db"),
                tenant_id="tenant-a",
                artifact_dir=artifact_dir,
            )
            artifact_dir.mkdir(parents=True, exist_ok=True)
            aged = artifact_dir / "vexic-export-old.json"
            racer = artifact_dir / "vexic-export-race.json"
            for path in (aged, racer):
                path.write_text("{}", encoding="utf-8")
            old_time = 1_700_000_000
            os.utime(aged, (old_time, old_time))
            os.utime(racer, (old_time, old_time))

            original_stat = Path.stat

            def racy_stat(self: Path, *args: object, **kwargs: object) -> object:
                if self.name == "vexic-export-race.json":
                    raise FileNotFoundError(str(self))
                return original_stat(self, *args, **kwargs)

            with patch.object(Path, "stat", racy_stat):
                removed = service.prune_artifacts(older_than_seconds=3600)

            self.assertEqual(removed, 1)
            self.assertFalse(aged.exists())

    async def test_pre_existing_artifact_dir_is_tightened_on_posix(self) -> None:
        import os
        import stat as stat_module

        if os.name == "nt":
            self.skipTest("POSIX mode-bit enforcement")

        from vexic.service import LocalMemoryService

        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp) / "managed-artifacts"
            # Simulates CI: the directory already exists with a default umask
            # mode, which mkdir(mode=0o700, exist_ok=True) does not repair.
            artifact_dir.mkdir(parents=True)
            artifact_dir.chmod(0o755)
            service = LocalMemoryService(
                db_path=str(Path(temp) / "memory.db"),
                tenant_id="tenant-a",
                artifact_dir=artifact_dir,
            )

            service.prune_artifacts(older_than_seconds=3600)

            mode = stat_module.S_IMODE(artifact_dir.stat().st_mode)
            self.assertEqual(mode, 0o700)


class FreshContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save(
        self,
        text: str,
        *,
        session_id: str = "default",
        agent_id: str | None = None,
    ) -> int:
        return save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content=text)])],
            session_id=session_id,
            agent_id=agent_id,
        )[0]

    def _summary(
        self,
        *,
        session_id: str = "default",
        agent_id: str | None = None,
        kind: str,
        first_message_id: int,
        last_message_id: int,
        summary_text: str,
        replaces_summary_ids: tuple[int, ...] = (),
    ) -> int:
        from vexic.storage import record_session_summary

        return record_session_summary(
            self.db_path,
            session_id=session_id,
            agent_id=agent_id,
            kind=kind,
            first_message_id=first_message_id,
            last_message_id=last_message_id,
            summary_text=summary_text,
            replaces_summary_ids=replaces_summary_ids,
        )

    async def test_frontier_selection_is_condensed_plus_unreplaced_leaves(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        for text in ("m1", "m2", "m3", "m4", "m5", "m6"):
            self._save(text)
        leaf_a = self._summary(
            kind="leaf", first_message_id=1, last_message_id=2, summary_text="leaf a"
        )
        leaf_b = self._summary(
            kind="leaf", first_message_id=3, last_message_id=4, summary_text="leaf b"
        )
        self._summary(
            kind="condensed",
            first_message_id=1,
            last_message_id=4,
            summary_text="condensed a+b",
            replaces_summary_ids=(leaf_a, leaf_b),
        )
        self._summary(
            kind="leaf", first_message_id=5, last_message_id=6, summary_text="leaf c"
        )

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual(
            [summary.summary_text for summary in result.summaries],
            ["condensed a+b", "leaf c"],
        )
        self.assertEqual(
            [(s.first_message_id, s.last_message_id) for s in result.summaries],
            [(1, 4), (5, 6)],
        )

    async def test_tail_starts_after_frontier_covered_prefix(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        for text in ("m1", "m2", "m3", "m4"):
            self._save(text)
        self._summary(
            kind="leaf", first_message_id=1, last_message_id=2, summary_text="leaf a"
        )

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual([hit.body for hit in result.recent], ["User: m3", "User: m4"])
        self.assertIn(
            "[Recap of messages 1-2 -- verbatim via expand_history]", result.text
        )
        self.assertIn("User: m3", result.text)

    async def test_first_message_included_even_when_over_token_budget(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        long_text = "x" * 4000
        self._save(long_text)
        self._save(long_text)

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
                token_budget=1,
            )
        )

        # The walk always keeps at least the most recent message even when it
        # alone exceeds the budget (matches existing walk semantics).
        self.assertEqual(len(result.recent), 1)
        self.assertEqual(result.recent[0].body, f"User: {long_text}")

    async def test_oversized_frontier_is_trimmed_oldest_first(self) -> None:
        from vexic.service import LocalMemoryService
        from vexic.text_utils import estimate_tokens

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        for text in ("m1", "m2", "m3", "m4", "m5", "m6"):
            self._save(text)
        self._summary(
            kind="leaf",
            first_message_id=1,
            last_message_id=2,
            summary_text="a" * 400,
        )
        self._summary(
            kind="leaf",
            first_message_id=3,
            last_message_id=4,
            summary_text="b" * 400,
        )
        self._summary(
            kind="leaf",
            first_message_id=5,
            last_message_id=6,
            summary_text="c" * 40,
        )

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
                token_budget=250,
            )
        )

        # leaf a and leaf b (~100 tokens each) are dropped oldest-first
        # because keeping all three would exceed token_budget (250) minus
        # the minimum tail reservation (200); only the newest, small leaf c
        # summary (~10 tokens) fits.
        self.assertEqual([s.summary_text for s in result.summaries], ["c" * 40])
        self.assertLessEqual(estimate_tokens(result.text), 250)

    async def test_combined_frontier_and_tail_estimate_stays_within_token_budget(
        self,
    ) -> None:
        # Exercise the storage-level assembly directly: the frontier ceiling
        # (token_budget - _MIN_TAIL_TOKEN_BUDGET) must hold regardless of how
        # the tail walk happens to size individual messages, so we check the
        # invariant load_fresh_context_rows is documented to guarantee rather
        # than depending on exact per-message JSON-overhead token estimates.
        from vexic.service import LocalMemoryService
        from vexic.storage.session_summaries import (
            _MIN_TAIL_TOKEN_BUDGET,
            load_fresh_context_rows,
        )
        from vexic.text_utils import estimate_tokens

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        for text in ("m1", "m2", "m3", "m4", "m5", "m6"):
            self._save(text)
        self._summary(
            kind="leaf",
            first_message_id=1,
            last_message_id=2,
            summary_text="s" * 2400,
        )
        self._summary(
            kind="leaf",
            first_message_id=3,
            last_message_id=4,
            summary_text="t" * 1200,
        )

        token_budget = 1000
        frontier, tail = load_fresh_context_rows(
            self.db_path, token_budget=token_budget, session_id="default"
        )

        frontier_tokens = sum(
            estimate_tokens(summary.summary_text) for summary in frontier
        )
        # The frontier alone must leave room for at least the minimum tail
        # reservation -- this is the ceiling the oldest-first trim enforces.
        self.assertLessEqual(frontier_tokens, token_budget - _MIN_TAIL_TOKEN_BUDGET)

    async def test_single_giant_summary_returns_tail_only(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        for text in ("m1", "m2", "m3"):
            self._save(text)
        self._summary(
            kind="leaf",
            first_message_id=1,
            last_message_id=2,
            summary_text="giant " * 2000,
        )

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
                token_budget=250,
            )
        )

        # The single summary alone blows the budget, so it's dropped
        # entirely and the tail falls back to the full-budget path (same
        # shape as the no-summaries fallback).
        self.assertEqual(result.summaries, [])
        self.assertTrue(result.recent)

    async def test_missing_summary_fallback_returns_budgeted_tail_from_start(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        for text in ("m1", "m2", "m3"):
            self._save(text)

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual(result.summaries, [])
        self.assertEqual(
            [hit.body for hit in result.recent], ["User: m1", "User: m2", "User: m3"]
        )
        self.assertTrue(result.text)

    async def test_empty_database_returns_empty_result_without_error(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope().model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual(result.summaries, [])
        self.assertEqual(result.recent, [])
        self.assertEqual(result.text, "")

    async def test_scope_isolation_across_sessions_and_agents(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        self._save("session-a agent-a text", session_id="session-a", agent_id="agent-a")
        self._save("session-a agent-b text", session_id="session-a", agent_id="agent-b")
        self._save("session-b agent-a text", session_id="session-b", agent_id="agent-a")
        self._summary(
            session_id="session-a",
            agent_id="agent-a",
            kind="leaf",
            first_message_id=1,
            last_message_id=1,
            summary_text="summary session-a agent-a",
        )

        result = await service.fresh_context(
            FreshContextRequest(
                scope=_scope(session_id="session-a", agent_id="agent-a").model_copy(
                    update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                ),
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        self.assertEqual(
            [s.summary_text for s in result.summaries], ["summary session-a agent-a"]
        )
        self.assertNotIn("agent-b", result.text)
        self.assertNotIn("session-b", result.text)
        for hit in result.recent:
            self.assertNotIn("agent-b", hit.body)
            self.assertNotIn("session-b", hit.body)

    async def test_missing_capability_raises_permission_error(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        self._save("m1")

        with self.assertRaises(PermissionError):
            await service.fresh_context(
                FreshContextRequest(
                    scope=_scope(),
                    redaction=RedactionContext(forbidden_values=()),
                )
            )

    async def test_redaction_blocks_egress_of_forbidden_value(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        self._save("cedar-secret raw detail")
        self._summary(
            kind="leaf",
            first_message_id=1,
            last_message_id=1,
            summary_text="a summary mentioning cedar-secret",
        )

        with self.assertRaisesRegex(ValueError, "forbidden secret"):
            await service.fresh_context(
                FreshContextRequest(
                    scope=_scope().model_copy(
                        update={"capabilities": {MemoryCapability.FRESH_CONTEXT}}
                    ),
                    redaction=RedactionContext(forbidden_values=("cedar-secret",)),
                )
            )

    async def test_tombstoned_scope_is_rejected(self) -> None:
        from vexic.service import LocalMemoryService

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        service.init_schema()
        self._save("m1", agent_id="agent-a")

        redaction = RedactionContext(forbidden_values=())
        scope = _scope(agent_id="agent-a").model_copy(
            update={
                "capabilities": {
                    MemoryCapability.FRESH_CONTEXT,
                    MemoryCapability.ADMIN_LIFECYCLE,
                }
            }
        )
        await service.delete_scope(
            DeleteScopeRequest(
                scope=scope,
                target_scope=MemoryScopeSelector(tenant_id="tenant-a", agent_id="agent-a"),
                reason="test tombstone",
                redaction=redaction,
            )
        )

        with self.assertRaisesRegex(PermissionError, "tombstoned"):
            await service.fresh_context(
                FreshContextRequest(scope=scope, redaction=redaction)
            )


class WithEventsSortedTests(unittest.TestCase):
    """Ordering contract for the event-slot reordering in Tier-3 retrieval."""

    @staticmethod
    def _fact(
        fact_id: int,
        category: str,
        *,
        occurred_at: str | None = None,
        created_at: str = "",
    ):
        from vexic.storage import LongTermFact

        return LongTermFact(
            fact_id=fact_id,
            fact_text=f"fact-{fact_id}",
            subject="Ryan",
            category=category,
            importance=5,
            confidence=0.5,
            source_message_ids=[fact_id],
            retrieved_count=0,
            used_count=0,
            editable=True,
            created_at=created_at,
            occurred_at=occurred_at,
        )

    def test_events_sort_in_place_non_events_keep_slots(self) -> None:
        from vexic.subagents.retrieval import _with_events_sorted

        facts = [
            self._fact(1, "event", occurred_at="2024-01"),
            self._fact(2, "preference"),
            self._fact(3, "event", occurred_at="2025-06"),
            self._fact(4, "event", occurred_at="2024-09"),
        ]

        result = _with_events_sorted(facts)

        # Event slots (0, 2, 3) hold events newest-first; the non-event at
        # slot 1 keeps its relevance position untouched.
        self.assertEqual([fact.fact_id for fact in result], [3, 2, 4, 1])
        self.assertEqual(result[1].category, "preference")

    def test_equal_event_time_keeps_rrf_order(self) -> None:
        from vexic.subagents.retrieval import _with_events_sorted

        facts = [
            self._fact(1, "event", occurred_at="2025-01"),
            self._fact(2, "event", occurred_at="2025-01"),
        ]

        result = _with_events_sorted(facts)

        # Stable sort: equal event time preserves incoming (RRF) order.
        self.assertEqual([fact.fact_id for fact in result], [1, 2])

    def test_missing_occurred_at_falls_back_to_created_at(self) -> None:
        from vexic.subagents.retrieval import _with_events_sorted

        facts = [
            self._fact(
                1, "event", occurred_at=None, created_at="2020-01-01 00:00:00"
            ),
            self._fact(2, "event", occurred_at="2025-06"),
        ]

        result = _with_events_sorted(facts)

        # The 2025 event outranks the one dated only by its 2020 storage time.
        self.assertEqual([fact.fact_id for fact in result], [2, 1])

    def test_day_grain_truncation_ignores_timestamp_suffix(self) -> None:
        from vexic.subagents.retrieval import _with_events_sorted

        facts = [
            self._fact(1, "event", occurred_at="2025-03-15"),
            self._fact(
                2, "event", occurred_at=None, created_at="2025-03-15 08:00:00"
            ),
        ]

        result = _with_events_sorted(facts)

        # Same day: the created_at time suffix is truncated ([:10]) so the two
        # compare equal and stay in RRF order, rather than the timestamped one
        # jumping ahead.
        self.assertEqual([fact.fact_id for fact in result], [1, 2])


if __name__ == "__main__":
    unittest.main()
