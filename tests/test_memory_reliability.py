import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.deep import run_deep_phase, select_promotions
from vexic.embeddings import EMBEDDING_DIM
from vexic.models import ContradictionJudgment, FactCandidate
from vexic.pipeline import run_light_phase
from vexic.rem import run_rem_phase
from vexic.storage import (
    commit_deep_cycle,
    commit_dream_cycle,
    fetch_long_term_facts,
    init_db,
    load_promotion_candidates,
    record_candidate_retrieval,
    save_messages,
)
from vexic.storage.candidates import (
    PromotionCandidate,
    _load_candidate_by_id,
    keyword_candidate_ids,
    nearest_candidate_ids,
    read_candidate_for_promotion,
)
from vexic.storage.connection import connect
from vexic.storage.longterm import (
    insert_long_term_fact,
    keyword_long_term_fact_ids,
    nearest_long_term_facts,
)
from vexic.storage.promotion import PromotionDecision
from vexic.storage.schema import (
    _backfill_mentioned_at,
    _earliest_mention_date,
    _ensure_vector_memory_schema,
    _reset_init_memo,
    init_vector_memory,
)
from vexic.subagents.retrieval import retrieve_long_term_facts
from vexic.tools import expand_history, search_long_term, search_memory


REPO_ROOT = Path(__file__).resolve().parents[1]


def _unit_vector(first: float, second: float = 0.0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = first
    if EMBEDDING_DIM > 1:
        vector[1] = second
    return vector


def _candidate(
    fact_text: str,
    *,
    message_ids: list[int],
    category: str = "fact",
    confidence: float = 0.8,
    occurred_at: str | None = None,
) -> FactCandidate:
    return FactCandidate(
        fact_text=fact_text,
        subject="Ryan",
        category=category,
        importance=6,
        confidence=confidence,
        source_message_ids=message_ids,
        occurred_at=occurred_at,
    )


class _FakeResult:
    def __init__(self, output: object) -> None:
        self.output = output

    def usage(self) -> object:
        return type(
            "FakeUsage",
            (),
            {
                "requests": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        )()


class _StableExtractionAgent:
    async def run(self, transcript: str) -> _FakeResult:
        return _FakeResult(
            [
                _candidate(
                    "Ryan prefers compact reliability reports with provenance.",
                    message_ids=[1],
                    category="preference",
                )
            ]
        )


class _ContradictionAgent:
    def __init__(self, *, contradicts: bool, confidence: float = 0.9) -> None:
        self.contradicts = contradicts
        self.confidence = confidence

    async def run(self, prompt: str) -> _FakeResult:
        return _FakeResult(
            ContradictionJudgment(
                contradicts=self.contradicts,
                reason="synthetic reliability fixture",
                confidence=self.confidence,
            )
        )


class MemoryReliabilityCommandDocumentationTests(unittest.TestCase):
    def test_reliability_gate_discovery_markers_are_documented(self) -> None:
        usage_doc = (REPO_ROOT / "docs" / "usage.md").read_text()

        self.assertIn("## Running the Project", usage_doc)
        self.assertIn("<!-- memory-reliability-gate -->", usage_doc)
        self.assertIn("<!-- memory-reliability-live-smoke -->", usage_doc)


class FreshCandidateFallbackReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        self.ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db_path=self.db_path,
                session_id="default",
                secrets={},
                authority=None,
                retrieved_facts_this_turn=[],
            ),
            usage=None,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _stage_candidate(
        self,
        fact_text: str,
        *,
        candidate_id: int,
        embedding: list[float],
    ) -> None:
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[candidate_id, candidate_id + 100])],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=candidate_id,
        )

    def _promote(
        self,
        fact_text: str,
        *,
        candidate_id: int,
        embedding: list[float],
    ) -> None:
        self._stage_candidate(fact_text, candidate_id=candidate_id, embedding=embedding)
        commit_deep_cycle(
            self.db_path,
            [PromotionDecision(candidate_id=candidate_id, embedding=embedding)],
            started_at="2026-06-02T00:00:00+00:00",
            finished_at="2026-06-02T00:00:01+00:00",
        )

    async def test_fresh_candidate_surfaces_only_as_unverified_note_with_counter_event_consistency(
        self,
    ) -> None:
        self._stage_candidate(
            "Ryan's preferred launch checklist starts with a calendar audit.",
            candidate_id=1,
            embedding=_unit_vector(1.0),
        )
        self.ctx.deps.embed = lambda texts: [_unit_vector(1.0) for _ in texts]

        result = await search_long_term(
            self.ctx,
            "what starts Ryan's launch checklist?",
        )

        self.assertIn("No durable long-term memories matched", result)
        self.assertIn("[unverified note]", result)
        self.assertIn("calendar audit", result)
        self.assertIn("source messages: 1, 101", result)
        self.assertNotIn("[fact ", result)

        with closing(sqlite3.connect(self.db_path)) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM candidate_retrieval_events WHERE candidate_id = 1"
            ).fetchone()[0]
            retrieved_count = conn.execute(
                "SELECT retrieved_count FROM memory_candidates WHERE id = 1"
            ).fetchone()[0]
            tier3_count = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]

        self.assertEqual(event_count, 1, "candidate fallback must write one event")
        self.assertEqual(
            retrieved_count,
            event_count,
            "candidate retrieved_count must stay derivable from candidate_retrieval_events",
        )
        self.assertEqual(tier3_count, 0, "candidate fallback must not write Tier 3 events")

    async def test_tier3_hit_suppresses_fresh_candidate_fallback(self) -> None:
        self._promote(
            "Ryan likes Python for platform automation.",
            candidate_id=1,
            embedding=_unit_vector(1.0),
        )
        self._stage_candidate(
            "Ryan's sister is named Mara.",
            candidate_id=2,
            embedding=_unit_vector(0.0, 1.0),
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            return_value=[_unit_vector(1.0)],
        ):
            result = await search_long_term(self.ctx, "what language does Ryan like?")

        self.assertIn("[fact ", result)
        self.assertIn("Ryan likes Python", result)
        self.assertNotIn("[unverified note]", result)
        self.assertNotIn("Mara", result)

        with closing(sqlite3.connect(self.db_path)) as conn:
            candidate_event_count = conn.execute(
                "SELECT COUNT(*) FROM candidate_retrieval_events"
            ).fetchone()[0]
            tier3_event_count = conn.execute(
                "SELECT COUNT(*) FROM retrieval_events"
            ).fetchone()[0]

        self.assertEqual(candidate_event_count, 0)
        self.assertGreaterEqual(tier3_event_count, 1)


class StableRecallReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        self.ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db_path=self.db_path,
                session_id="default",
                secrets={},
                authority=None,
                retrieved_facts_this_turn=[],
            ),
            usage=None,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_repeated_observations_promote_with_provenance_and_degrade_when_use_judge_skips(
        self,
    ) -> None:
        fact_text = "Ryan prefers compact reliability reports with provenance."
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[1], category="preference")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[2], category="preference")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:01:00+00:00",
            finished_at="2026-06-01T00:01:01+00:00",
            messages_processed=1,
            last_processed_message_id=2,
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            candidate = conn.execute(
                """
                SELECT hit_count, source_message_ids, promoted
                FROM memory_candidates
                WHERE id = 1
                """
            ).fetchone()

        self.assertEqual(
            candidate,
            (2, "[1, 2]", 0),
            "stable recall should reinforce one Candidate before promotion",
        )

        commit_deep_cycle(
            self.db_path,
            [PromotionDecision(candidate_id=1, embedding=embedding)],
            started_at="2026-06-02T00:00:00+00:00",
            finished_at="2026-06-02T00:00:01+00:00",
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            return_value=[embedding],
        ):
            result = await search_long_term(self.ctx, "how should reliability be reported?")

        self.assertIn("[fact 1]", result)
        self.assertIn(fact_text, result)
        self.assertIn("category: preference", result)
        self.assertIn("source messages: 1, 2", result)
        self.assertNotIn("[unverified note]", result)

        with closing(sqlite3.connect(self.db_path)) as conn:
            fact = conn.execute(
                """
                SELECT source_message_ids, retrieved_count, used_count
                FROM long_term_memory
                WHERE id = 1
                """
            ).fetchone()
            retrieval_event = conn.execute(
                """
                SELECT fact_id, session_id, query, used, judged_at
                FROM retrieval_events
                WHERE fact_id = 1
                """
            ).fetchone()

        self.assertEqual(
            fact,
            ("[1, 2]", 1, 0),
            "Tier 3 retrieval should preserve provenance and leave used_count unchanged when the use judge skips",
        )
        self.assertEqual(retrieval_event[:3], (1, "default", "how should reliability be reported?"))
        self.assertIsNone(
            retrieval_event[3],
            "use-judge failure/skip should degrade to an unjudged event, not a false used verdict",
        )
        self.assertIsNone(retrieval_event[4])
        self.assertEqual(len(self.ctx.deps.retrieved_facts_this_turn), 1)
        self.assertEqual(self.ctx.deps.retrieved_facts_this_turn[0].event_id, 1)


class DreamCycleReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_light_rem_deep_cycle_is_idempotent_and_keeps_transcript_append_only(
        self,
    ) -> None:
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            original_messages = conn.execute(
                "SELECT id, session_id, message_json FROM messages ORDER BY id"
            ).fetchall()

        with (
            patch("vexic.pipeline.build_extraction_agent", return_value=_StableExtractionAgent()),
            patch(
                "vexic.deep.build_contradiction_agent",
                return_value=_ContradictionAgent(contradicts=False),
            ),
        ):
            await run_light_phase(
                self.db_path,
                "glm",
                embed=lambda texts: [_unit_vector(1.0) for _ in texts],
            )
            await run_rem_phase(self.db_path)
            await run_deep_phase(self.db_path, "glm")
            await run_light_phase(
                self.db_path,
                "glm",
                embed=lambda texts: [_unit_vector(1.0) for _ in texts],
            )
            await run_rem_phase(self.db_path)
            await run_deep_phase(self.db_path, "glm")

        with closing(sqlite3.connect(self.db_path)) as conn:
            messages_after = conn.execute(
                "SELECT id, session_id, message_json FROM messages ORDER BY id"
            ).fetchall()
            candidate_count = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
            fact_count = conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]
            promoted = conn.execute(
                "SELECT promoted, promoted_fact_id FROM memory_candidates WHERE id = 1"
            ).fetchone()
            audit_counts = conn.execute(
                """
                SELECT
                    COUNT(CASE WHEN promotions = 1 THEN 1 END),
                    COUNT(CASE WHEN messages_processed = 0 THEN 1 END)
                FROM dream_runs
                WHERE status = 'ok'
                """
            ).fetchone()

        self.assertEqual(
            messages_after,
            original_messages,
            "Light->REM->Deep must not UPDATE/DELETE Tier 1 transcript rows",
        )
        self.assertEqual(candidate_count, 1, "idempotent cycle must not duplicate Candidates")
        self.assertEqual(fact_count, 1, "idempotent cycle must not duplicate Tier 3 facts")
        self.assertEqual(promoted, (1, 1))
        self.assertEqual(audit_counts[0], 1, "only the first Deep run should promote")
        self.assertGreaterEqual(audit_counts[1], 2, "second cycle should audit no-op phases")


class ContradictionSupersessionReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _stage_candidate(
        self,
        fact_text: str,
        *,
        candidate_id: int,
        confidence: float,
    ) -> None:
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[candidate_id], confidence=confidence)],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=candidate_id,
        )

    async def test_conflicting_higher_confidence_fact_retires_old_fact_without_deleting_canonical_rows(
        self,
    ) -> None:
        self._stage_candidate("Ryan uses VS Code.", candidate_id=1, confidence=0.6)
        commit_deep_cycle(
            self.db_path,
            [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
            started_at="2026-06-02T00:00:00+00:00",
            finished_at="2026-06-02T00:00:01+00:00",
        )
        self._stage_candidate("Ryan uses Neovim now.", candidate_id=2, confidence=0.9)

        await run_deep_phase(
            self.db_path,
            "glm",
            contradiction_agent_factory=lambda *_args, **_kwargs: _ContradictionAgent(
                contradicts=True
            ),
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            facts = conn.execute(
                """
                SELECT id, fact_text, retired, retired_by_fact_id
                FROM long_term_memory
                ORDER BY id ASC
                """
            ).fetchall()
            candidates = conn.execute(
                """
                SELECT id, promoted, promoted_fact_id, retired
                FROM memory_candidates
                ORDER BY id ASC
                """
            ).fetchall()

        self.assertEqual(
            facts,
            [
                (1, "Ryan uses VS Code.", 1, 2),
                (2, "Ryan uses Neovim now.", 0, None),
            ],
            "supersession should retire, not delete, the old canonical Tier 3 row",
        )
        self.assertEqual(candidates, [(1, 1, 1, 0), (2, 1, 2, 0)])


class KnowledgeUpdateSupersessionTests(unittest.IsolatedAsyncioTestCase):
    # ADR 0037 acceptance, the 852ce960 eval shape: a knowledge update
    # extracted as an undated event ("mortgage is $400k now") must escape the
    # Tier 2 sink via mentioned_at, reach Tier 3, and retire the stale fact
    # through the contradiction path — with occurred_at never fabricated.

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_undated_event_update_reaches_tier3_and_retires_stale_fact(self) -> None:
        old_message_id = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="My mortgage is $350k.")])],
            timestamp="2026-01-10T09:00:00+00:00",
        )[0]
        new_message_id = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="The mortgage is $400k now.")])],
            timestamp="2026-03-05T10:00:00+00:00",
        )[0]

        # Stage and promote the stale fact at lower confidence, so the
        # incoming higher-confidence update wins the contradiction instead of
        # being blocked by the neighbor rule.
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan's mortgage is $350k.",
                    message_ids=[old_message_id],
                    confidence=0.6,
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=old_message_id,
        )
        commit_deep_cycle(
            self.db_path,
            [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
            started_at="2026-06-01T00:01:00+00:00",
            finished_at="2026-06-01T00:01:01+00:00",
        )

        update_candidate = _candidate(
            "Ryan's mortgage is $400k.",
            message_ids=[new_message_id],
            category="event",
            confidence=0.9,
            occurred_at=None,
        )

        class _UpdateExtractionAgent:
            async def run(self, transcript: str) -> _FakeResult:
                return _FakeResult([update_candidate])

        # Same unit embedding as the stale fact so the nearest-neighbor scan
        # surfaces it for the contradiction judge.
        with patch(
            "vexic.pipeline.build_extraction_agent",
            return_value=_UpdateExtractionAgent(),
        ):
            await run_light_phase(
                self.db_path,
                "glm",
                embed=lambda texts: [_unit_vector(1.0) for _ in texts],
            )
        await run_deep_phase(
            self.db_path,
            "glm",
            contradiction_agent_factory=lambda *_args, **_kwargs: _ContradictionAgent(
                contradicts=True
            ),
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            new_fact = conn.execute(
                """
                SELECT id, occurred_at, mentioned_at, retired
                FROM long_term_memory
                WHERE fact_text = 'Ryan''s mortgage is $400k.'
                """
            ).fetchone()
            old_fact = conn.execute(
                """
                SELECT retired, retired_by_fact_id
                FROM long_term_memory
                WHERE fact_text = 'Ryan''s mortgage is $350k.'
                """
            ).fetchone()
            message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        self.assertIsNotNone(new_fact, "the $400k update must reach Tier 3")
        new_fact_id, occurred_at, mentioned_at, new_retired = new_fact
        self.assertEqual(new_retired, 0)
        self.assertIsNone(occurred_at, "occurred_at must never be fabricated from mention time")
        self.assertEqual(mentioned_at, "2026-03-05")
        self.assertEqual(
            old_fact,
            (1, new_fact_id),
            "supersession must retire the stale fact in place (Invariant 6)",
        )
        self.assertEqual(message_count, 2, "Tier 1 transcript stays append-only")


class MemoryIsolationAndRedactionReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        self.other_tenant_db_path = str(Path(self.temp_dir.name) / "other-memory.db")
        init_db(self.db_path)
        init_db(self.other_tenant_db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _ctx(self, db_path: str, *, session_id: str, secrets: dict[str, str] | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            deps=SimpleNamespace(
                db_path=db_path,
                session_id=session_id,
                secrets=secrets or {},
                authority=None,
                retrieved_facts_this_turn=[],
            ),
            usage=None,
        )

    def test_search_memory_and_expand_history_are_scoped_to_current_session(self) -> None:
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="default session cedar detail")])],
            session_id="default",
        )
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="telegram session cedar detail")])],
            session_id="telegram:42",
        )

        default_ctx = self._ctx(self.db_path, session_id="default")
        telegram_ctx = self._ctx(self.db_path, session_id="telegram:42")

        default_search = search_memory(default_ctx, "cedar")
        telegram_search = search_memory(telegram_ctx, "cedar")
        default_expand = expand_history(default_ctx, 1, 2)
        telegram_expand = expand_history(telegram_ctx, 1, 2)

        self.assertIn("default session cedar detail", default_search)
        self.assertNotIn("telegram session cedar detail", default_search)
        self.assertIn("telegram session cedar detail", telegram_search)
        self.assertNotIn("default session cedar detail", telegram_search)
        self.assertIn("default session cedar detail", default_expand)
        self.assertNotIn("telegram session cedar detail", default_expand)
        self.assertIn("telegram session cedar detail", telegram_expand)
        self.assertNotIn("default session cedar detail", telegram_expand)

    async def test_source_transcript_ledger_failure_rolls_back_message_and_fts(self) -> None:
        from vexic.contract import (
            IngestSourceTranscriptRequest,
            MemoryCapability,
            MemoryScope,
            Principal,
            PrincipalType,
            RedactionContext,
            SourceTranscriptMessage,
            TrustBoundary,
        )
        from vexic.service import LocalMemoryService
        from vexic.storage import single_message_adapter

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_source_ledger_insert
                BEFORE INSERT ON source_transcript_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'ledger insert failed');
                END
                """
            )
            conn.commit()

        service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")
        scope = MemoryScope(
            tenant_id="tenant-a",
            session_id="default",
            principal=Principal(
                principal_id="test-operator",
                principal_type=PrincipalType.OPERATOR,
            ),
            trust_boundary=TrustBoundary.LOCAL_TRUSTED,
            capabilities={MemoryCapability.WRITE},
        )

        with self.assertRaises(sqlite3.IntegrityError):
            await service.ingest_source_transcript(
                IngestSourceTranscriptRequest(
                    scope=scope,
                    messages=[
                        SourceTranscriptMessage(
                            source_host="claude-code",
                            source_session_id="session-1",
                            source_message_id="uuid-1",
                            message_json=single_message_adapter.dump_json(
                                ModelRequest(
                                    parts=[UserPromptPart(content="atomic cedar")]
                                )
                            ).decode(),
                        )
                    ],
                    redaction=RedactionContext(forbidden_values=()),
                )
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_transcript_ledger").fetchone()[0],
                0,
            )

    def test_search_memory_fails_closed_on_loaded_secret_egress(self) -> None:
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="cedar-secret transcript")])],
            session_id="default",
        )

        with self.assertRaisesRegex(ValueError, "forbidden secret"):
            search_memory(
                self._ctx(
                    self.db_path,
                    session_id="default",
                    secrets={"api_key": "cedar-secret"},
                ),
                "cedar",
            )

    async def test_long_term_fact_search_fails_closed_on_loaded_secret_egress(self) -> None:
        commit_dream_cycle(
            self.db_path,
            [_candidate("Ryan stores cedar-secret in memory.", message_ids=[1])],
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
            started_at="2026-06-02T00:00:00+00:00",
            finished_at="2026-06-02T00:00:01+00:00",
        )

        with (
            patch(
                "vexic.subagents.retrieval.embed_texts",
                return_value=[_unit_vector(1.0)],
            ),
            self.assertRaisesRegex(ValueError, "forbidden secret"),
        ):
            await search_long_term(
                self._ctx(
                    self.db_path,
                    session_id="default",
                    secrets={"api_key": "cedar-secret"},
                ),
                "memory storage",
            )

    def test_candidate_retrieval_redaction_fails_closed_before_event_or_counter_write(self) -> None:
        commit_dream_cycle(
            self.db_path,
            [_candidate("Ryan has a private launch token.", message_ids=[1])],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )

        with self.assertRaisesRegex(ValueError, "forbidden secret"):
            record_candidate_retrieval(
                self.db_path,
                [1],
                session_id="default",
                query="lookup secret-token",
                forbidden_secret_values=["secret-token"],
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM candidate_retrieval_events"
            ).fetchone()[0]
            retrieved_count = conn.execute(
                "SELECT retrieved_count FROM memory_candidates WHERE id = 1"
            ).fetchone()[0]

        self.assertEqual(event_count, 0)
        self.assertEqual(retrieved_count, 0)

    async def test_long_term_and_candidate_retrieval_are_scoped_to_tenant_database(self) -> None:
        commit_dream_cycle(
            self.db_path,
            [_candidate("Ryan tenant A likes Python.", message_ids=[1])],
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
            started_at="2026-06-02T00:00:00+00:00",
            finished_at="2026-06-02T00:00:01+00:00",
        )
        commit_dream_cycle(
            self.other_tenant_db_path,
            [_candidate("Ryan tenant B likes Ruby.", message_ids=[1])],
            candidate_embeddings=[_unit_vector(0.0, 1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            return_value=[_unit_vector(1.0)],
        ):
            tenant_a_result = await search_long_term(
                self._ctx(self.db_path, session_id="default"),
                "what language does Ryan like?",
            )
        with patch(
            "vexic.subagents.retrieval.embed_texts",
            return_value=[_unit_vector(0.0, 1.0)],
        ):
            tenant_b_result = await search_long_term(
                self._ctx(self.other_tenant_db_path, session_id="default"),
                "what language does Ryan like?",
            )

        self.assertIn("tenant A likes Python", tenant_a_result)
        self.assertNotIn("tenant B likes Ruby", tenant_a_result)
        self.assertIn("[unverified note]", tenant_b_result)
        self.assertIn("tenant B likes Ruby", tenant_b_result)
        self.assertNotIn("tenant A likes Python", tenant_b_result)


class OfflineRetrievalBaselinePreflightTests(unittest.IsolatedAsyncioTestCase):
    """Deterministic offline preflight for the live retrieval-quality baseline.

    Drives a full Tier 1 -> Light -> REM -> Deep -> retrieve cycle against an
    isolated disposable database with injected fake agents and embeddings, so
    the plumbing the live provider-backed baseline depends on is exercised with
    zero provider calls. Emits the per-row diagnostic taxonomy (Tier 1 found,
    Tier 2 extracted, Tier 3 promoted, Tier 3 retrieved, candidate fallback
    used) the live harness must reproduce, and asserts a planted preference is
    recalled as a durable Tier 3 fact rather than an unverified fallback note.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        self.ctx = SimpleNamespace(
            deps=SimpleNamespace(
                db_path=self.db_path,
                session_id="default",
                secrets={},
                authority=None,
                retrieved_facts_this_turn=[],
            ),
            usage=None,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _diagnostics(self, retrieval_text: str) -> dict[str, object]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            tier1_found = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            tier2_extracted = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
            tier3_promoted = conn.execute(
                "SELECT COUNT(*) FROM long_term_memory WHERE retired = 0"
            ).fetchone()[0]
        return {
            "tier1_found": tier1_found,
            "tier2_extracted": tier2_extracted,
            "tier3_promoted": tier3_promoted,
            "tier3_retrieved": "[fact " in retrieval_text,
            "candidate_fallback_used": "[unverified note]" in retrieval_text,
        }

    async def test_full_offline_cycle_recalls_planted_fact_as_durable_tier3(self) -> None:
        save_messages(
            self.db_path,
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content="I prefer compact reliability reports with provenance."
                        )
                    ]
                )
            ],
        )

        with (
            patch("vexic.pipeline.build_extraction_agent", return_value=_StableExtractionAgent()),
            patch(
                "vexic.deep.build_contradiction_agent",
                return_value=_ContradictionAgent(contradicts=False),
            ),
        ):
            await run_light_phase(
                self.db_path,
                "glm",
                embed=lambda texts: [_unit_vector(1.0) for _ in texts],
            )
            await run_rem_phase(self.db_path)
            await run_deep_phase(self.db_path, "glm")

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            return_value=[_unit_vector(1.0)],
        ):
            result = await search_long_term(self.ctx, "how should reliability be reported?")

        self.assertEqual(
            self._diagnostics(result),
            {
                "tier1_found": 1,
                "tier2_extracted": 1,
                "tier3_promoted": 1,
                "tier3_retrieved": True,
                "candidate_fallback_used": False,
            },
            "offline preflight must drive Tier 1 -> Tier 2 -> Tier 3 and recall a durable fact",
        )
        self.assertIn("[fact 1]", result)
        self.assertIn("Ryan prefers compact reliability reports with provenance.", result)
        self.assertIn("source messages: 1", result)
        self.assertNotIn("[unverified note]", result)


class OccurredAtRoundTripTests(unittest.TestCase):
    # `occurred_at` is a nullable, flexible ISO-ish event-time string
    # (e.g. "2025-03" or "2025-03-14") threaded through Tier 2 candidate
    # insert/merge/load and Tier 3 fact insert/fetch. These tests exercise the
    # SQL wiring directly; promotion.py's carry-through of occurred_at into
    # Tier 3 is a separate, later task and is intentionally not asserted here.

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _committed_candidate_id(self) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT id FROM memory_candidates ORDER BY id DESC LIMIT 1").fetchone()
        return int(row[0])

    def test_insert_persists_occurred_at(self) -> None:
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [_candidate("Ryan started a new job.", message_ids=[1], occurred_at="2025-03-14")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        candidate_id = self._committed_candidate_id()

        with closing(sqlite3.connect(self.db_path)) as conn:
            occurred_at = conn.execute(
                "SELECT occurred_at FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()[0]
        self.assertEqual(occurred_at, "2025-03-14")

        loaded = load_promotion_candidates(self.db_path)
        self.assertEqual(loaded[0].occurred_at, "2025-03-14")

        with closing(connect(self.db_path)) as conn:
            candidate = _load_candidate_by_id(conn, candidate_id)
        self.assertEqual(candidate.occurred_at, "2025-03-14")

        with closing(connect(self.db_path)) as conn:
            row = read_candidate_for_promotion(conn, candidate_id)
        self.assertEqual(len(row), 15, "mentioned_at must be appended as the last column")
        self.assertEqual(row[-2], "2025-03-14", "occurred_at is the second-to-last column")

    def test_merge_backfills_missing_occurred_at_without_clobbering_known_date(self) -> None:
        fact_text = "Ryan started a new job."
        embedding = _unit_vector(1.0)
        # First observation has no event-time yet.
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[1], occurred_at=None)],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        candidate_id = self._committed_candidate_id()

        # A later duplicate observation supplies the date; merge should
        # backfill it via COALESCE(occurred_at, ?).
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[2], occurred_at="2025-03")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:01:00+00:00",
            finished_at="2026-06-01T00:01:01+00:00",
            messages_processed=1,
            last_processed_message_id=2,
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            occurred_at = conn.execute(
                "SELECT occurred_at FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()[0]
        self.assertEqual(occurred_at, "2025-03")

        # A third duplicate observation with a *different* date must not
        # clobber the date already known.
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[3], occurred_at="1999-01-01")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:02:00+00:00",
            finished_at="2026-06-01T00:02:01+00:00",
            messages_processed=1,
            last_processed_message_id=3,
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            occurred_at = conn.execute(
                "SELECT occurred_at FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()[0]
        self.assertEqual(
            occurred_at,
            "2025-03",
            "a later duplicate's occurred_at must not clobber a date already known",
        )

    def test_insert_persists_year_only_occurred_at_as_string(self) -> None:
        # Regression: a bare year like "2025" is a well-formed SQLite integer
        # literal. A column declared DATETIME has NUMERIC affinity and would
        # silently coerce it to INTEGER 2025 on write; occurred_at must be
        # declared TEXT so a partial-precision date round-trips as a string.
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [_candidate("Ryan moved to Vancouver.", message_ids=[1], occurred_at="2025")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        candidate_id = self._committed_candidate_id()

        with closing(sqlite3.connect(self.db_path)) as conn:
            occurred_at, type_name = conn.execute(
                "SELECT occurred_at, typeof(occurred_at) FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        self.assertEqual(occurred_at, "2025")
        self.assertEqual(type_name, "text")

    def test_merge_treats_blank_occurred_at_as_missing_for_backfill(self) -> None:
        # Regression: COALESCE(occurred_at, ?) only treats SQL NULL as
        # replaceable. An empty string is not NULL, so a first observation
        # that (incorrectly) produced "" instead of None would otherwise
        # permanently block a later observation from backfilling a real date.
        fact_text = "Ryan started a new job."
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[1], occurred_at="")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        candidate_id = self._committed_candidate_id()

        commit_dream_cycle(
            self.db_path,
            [_candidate(fact_text, message_ids=[2], occurred_at="2025-03")],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:01:00+00:00",
            finished_at="2026-06-01T00:01:01+00:00",
            messages_processed=1,
            last_processed_message_id=2,
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            occurred_at = conn.execute(
                "SELECT occurred_at FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()[0]
        self.assertEqual(
            occurred_at,
            "2025-03",
            "a blank occurred_at from the first observation must not block backfill",
        )

    def test_insert_long_term_fact_and_fetch_round_trip_occurred_at(self) -> None:
        init_vector_memory(self.db_path)
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text="Ryan started a new job.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    agent_id=None,
                    promoted_from_candidate_id=1,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=_unit_vector(1.0),
                    occurred_at="2025-03-14",
                )

        facts = fetch_long_term_facts(self.db_path, [fact_id])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].occurred_at, "2025-03-14")

    def test_insert_long_term_fact_defaults_occurred_at_to_none(self) -> None:
        init_vector_memory(self.db_path)
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text="Ryan likes Python.",
                    subject="Ryan",
                    category="preference",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    agent_id=None,
                    promoted_from_candidate_id=1,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=_unit_vector(1.0),
                )

        facts = fetch_long_term_facts(self.db_path, [fact_id])
        self.assertEqual(len(facts), 1)
        self.assertIsNone(facts[0].occurred_at)


class MentionedAtDerivationTests(unittest.TestCase):
    # `mentioned_at` is deterministic provenance: the earliest UTC calendar
    # date (date-only ISO string) of a candidate's source messages, derived
    # from messages.timestamp at insert. Never LLM-derived, never fabricated;
    # occurred_at stays event-time-only (ADR 0037).

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save_message(self, text: str, *, timestamp: str | None = None) -> int:
        return save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content=text)])],
            timestamp=timestamp,
        )[0]

    def _commit_candidate(
        self, candidate: FactCandidate, *, embedding: list[float] | None = None
    ) -> int:
        commit_dream_cycle(
            self.db_path,
            [candidate],
            candidate_embeddings=[embedding if embedding is not None else _unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=max(candidate.source_message_ids, default=1),
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id FROM memory_candidates ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return int(row[0])

    def _mentioned_at(self, candidate_id: int) -> tuple[object, str]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT mentioned_at, typeof(mentioned_at) FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()

    def test_insert_derives_mentioned_at_from_earliest_source_message(self) -> None:
        first = self._save_message(
            "We finally visited Yellowstone.",
            timestamp="2026-03-05T10:00:00+00:00",
        )
        second = self._save_message(
            "The Yellowstone trip was amazing.",
            timestamp="2026-04-10T09:30:00+00:00",
        )
        candidate_id = self._commit_candidate(
            _candidate(
                "Ryan visited Yellowstone.",
                message_ids=[second, first],
                category="event",
            )
        )

        mentioned_at, type_name = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-03-05")
        self.assertEqual(type_name, "text")

    def test_insert_handles_mixed_timestamp_formats(self) -> None:
        # messages.timestamp arrives in two shapes: naive
        # 'YYYY-MM-DD HH:MM:SS' (SQLite CURRENT_TIMESTAMP default) and
        # offset-aware ISO from ingest. Both parse; naive is treated as UTC.
        naive = self._save_message(
            "Booked the trip.", timestamp="2026-02-01 08:00:00"
        )
        aware = self._save_message(
            "Trip photos are up.", timestamp="2026-05-20T12:00:00+00:00"
        )
        candidate_id = self._commit_candidate(
            _candidate("Ryan booked a trip.", message_ids=[aware, naive], category="event")
        )

        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-02-01")

    def test_insert_leaves_mentioned_at_null_when_source_messages_missing(self) -> None:
        candidate_id = self._commit_candidate(
            _candidate("Ryan visited Yellowstone.", message_ids=[999], category="event")
        )

        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertIsNone(mentioned_at)

    def test_derivation_is_fail_soft_on_bad_timestamps(self) -> None:
        # save_messages stores host-supplied timestamp strings unvalidated, so
        # the derivation must skip garbage rather than raise: a raise would
        # abort the whole Light batch (against ADR 0031 fail-soft) or brick
        # init_db via the ensure backfill.
        garbage = self._save_message("Garbage clock.", timestamp="not-a-date")
        blank = self._save_message("Blank clock.", timestamp="")
        good = self._save_message("Good clock.", timestamp="2026-01-15T00:00:00+00:00")

        candidate_id = self._commit_candidate(
            _candidate(
                "Ryan visited Yellowstone.",
                message_ids=[garbage, blank, good],
                category="event",
            )
        )
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-01-15")

    def test_derivation_skips_timestamps_that_overflow_utc_conversion(self) -> None:
        # Regression (codex audit F4): "0001-01-01T00:00:00+23:59" parses via
        # fromisoformat but astimezone(utc) raises OverflowError. Fail-soft
        # must cover the whole normalization, not just parsing.
        overflow = self._save_message(
            "Ancient clock.", timestamp="0001-01-01T00:00:00+23:59"
        )
        good = self._save_message("Good clock.", timestamp="2026-01-15T00:00:00+00:00")

        candidate_id = self._commit_candidate(
            _candidate(
                "Ryan visited Yellowstone.",
                message_ids=[overflow, good],
                category="event",
            )
        )
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-01-15")

    def test_derivation_returns_null_when_all_timestamps_unparseable(self) -> None:
        garbage = self._save_message("Garbage clock.", timestamp="not-a-date")
        blank = self._save_message("Blank clock.", timestamp="")

        candidate_id = self._commit_candidate(
            _candidate(
                "Ryan visited Yellowstone.",
                message_ids=[garbage, blank],
                category="event",
            )
        )
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertIsNone(mentioned_at)

    def test_loaders_round_trip_mentioned_at(self) -> None:
        dated = self._save_message(
            "We finally visited Yellowstone.",
            timestamp="2026-03-05T10:00:00+00:00",
        )
        candidate_id = self._commit_candidate(
            _candidate("Ryan visited Yellowstone.", message_ids=[dated], category="event")
        )

        loaded = load_promotion_candidates(self.db_path)
        self.assertEqual(loaded[0].mentioned_at, "2026-03-05")

        with closing(connect(self.db_path)) as conn:
            row = read_candidate_for_promotion(conn, candidate_id)
        self.assertEqual(len(row), 15, "mentioned_at must be appended as the last column")
        self.assertEqual(row[-1], "2026-03-05")

    def test_insert_long_term_fact_round_trips_mentioned_at(self) -> None:
        init_vector_memory(self.db_path)
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text="Ryan visited Yellowstone.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    agent_id=None,
                    promoted_from_candidate_id=1,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=_unit_vector(1.0),
                    occurred_at=None,
                    mentioned_at="2026-03-05",
                )

        facts = fetch_long_term_facts(self.db_path, [fact_id])
        self.assertEqual(len(facts), 1)
        self.assertIsNone(facts[0].occurred_at)
        self.assertEqual(facts[0].mentioned_at, "2026-03-05")

    def test_derivation_and_backfill_survive_low_bind_parameter_limits(self) -> None:
        # codex audit F1: an IN (?, ?, ...) clause with one bound parameter
        # per cited message can blow SQLITE_MAX_VARIABLE_NUMBER (999 on older
        # SQLite) on a legacy DB with many cited messages. Ids must ride a
        # single JSON parameter (json_each, the purge.py precedent).
        message_ids = [
            self._save_message(f"note {index}", timestamp="2026-01-15T00:00:00+00:00")
            for index in range(8)
        ]
        candidate_id = self._commit_candidate(
            _candidate("Ryan wrote many notes.", message_ids=message_ids, category="event")
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE memory_candidates SET mentioned_at = NULL WHERE id = ?",
                (candidate_id,),
            )
            conn.commit()

        with closing(connect(self.db_path)) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 5)
            derived = _earliest_mention_date(conn, message_ids)
            _backfill_mentioned_at(conn, "memory_candidates")
            conn.commit()

        self.assertEqual(derived, "2026-01-15")
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-01-15")

    def test_merge_recomputes_mentioned_at_from_merged_source_union(self) -> None:
        # mentioned_at is a pure function of source_message_ids, so a merge
        # recomputes it over the union — an earlier mention discovered by a
        # duplicate observation moves the date back (earliest-mention wins).
        later = self._save_message(
            "The Yellowstone trip was amazing.",
            timestamp="2026-04-10T09:30:00+00:00",
        )
        earlier = self._save_message(
            "We finally visited Yellowstone.",
            timestamp="2026-03-05T10:00:00+00:00",
        )
        fact_text = "Ryan visited Yellowstone."
        candidate_id = self._commit_candidate(
            _candidate(fact_text, message_ids=[later], category="event")
        )
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-04-10")

        merged_id = self._commit_candidate(
            _candidate(fact_text, message_ids=[earlier], category="event")
        )
        self.assertEqual(merged_id, candidate_id, "duplicate must merge, not insert")
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-03-05")

    def test_merge_heals_legacy_null_mentioned_at(self) -> None:
        first = self._save_message(
            "We finally visited Yellowstone.",
            timestamp="2026-03-05T10:00:00+00:00",
        )
        second = self._save_message(
            "The Yellowstone trip was amazing.",
            timestamp="2026-04-10T09:30:00+00:00",
        )
        fact_text = "Ryan visited Yellowstone."
        candidate_id = self._commit_candidate(
            _candidate(fact_text, message_ids=[first], category="event")
        )
        # Simulate a pre-column legacy row.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE memory_candidates SET mentioned_at = NULL WHERE id = ?",
                (candidate_id,),
            )
            conn.commit()

        self._commit_candidate(_candidate(fact_text, message_ids=[second], category="event"))
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-03-05")

    def test_init_db_backfills_mentioned_at_for_legacy_rows(self) -> None:
        # Pre-column rows (the eval-DB sink: 371 stuck undated events) heal on
        # the next init_db, same pattern as the last_seen_at ensure backfill.
        # Rows whose sources are missing or unparseable stay NULL.
        dated = self._save_message(
            "We finally visited Yellowstone.",
            timestamp="2026-03-05T10:00:00+00:00",
        )
        candidate_id = self._commit_candidate(
            _candidate("Ryan visited Yellowstone.", message_ids=[dated], category="event")
        )
        orphan_id = self._commit_candidate(
            _candidate("Ryan met a bear.", message_ids=[999], category="event"),
            embedding=_unit_vector(0.0, 1.0),
        )
        self.assertNotEqual(orphan_id, candidate_id)

        init_vector_memory(self.db_path)
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text="Ryan visited Yellowstone.",
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[dated],
                    agent_id=None,
                    promoted_from_candidate_id=candidate_id,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=_unit_vector(1.0),
                    occurred_at="2026-03-01",
                )

        # Simulate pre-column rows.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE memory_candidates SET mentioned_at = NULL")
            conn.execute("UPDATE long_term_memory SET mentioned_at = NULL")
            conn.commit()

        _reset_init_memo()
        init_db(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as conn:
            candidate_value = conn.execute(
                "SELECT mentioned_at FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()[0]
            orphan_value = conn.execute(
                "SELECT mentioned_at FROM memory_candidates WHERE id = ?",
                (orphan_id,),
            ).fetchone()[0]
            fact_value = conn.execute(
                "SELECT mentioned_at FROM long_term_memory WHERE id = ?",
                (fact_id,),
            ).fetchone()[0]
        self.assertEqual(candidate_value, "2026-03-05")
        self.assertIsNone(orphan_value)
        self.assertEqual(fact_value, "2026-03-05")

    def test_backfill_does_not_overwrite_a_value_written_after_its_snapshot(self) -> None:
        # codex audit F2: on hosted libSQL each statement auto-commits, so a
        # concurrent merge can land a (correct, union-derived) mentioned_at
        # between the backfill's NULL-row snapshot and its UPDATE. The UPDATE
        # must be conditional on the row still being NULL so the stale
        # snapshot value never overwrites the fresher merge write.
        late = self._save_message(
            "The Yellowstone trip was amazing.",
            timestamp="2026-04-10T09:30:00+00:00",
        )
        candidate_id = self._commit_candidate(
            _candidate("Ryan visited Yellowstone.", message_ids=[late], category="event")
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE memory_candidates SET mentioned_at = NULL WHERE id = ?",
                (candidate_id,),
            )
            conn.commit()

        import vexic.storage.schema as schema_module

        real_derive = schema_module._earliest_date_from_timestamps

        with closing(connect(self.db_path)) as conn:
            def concurrent_merge_lands(values: object) -> str | None:
                # Simulates the merge write landing between snapshot and
                # UPDATE: an earlier mention from the merged source union.
                conn.execute(
                    "UPDATE memory_candidates SET mentioned_at = '2026-03-05' WHERE id = ?",
                    (candidate_id,),
                )
                return real_derive(values)

            with patch.object(
                schema_module,
                "_earliest_date_from_timestamps",
                side_effect=concurrent_merge_lands,
            ):
                _backfill_mentioned_at(conn, "memory_candidates")
            conn.commit()

        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(
            mentioned_at,
            "2026-03-05",
            "backfill must not clobber a value written after its snapshot",
        )

    def test_merge_does_not_clobber_known_mentioned_at_when_recompute_yields_null(self) -> None:
        # After a physical purge the source ids can dangle; recompute then
        # yields NULL, which must not wipe provenance already known
        # (COALESCE mirror of the occurred_at no-clobber rule).
        first = self._save_message(
            "We finally visited Yellowstone.",
            timestamp="2026-03-05T10:00:00+00:00",
        )
        fact_text = "Ryan visited Yellowstone."
        candidate_id = self._commit_candidate(
            _candidate(fact_text, message_ids=[first], category="event")
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM messages WHERE id = ?", (first,))
            conn.commit()

        self._commit_candidate(_candidate(fact_text, message_ids=[first], category="event"))
        mentioned_at, _ = self._mentioned_at(candidate_id)
        self.assertEqual(mentioned_at, "2026-03-05")


class LongTermSearchAsOfFilterTests(unittest.TestCase):
    # `as_of` restricts keyword (FTS) and vector (KNN) Tier 3 search to facts
    # whose COALESCE(NULLIF(occurred_at, ''), NULLIF(mentioned_at, ''),
    # created_at) <= as_of (ADR 0037). Facts are seeded directly via
    # insert_long_term_fact rather than a full dream->deep promotion cycle --
    # cheaper, and keeps this search-filtering behavior decoupled from
    # promotion's separately-tested occurred_at carry-through
    # (OccurredAtRoundTripTests, above). Seeded facts cite unsaved message
    # ids and get no mentioned_at unless a test sets one, keeping the
    # created_at fallback exercised (load-bearing convention).

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        init_vector_memory(self.db_path)
        self._next_candidate_id = 1

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_fact(
        self,
        fact_text: str,
        *,
        occurred_at: str | None = None,
        mentioned_at: str | None = None,
        category: str = "event",
        embedding: list[float] | None = None,
    ) -> int:
        candidate_id = self._next_candidate_id
        self._next_candidate_id += 1
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text=fact_text,
                    subject="Ryan",
                    category=category,
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    agent_id=None,
                    promoted_from_candidate_id=candidate_id,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=embedding if embedding is not None else _unit_vector(1.0),
                    occurred_at=occurred_at,
                    mentioned_at=mentioned_at,
                )
        return fact_id

    def _set_created_at(self, fact_id: int, created_at: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE long_term_memory SET created_at = ? WHERE id = ?",
                (created_at, fact_id),
            )
            conn.commit()

    def test_as_of_ladder_prefers_occurred_then_mentioned_then_created(self) -> None:
        # ADR 0037 windowing ladder. The provenance row is deliberately a
        # non-event category: the ladder carries no category predicate, so any
        # row with resolvable mention time windows by it instead of created_at
        # (retroactive-dating semantic, stated in the ADR).
        provenance_id = self._insert_fact(
            "Ryan mentioned the mortgage plan.",
            mentioned_at="2025-01-01",
            category="preference",
        )
        self._set_created_at(provenance_id, "2027-01-01 00:00:00")
        dated_id = self._insert_fact(
            "Ryan refinanced the mortgage.",
            occurred_at="2025-06-01",
            mentioned_at="2025-01-01",
        )

        ids = keyword_long_term_fact_ids(self.db_path, "mortgage", k=10, as_of="2025-03-01")
        self.assertIn(provenance_id, ids, "mentioned_at must beat the created_at fallback")
        self.assertNotIn(dated_id, ids, "occurred_at must beat mentioned_at")

        neighbor_ids = {
            neighbor.fact_id
            for neighbor in nearest_long_term_facts(
                self.db_path, _unit_vector(1.0), k=10, as_of="2025-03-01"
            )
        }
        self.assertIn(provenance_id, neighbor_ids)
        self.assertNotIn(dated_id, neighbor_ids)

    def test_keyword_filters_by_as_of_via_occurred_at(self) -> None:
        earlier_id = self._insert_fact("Ryan started a new job.", occurred_at="2025-01-01")
        later_id = self._insert_fact("Ryan started a new project.", occurred_at="2025-06-01")

        ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, as_of="2025-03-01"
        )

        self.assertIn(earlier_id, ids)
        self.assertNotIn(later_id, ids)

    def test_keyword_as_of_none_stays_unfiltered(self) -> None:
        earlier_id = self._insert_fact("Ryan started a new job.", occurred_at="2025-01-01")
        later_id = self._insert_fact("Ryan started a new project.", occurred_at="2025-06-01")

        ids = keyword_long_term_fact_ids(self.db_path, "Ryan started", k=10, as_of=None)

        self.assertIn(earlier_id, ids)
        self.assertIn(later_id, ids)

    def test_keyword_falls_back_to_created_at_when_occurred_at_is_null(self) -> None:
        earlier_id = self._insert_fact("Ryan started a new job.", occurred_at=None)
        self._set_created_at(earlier_id, "2025-01-01 00:00:00")
        later_id = self._insert_fact("Ryan started a new project.", occurred_at=None)
        self._set_created_at(later_id, "2025-06-01 00:00:00")

        ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, as_of="2025-03-01"
        )

        self.assertIn(earlier_id, ids)
        self.assertNotIn(later_id, ids)

    def test_keyword_empty_string_occurred_at_also_falls_back_to_created_at(self) -> None:
        earlier_id = self._insert_fact("Ryan started a new job.", occurred_at="")
        self._set_created_at(earlier_id, "2025-01-01 00:00:00")
        later_id = self._insert_fact("Ryan started a new project.", occurred_at="")
        self._set_created_at(later_id, "2025-06-01 00:00:00")

        ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, as_of="2025-03-01"
        )

        self.assertIn(earlier_id, ids)
        self.assertNotIn(later_id, ids)

    def test_keyword_same_day_boundary_is_separator_sensitive(self) -> None:
        fact_id = self._insert_fact("Ryan started a new job.", occurred_at=None)
        self._set_created_at(fact_id, "2025-04-01 10:00:00")

        date_only_ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, as_of="2025-04-01"
        )
        t_separator_ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, as_of="2025-04-01T00:00:00"
        )

        self.assertNotIn(
            fact_id,
            date_only_ids,
            "date-only as_of is lexicographically less than a same-day 'created_at' timestamp",
        )
        self.assertIn(
            fact_id,
            t_separator_ids,
            "'T' separator sorts after the space in 'created_at', so it includes same-day rows",
        )

    def test_keyword_partial_precision_occurred_at_included_or_excluded_by_full_as_of(
        self,
    ) -> None:
        fact_id = self._insert_fact("Ryan started a new job.", occurred_at="2025")

        included_ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, as_of="2025-06-01"
        )
        excluded_ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, as_of="2024-12-31"
        )

        self.assertIn(
            fact_id,
            included_ids,
            "a partial occurred_at is <= any as_of at or after its period's start",
        )
        self.assertNotIn(fact_id, excluded_ids)

    def test_keyword_partial_as_of_excludes_more_specific_occurred_at(self) -> None:
        # "2025" reads as "before this period began", not "through it": a
        # more specific occurred_at like "2025-03" is lexicographically
        # greater than the bare "2025" cutoff, so it must be excluded.
        fact_id = self._insert_fact("Ryan started a new job.", occurred_at="2025-03")

        ids = keyword_long_term_fact_ids(self.db_path, "Ryan started", k=10, as_of="2025")

        self.assertNotIn(fact_id, ids)

    def test_nearest_filters_by_as_of(self) -> None:
        embedding = _unit_vector(1.0)
        earlier_id = self._insert_fact(
            "Ryan started a new job.", occurred_at="2025-01-01", embedding=embedding
        )
        later_id = self._insert_fact(
            "Ryan started a new project.", occurred_at="2025-06-01", embedding=embedding
        )

        neighbors = nearest_long_term_facts(
            self.db_path, embedding, k=10, as_of="2025-03-01"
        )

        neighbor_ids = [neighbor.fact_id for neighbor in neighbors]
        self.assertIn(earlier_id, neighbor_ids)
        self.assertNotIn(later_id, neighbor_ids)

    def test_nearest_falls_back_to_created_at_when_occurred_at_is_null(self) -> None:
        embedding = _unit_vector(1.0)
        earlier_id = self._insert_fact(
            "Ryan started a new job.", occurred_at=None, embedding=embedding
        )
        self._set_created_at(earlier_id, "2025-01-01 00:00:00")
        later_id = self._insert_fact(
            "Ryan started a new project.", occurred_at=None, embedding=embedding
        )
        self._set_created_at(later_id, "2025-06-01 00:00:00")

        neighbors = nearest_long_term_facts(
            self.db_path, embedding, k=10, as_of="2025-03-01"
        )

        neighbor_ids = [neighbor.fact_id for neighbor in neighbors]
        self.assertIn(earlier_id, neighbor_ids)
        self.assertNotIn(later_id, neighbor_ids)


class RetrieveLongTermFactsAsOfThreadThroughTests(unittest.IsolatedAsyncioTestCase):
    # retrieve_long_term_facts must thread `as_of` into both
    # keyword_long_term_fact_ids and nearest_long_term_facts. This is a
    # tracer bullet for the thread-through, not a re-test of the filtering
    # semantics themselves (already covered by LongTermSearchAsOfFilterTests
    # above) -- the fact excluded here would otherwise surface via both the
    # keyword and vector halves of the hybrid search if `as_of` were dropped
    # anywhere along the call chain.

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        init_vector_memory(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_fact(
        self,
        fact_text: str,
        *,
        candidate_id: int,
        occurred_at: str | None,
        embedding: list[float],
    ) -> int:
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text=fact_text,
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    agent_id=None,
                    promoted_from_candidate_id=candidate_id,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=embedding,
                    occurred_at=occurred_at,
                )
        return fact_id

    async def test_retrieve_long_term_facts_threads_as_of_to_storage_layer(self) -> None:
        embedding = _unit_vector(1.0)
        self._insert_fact(
            "Ryan started a new job.",
            candidate_id=1,
            occurred_at="2025-01-01",
            embedding=embedding,
        )
        self._insert_fact(
            "Ryan started a new project.",
            candidate_id=2,
            occurred_at="2025-06-01",
            embedding=embedding,
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [embedding for _ in texts],
        ):
            facts = await retrieve_long_term_facts(
                self.db_path,
                "Ryan started",
                as_of="2025-03-01",
            )

        fact_texts = [fact.fact_text for fact in facts]
        self.assertIn("Ryan started a new job.", fact_texts)
        self.assertNotIn("Ryan started a new project.", fact_texts)


class CandidateSearchAsOfFilterTests(unittest.TestCase):
    # `as_of` restricts keyword (FTS) and vector (KNN) Tier 2
    # candidate-fallback search to candidates whose
    # COALESCE(NULLIF(occurred_at, ''), NULLIF(mentioned_at, ''), created_at)
    # <= as_of. Candidates are seeded via commit_dream_cycle (the existing
    # dedup-aware insert path), not a private insert helper, since
    # candidates.py has no direct-insert equivalent to insert_long_term_fact.
    #
    # Load-bearing convention: seeded candidates cite message ids that were
    # never save_messages'd, so mentioned_at derives NULL and the created_at
    # fallback stays exercised. Saving those messages in a helper would flip
    # the windowing behavior suite-wide (ADR 0037).

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seeded_candidate_ids(self) -> list[int]:
        # Ids assigned in insertion order by commit_dream_cycle within a
        # single call; read back oldest-first so callers can zip against the
        # candidates list they passed in.
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT id FROM memory_candidates ORDER BY id ASC").fetchall()
        return [int(row[0]) for row in rows]

    def _set_created_at(self, candidate_id: int, created_at: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE memory_candidates SET created_at = ? WHERE id = ?",
                (created_at, candidate_id),
            )
            conn.commit()

    def _set_mentioned_at(self, candidate_id: int, mentioned_at: str) -> None:
        # Windowing tests set mentioned_at directly; derivation-from-messages
        # is pinned separately in MentionedAtDerivationTests.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE memory_candidates SET mentioned_at = ? WHERE id = ?",
                (mentioned_at, candidate_id),
            )
            conn.commit()

    def test_candidate_as_of_ladder_prefers_occurred_then_mentioned_then_created(self) -> None:
        # ADR 0037 windowing ladder on Tier 2; non-event categories window by
        # mentioned_at too (no category predicate in the ladder).
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan mentioned the mortgage plan.",
                    message_ids=[1],
                    category="preference",
                ),
                _candidate(
                    "Ryan refinanced the mortgage.",
                    message_ids=[2],
                    category="event",
                    occurred_at="2025-06-01",
                ),
            ],
            candidate_embeddings=[embedding, embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        provenance_id, dated_id = self._seeded_candidate_ids()
        self._set_mentioned_at(provenance_id, "2025-01-01")
        self._set_created_at(provenance_id, "2027-01-01 00:00:00")
        self._set_mentioned_at(dated_id, "2025-01-01")

        ids = keyword_candidate_ids(self.db_path, "mortgage", k=10, as_of="2025-03-01")
        self.assertIn(provenance_id, ids, "mentioned_at must beat the created_at fallback")
        self.assertNotIn(dated_id, ids, "occurred_at must beat mentioned_at")

        nearest = nearest_candidate_ids(
            self.db_path, _unit_vector(1.0), k=10, as_of="2025-03-01"
        )
        self.assertIn(provenance_id, nearest)
        self.assertNotIn(dated_id, nearest)

    def test_keyword_candidate_ids_filters_by_as_of_via_occurred_at(self) -> None:
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan adopted a rescue greyhound.",
                    message_ids=[1],
                    category="event",
                    occurred_at="2025-01-01",
                ),
                _candidate(
                    "Ryan adopted a rescue parrot.",
                    message_ids=[2],
                    category="fact",
                    occurred_at="2025-06-01",
                ),
            ],
            candidate_embeddings=[embedding, embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        earlier_id, later_id = self._seeded_candidate_ids()

        ids = keyword_candidate_ids(
            self.db_path, "Ryan adopted", k=10, as_of="2025-03-01"
        )

        self.assertIn(earlier_id, ids)
        self.assertNotIn(later_id, ids)

    def test_keyword_candidate_ids_falls_back_to_created_at_when_occurred_at_missing(
        self,
    ) -> None:
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan switched to a standing desk.",
                    message_ids=[1],
                    occurred_at="",
                )
            ],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        (candidate_id,) = self._seeded_candidate_ids()
        self._set_created_at(candidate_id, "2025-04-01 10:00:00")

        date_only_ids = keyword_candidate_ids(
            self.db_path, "standing desk", k=10, as_of="2025-04-01"
        )
        self.assertNotIn(
            candidate_id,
            date_only_ids,
            "date-only as_of should exclude a same-day created_at fallback row",
        )

        full_precision_ids = keyword_candidate_ids(
            self.db_path, "standing desk", k=10, as_of="2025-04-01T00:00:00"
        )
        self.assertIn(
            candidate_id,
            full_precision_ids,
            "a 'T'-separated as_of should include the same-day created_at fallback row",
        )

    def test_nearest_candidate_ids_filters_by_as_of(self) -> None:
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan learned to sail.",
                    message_ids=[1],
                    category="event",
                    occurred_at="2025-01-01",
                ),
                _candidate(
                    "Ryan learned to weld.",
                    message_ids=[2],
                    category="fact",
                    occurred_at="2025-06-01",
                ),
            ],
            candidate_embeddings=[embedding, embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        earlier_id, later_id = self._seeded_candidate_ids()

        ids = nearest_candidate_ids(
            self.db_path, embedding, k=10, as_of="2025-03-01"
        )

        self.assertIn(earlier_id, ids)
        self.assertNotIn(later_id, ids)

    def test_nearest_candidate_ids_falls_back_to_created_at_when_occurred_at_missing(
        self,
    ) -> None:
        embedding = _unit_vector(1.0)
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan tried freediving.",
                    message_ids=[1],
                    occurred_at=None,
                )
            ],
            candidate_embeddings=[embedding],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        (candidate_id,) = self._seeded_candidate_ids()
        self._set_created_at(candidate_id, "2025-04-01 10:00:00")

        date_only_ids = nearest_candidate_ids(
            self.db_path, embedding, k=10, as_of="2025-04-01"
        )
        self.assertNotIn(
            candidate_id,
            date_only_ids,
            "date-only as_of should exclude a same-day created_at fallback row",
        )

        full_precision_ids = nearest_candidate_ids(
            self.db_path, embedding, k=10, as_of="2025-04-01T00:00:00"
        )
        self.assertIn(
            candidate_id,
            full_precision_ids,
            "a 'T'-separated as_of should include the same-day created_at fallback row",
        )


class LongTermSearchEventRangeFilterTests(unittest.TestCase):
    # `event_after`/`event_before` bound Tier 3 keyword (FTS) and vector (KNN)
    # search to facts whose COALESCE(NULLIF(occurred_at, ''),
    # NULLIF(mentioned_at, ''), created_at) is >= event_after and/or
    # <= event_before -- the same ladder the `as_of` upper bound uses
    # (ADR 0037). Bounds are independent (either/both/neither) and
    # `event_before` may coexist with `as_of`.

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        init_vector_memory(self.db_path)
        self._next_candidate_id = 1

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_fact(
        self,
        fact_text: str,
        *,
        occurred_at: str | None = None,
        mentioned_at: str | None = None,
        embedding: list[float] | None = None,
    ) -> int:
        candidate_id = self._next_candidate_id
        self._next_candidate_id += 1
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text=fact_text,
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    agent_id=None,
                    promoted_from_candidate_id=candidate_id,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=embedding if embedding is not None else _unit_vector(1.0),
                    occurred_at=occurred_at,
                    mentioned_at=mentioned_at,
                )
        return fact_id

    def _set_created_at(self, fact_id: int, created_at: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE long_term_memory SET created_at = ? WHERE id = ?",
                (created_at, fact_id),
            )
            conn.commit()

    def test_event_range_windows_on_mentioned_at_fallback(self) -> None:
        # ADR 0037: mentioned_at slots between occurred_at and created_at in
        # the event-range ladder too.
        fact_id = self._insert_fact("Ryan hiked a volcano.", mentioned_at="2025-03-01")
        self._set_created_at(fact_id, "2027-01-01 00:00:00")

        inside = keyword_long_term_fact_ids(
            self.db_path,
            "volcano",
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )
        self.assertIn(fact_id, inside)

        outside = keyword_long_term_fact_ids(
            self.db_path, "volcano", k=10, event_after="2025-04-01"
        )
        self.assertNotIn(fact_id, outside)

    def test_keyword_event_range_keeps_only_in_window_facts(self) -> None:
        before_id = self._insert_fact("Ryan started early.", occurred_at="2025-01-01")
        inside_id = self._insert_fact("Ryan started mid.", occurred_at="2025-03-01")
        after_id = self._insert_fact("Ryan started late.", occurred_at="2025-06-01")

        ids = keyword_long_term_fact_ids(
            self.db_path,
            "Ryan started",
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        self.assertIn(inside_id, ids)
        self.assertNotIn(before_id, ids)
        self.assertNotIn(after_id, ids)

    def test_keyword_event_after_only_is_open_ended_lower_bound(self) -> None:
        before_id = self._insert_fact("Ryan started early.", occurred_at="2025-01-01")
        after_id = self._insert_fact("Ryan started late.", occurred_at="2025-06-01")

        ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, event_after="2025-03-01"
        )

        self.assertIn(after_id, ids)
        self.assertNotIn(before_id, ids)

    def test_keyword_event_before_only_is_open_ended_upper_bound(self) -> None:
        before_id = self._insert_fact("Ryan started early.", occurred_at="2025-01-01")
        after_id = self._insert_fact("Ryan started late.", occurred_at="2025-06-01")

        ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, event_before="2025-03-01"
        )

        self.assertIn(before_id, ids)
        self.assertNotIn(after_id, ids)

    def test_keyword_event_range_falls_back_to_created_at_when_occurred_at_missing(
        self,
    ) -> None:
        inside_id = self._insert_fact("Ryan started mid.", occurred_at=None)
        self._set_created_at(inside_id, "2025-03-01 00:00:00")
        outside_id = self._insert_fact("Ryan started late.", occurred_at="")
        self._set_created_at(outside_id, "2025-06-01 00:00:00")

        ids = keyword_long_term_fact_ids(
            self.db_path,
            "Ryan started",
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        self.assertIn(inside_id, ids)
        self.assertNotIn(outside_id, ids)

    def test_keyword_event_before_coexists_with_as_of(self) -> None:
        kept_id = self._insert_fact("Ryan started mid.", occurred_at="2025-03-01")
        after_event_before_id = self._insert_fact(
            "Ryan started late.", occurred_at="2025-06-01"
        )

        ids = keyword_long_term_fact_ids(
            self.db_path,
            "Ryan started",
            k=10,
            as_of="2025-12-01",
            event_before="2025-04-01",
        )

        self.assertIn(kept_id, ids)
        self.assertNotIn(after_event_before_id, ids)

    def test_keyword_event_bounds_are_inclusive_at_the_boundary(self) -> None:
        # Guards the >= / <= semantics: a fact whose occurred_at is exactly
        # equal to either bound must be kept. A regression to > / < would drop
        # it and only this equality case would catch it.
        on_bound_id = self._insert_fact("Ryan started mid.", occurred_at="2025-03-01")

        after_ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, event_after="2025-03-01"
        )
        before_ids = keyword_long_term_fact_ids(
            self.db_path, "Ryan started", k=10, event_before="2025-03-01"
        )

        self.assertIn(on_bound_id, after_ids)
        self.assertIn(on_bound_id, before_ids)

    def test_nearest_event_range_keeps_only_in_window_facts(self) -> None:
        embedding = _unit_vector(1.0)
        before_id = self._insert_fact(
            "Ryan started early.", occurred_at="2025-01-01", embedding=embedding
        )
        inside_id = self._insert_fact(
            "Ryan started mid.", occurred_at="2025-03-01", embedding=embedding
        )
        after_id = self._insert_fact(
            "Ryan started late.", occurred_at="2025-06-01", embedding=embedding
        )

        neighbors = nearest_long_term_facts(
            self.db_path,
            embedding,
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        neighbor_ids = [neighbor.fact_id for neighbor in neighbors]
        self.assertIn(inside_id, neighbor_ids)
        self.assertNotIn(before_id, neighbor_ids)
        self.assertNotIn(after_id, neighbor_ids)

    def test_nearest_event_after_only_is_open_ended_lower_bound(self) -> None:
        embedding = _unit_vector(1.0)
        before_id = self._insert_fact(
            "Ryan started early.", occurred_at="2025-01-01", embedding=embedding
        )
        after_id = self._insert_fact(
            "Ryan started late.", occurred_at="2025-06-01", embedding=embedding
        )

        neighbors = nearest_long_term_facts(
            self.db_path, embedding, k=10, event_after="2025-03-01"
        )

        neighbor_ids = [neighbor.fact_id for neighbor in neighbors]
        self.assertIn(after_id, neighbor_ids)
        self.assertNotIn(before_id, neighbor_ids)

    def test_nearest_event_before_only_is_open_ended_upper_bound(self) -> None:
        embedding = _unit_vector(1.0)
        before_id = self._insert_fact(
            "Ryan started early.", occurred_at="2025-01-01", embedding=embedding
        )
        after_id = self._insert_fact(
            "Ryan started late.", occurred_at="2025-06-01", embedding=embedding
        )

        neighbors = nearest_long_term_facts(
            self.db_path, embedding, k=10, event_before="2025-03-01"
        )

        neighbor_ids = [neighbor.fact_id for neighbor in neighbors]
        self.assertIn(before_id, neighbor_ids)
        self.assertNotIn(after_id, neighbor_ids)

    def test_nearest_event_range_falls_back_to_created_at_when_occurred_at_missing(
        self,
    ) -> None:
        embedding = _unit_vector(1.0)
        inside_id = self._insert_fact(
            "Ryan started mid.", occurred_at=None, embedding=embedding
        )
        self._set_created_at(inside_id, "2025-03-01 00:00:00")
        outside_id = self._insert_fact(
            "Ryan started late.", occurred_at="", embedding=embedding
        )
        self._set_created_at(outside_id, "2025-06-01 00:00:00")

        neighbors = nearest_long_term_facts(
            self.db_path,
            embedding,
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        neighbor_ids = [neighbor.fact_id for neighbor in neighbors]
        self.assertIn(inside_id, neighbor_ids)
        self.assertNotIn(outside_id, neighbor_ids)


class RetrieveLongTermFactsEventRangeThreadThroughTests(
    unittest.IsolatedAsyncioTestCase
):
    # retrieve_long_term_facts must thread event_after/event_before into both
    # the keyword and vector halves of the hybrid search -- a tracer bullet for
    # the thread-through, not a re-test of the filtering semantics.

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        init_vector_memory(self.db_path)
        self._next_candidate_id = 1

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_fact(
        self, fact_text: str, *, occurred_at: str, embedding: list[float]
    ) -> int:
        candidate_id = self._next_candidate_id
        self._next_candidate_id += 1
        with closing(connect(self.db_path)) as conn:
            with conn:
                _ensure_vector_memory_schema(conn)
                fact_id = insert_long_term_fact(
                    conn,
                    fact_text=fact_text,
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    agent_id=None,
                    promoted_from_candidate_id=candidate_id,
                    retrieved_count=0,
                    used_count=0,
                    editable=True,
                    embedding=embedding,
                    occurred_at=occurred_at,
                )
        return fact_id

    async def test_retrieve_long_term_facts_threads_event_bounds(self) -> None:
        embedding = _unit_vector(1.0)
        self._insert_fact(
            "Ryan started early.", occurred_at="2025-01-01", embedding=embedding
        )
        self._insert_fact(
            "Ryan started mid.", occurred_at="2025-03-01", embedding=embedding
        )
        self._insert_fact(
            "Ryan started late.", occurred_at="2025-06-01", embedding=embedding
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [embedding for _ in texts],
        ):
            facts = await retrieve_long_term_facts(
                self.db_path,
                "Ryan started",
                event_after="2025-02-01",
                event_before="2025-04-01",
            )

        fact_texts = [fact.fact_text for fact in facts]
        self.assertIn("Ryan started mid.", fact_texts)
        self.assertNotIn("Ryan started early.", fact_texts)
        self.assertNotIn("Ryan started late.", fact_texts)


class CandidateSearchEventRangeFilterTests(unittest.TestCase):
    # event_after/event_before bound Tier 2 candidate-fallback keyword (FTS)
    # and vector (KNN) search to candidates whose
    # COALESCE(NULLIF(occurred_at, ''), NULLIF(mentioned_at, ''), created_at)
    # is >= event_after and/or <= event_before, mirroring the Tier 3
    # event-range filter. Seeded candidates cite unsaved message ids so
    # mentioned_at derives NULL and the created_at fallback stays exercised
    # (load-bearing convention, see CandidateSearchAsOfFilterTests).

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seeded_candidate_ids(self) -> list[int]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id FROM memory_candidates ORDER BY id ASC"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def _set_mentioned_at(self, candidate_id: int, mentioned_at: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE memory_candidates SET mentioned_at = ? WHERE id = ?",
                (mentioned_at, candidate_id),
            )
            conn.commit()

    def test_candidate_event_range_windows_on_mentioned_at_fallback(self) -> None:
        (candidate_id,) = self._seed([("Ryan hiked a volcano.", None)])
        self._set_mentioned_at(candidate_id, "2025-03-01")
        self._set_created_at(candidate_id, "2027-01-01 00:00:00")

        inside = keyword_candidate_ids(
            self.db_path,
            "volcano",
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )
        self.assertIn(candidate_id, inside)

        outside = keyword_candidate_ids(
            self.db_path, "volcano", k=10, event_after="2025-04-01"
        )
        self.assertNotIn(candidate_id, outside)

    def _set_created_at(self, candidate_id: int, created_at: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE memory_candidates SET created_at = ? WHERE id = ?",
                (created_at, candidate_id),
            )
            conn.commit()

    def _seed(self, texts_and_dates: list[tuple[str, str | None]]) -> list[int]:
        # Distinct category per candidate so the subject+category dedup in
        # commit_dream_cycle keeps them separate (same trick the as_of
        # candidate tests use), letting each survive with its own occurred_at.
        embedding = _unit_vector(1.0)
        categories = ["event", "fact", "goal", "skill", "context"]
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    text,
                    message_ids=[index + 1],
                    category=categories[index],
                    occurred_at=occurred_at,
                )
                for index, (text, occurred_at) in enumerate(texts_and_dates)
            ],
            candidate_embeddings=[embedding for _ in texts_and_dates],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )
        return self._seeded_candidate_ids()

    def test_keyword_candidate_event_range_keeps_only_in_window(self) -> None:
        before_id, inside_id, after_id = self._seed(
            [
                ("Ryan adopted a greyhound.", "2025-01-01"),
                ("Ryan adopted a parrot.", "2025-03-01"),
                ("Ryan adopted a cat.", "2025-06-01"),
            ]
        )

        ids = keyword_candidate_ids(
            self.db_path,
            "Ryan adopted",
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        self.assertIn(inside_id, ids)
        self.assertNotIn(before_id, ids)
        self.assertNotIn(after_id, ids)

    def test_keyword_candidate_event_after_only_open_ended(self) -> None:
        before_id, after_id = self._seed(
            [
                ("Ryan adopted a greyhound.", "2025-01-01"),
                ("Ryan adopted a cat.", "2025-06-01"),
            ]
        )

        ids = keyword_candidate_ids(
            self.db_path, "Ryan adopted", k=10, event_after="2025-03-01"
        )

        self.assertIn(after_id, ids)
        self.assertNotIn(before_id, ids)

    def test_keyword_candidate_event_before_only_open_ended(self) -> None:
        before_id, after_id = self._seed(
            [
                ("Ryan adopted a greyhound.", "2025-01-01"),
                ("Ryan adopted a cat.", "2025-06-01"),
            ]
        )

        ids = keyword_candidate_ids(
            self.db_path, "Ryan adopted", k=10, event_before="2025-03-01"
        )

        self.assertIn(before_id, ids)
        self.assertNotIn(after_id, ids)

    def test_keyword_candidate_event_range_falls_back_to_created_at(self) -> None:
        (candidate_id,) = self._seed([("Ryan adopted a greyhound.", "")])
        self._set_created_at(candidate_id, "2025-06-01 00:00:00")

        ids = keyword_candidate_ids(
            self.db_path,
            "Ryan adopted",
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        self.assertNotIn(candidate_id, ids)

    def test_keyword_candidate_event_before_coexists_with_as_of(self) -> None:
        kept_id, dropped_id = self._seed(
            [
                ("Ryan adopted a parrot.", "2025-03-01"),
                ("Ryan adopted a cat.", "2025-06-01"),
            ]
        )

        ids = keyword_candidate_ids(
            self.db_path,
            "Ryan adopted",
            k=10,
            as_of="2025-12-01",
            event_before="2025-04-01",
        )

        self.assertIn(kept_id, ids)
        self.assertNotIn(dropped_id, ids)

    def test_nearest_candidate_event_range_keeps_only_in_window(self) -> None:
        embedding = _unit_vector(1.0)
        before_id, inside_id, after_id = self._seed(
            [
                ("Ryan learned to sail.", "2025-01-01"),
                ("Ryan learned to weld.", "2025-03-01"),
                ("Ryan learned to ski.", "2025-06-01"),
            ]
        )

        ids = nearest_candidate_ids(
            self.db_path,
            embedding,
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        self.assertIn(inside_id, ids)
        self.assertNotIn(before_id, ids)
        self.assertNotIn(after_id, ids)

    def test_nearest_candidate_event_after_only_open_ended(self) -> None:
        embedding = _unit_vector(1.0)
        before_id, after_id = self._seed(
            [
                ("Ryan learned to sail.", "2025-01-01"),
                ("Ryan learned to ski.", "2025-06-01"),
            ]
        )

        ids = nearest_candidate_ids(
            self.db_path, embedding, k=10, event_after="2025-03-01"
        )

        self.assertIn(after_id, ids)
        self.assertNotIn(before_id, ids)

    def test_nearest_candidate_event_range_falls_back_to_created_at(self) -> None:
        embedding = _unit_vector(1.0)
        (candidate_id,) = self._seed([("Ryan tried freediving.", None)])
        self._set_created_at(candidate_id, "2025-06-01 00:00:00")

        ids = nearest_candidate_ids(
            self.db_path,
            embedding,
            k=10,
            event_after="2025-02-01",
            event_before="2025-04-01",
        )

        self.assertNotIn(candidate_id, ids)


class UndatedEventPromotionSelectionTests(unittest.TestCase):
    # Invariant 11 makes promotion refuse category="event" candidates without
    # occurred_at, but extraction legitimately emits them ("leave occurred_at
    # null when no temporal reference exists"). Selection must not hand them to
    # promotion: one undated event in the top-N aborts the whole Deep cycle and
    # deadlocks dreaming for the scope.

    def _promotion_candidate(
        self,
        candidate_id: int,
        *,
        category: str = "fact",
        occurred_at: str | None = None,
        mentioned_at: str | None = None,
    ) -> PromotionCandidate:
        return PromotionCandidate(
            candidate_id=candidate_id,
            fact_text=f"fact {candidate_id}",
            subject="Ryan",
            category=category,
            confidence=0.9,
            importance=8,
            hit_count=3,
            last_seen_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            rem_boost=0.0,
            embedding=_unit_vector(1.0),
            occurred_at=occurred_at,
            mentioned_at=mentioned_at,
        )

    def test_select_promotions_skips_event_candidates_without_occurred_at(self) -> None:
        candidates = [
            self._promotion_candidate(1, category="event", occurred_at=None),
            self._promotion_candidate(2, category="event", occurred_at=""),
            self._promotion_candidate(3, category="event", occurred_at="2026-03"),
            self._promotion_candidate(4, category="fact"),
            self._promotion_candidate(5, category="event", occurred_at="   "),
        ]

        selected = select_promotions(
            candidates,
            now=datetime(2026, 6, 2, tzinfo=timezone.utc),
            top_n=10,
        )

        self.assertEqual({c.candidate_id for c in selected}, {3, 4})

    def test_undated_event_candidates_do_not_consume_top_n_slots(self) -> None:
        # The filter must run before scoring/slicing: an undated event that
        # would have out-scored everyone cannot eat the only top-N slot.
        undated = self._promotion_candidate(1, category="event", occurred_at=None)
        undated = PromotionCandidate(
            **{**undated.__dict__, "hit_count": 1000, "importance": 10}
        )
        fact = self._promotion_candidate(2, category="fact")

        selected = select_promotions(
            [undated, fact],
            now=datetime(2026, 6, 2, tzinfo=timezone.utc),
            top_n=1,
        )

        self.assertEqual([c.candidate_id for c in selected], [2])

    def test_all_undated_event_candidates_select_nothing(self) -> None:
        # The literal deadlock shape: every eligible candidate is an undated
        # event. Selection must return empty (Deep no-ops) instead of handing
        # promotion a candidate it will refuse.
        candidates = [
            self._promotion_candidate(1, category="event", occurred_at=None),
            self._promotion_candidate(2, category="event", occurred_at=""),
        ]

        selected = select_promotions(
            candidates,
            now=datetime(2026, 6, 2, tzinfo=timezone.utc),
            top_n=10,
        )

        self.assertEqual(selected, [])

    def test_select_promotions_keeps_event_candidate_with_mentioned_at_only(self) -> None:
        # ADR 0037: mentioned_at is a promotable date for events, so an
        # undated event with resolvable mention time is no longer sunk in
        # Tier 2. Blank-ish mentioned_at values stay skipped, same .strip()
        # semantics as occurred_at.
        candidates = [
            self._promotion_candidate(1, category="event", mentioned_at="2026-03-05"),
            self._promotion_candidate(2, category="event", mentioned_at=""),
            self._promotion_candidate(3, category="event", mentioned_at="   "),
            self._promotion_candidate(4, category="event"),
        ]

        selected = select_promotions(
            candidates,
            now=datetime(2026, 6, 2, tzinfo=timezone.utc),
            top_n=10,
        )

        self.assertEqual({c.candidate_id for c in selected}, {1})


class UndatedEventDeepCycleReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_deep_cycle_survives_undated_event_candidate_and_promotes_the_rest(
        self,
    ) -> None:
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan attended a conference.",
                    message_ids=[1],
                    category="event",
                ),
                _candidate("Ryan uses uv.", message_ids=[2]),
            ],
            candidate_embeddings=[_unit_vector(1.0), _unit_vector(0.0, 1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=2,
            last_processed_message_id=2,
        )

        await run_deep_phase(
            self.db_path,
            "glm",
            contradiction_agent_factory=lambda *_args, **_kwargs: _ContradictionAgent(
                contradicts=False
            ),
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            facts = conn.execute(
                "SELECT fact_text FROM long_term_memory WHERE retired = 0"
            ).fetchall()
            event_row = conn.execute(
                """
                SELECT promoted, retired, stale, needs_review
                FROM memory_candidates
                WHERE category = 'event'
                """
            ).fetchone()

        self.assertEqual(facts, [("Ryan uses uv.",)])
        self.assertEqual(
            event_row,
            (0, 0, 0, 0),
            "undated event candidate must stay eligible Tier 2 staging, not "
            "crash the cycle or get retired",
        )

    async def test_deep_cycle_promotes_undated_event_with_resolvable_mention_time(
        self,
    ) -> None:
        # ADR 0037 end-to-end: same undated-event shape, but the source
        # message actually exists with a timestamp, so mentioned_at resolves
        # at insert and the event escapes the Tier 2 sink through a full
        # run_deep_phase cycle.
        message_id = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="I went to a conference.")])],
            timestamp="2026-03-05T10:00:00+00:00",
        )[0]
        commit_dream_cycle(
            self.db_path,
            [
                _candidate(
                    "Ryan attended a conference.",
                    message_ids=[message_id],
                    category="event",
                )
            ],
            candidate_embeddings=[_unit_vector(1.0)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=message_id,
        )

        await run_deep_phase(
            self.db_path,
            "glm",
            contradiction_agent_factory=lambda *_args, **_kwargs: _ContradictionAgent(
                contradicts=False
            ),
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT occurred_at, mentioned_at FROM long_term_memory
                WHERE retired = 0 AND category = 'event'
                """
            ).fetchone()
        self.assertIsNotNone(row, "undated event with mention time must reach Tier 3")
        self.assertIsNone(row[0], "occurred_at must stay NULL — never fabricated")
        self.assertEqual(row[1], "2026-03-05")


if __name__ == "__main__":
    unittest.main()
