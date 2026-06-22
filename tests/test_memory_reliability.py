import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.deep import run_deep_phase
from vexic.embeddings import EMBEDDING_DIM
from vexic.models import ContradictionJudgment, FactCandidate, RemBoost, RemBoostPlan
from vexic.pipeline import run_light_phase
from vexic.rem import run_rem_phase
from vexic.storage import (
    commit_deep_cycle,
    commit_dream_cycle,
    init_db,
    record_candidate_retrieval,
    save_messages,
)
from vexic.storage.promotion import PromotionDecision
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
) -> FactCandidate:
    return FactCandidate(
        fact_text=fact_text,
        subject="Ryan",
        category=category,
        importance=6,
        confidence=confidence,
        source_message_ids=message_ids,
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


class _NoOpRemAgent:
    async def run(self, prompt: str) -> _FakeResult:
        return _FakeResult(RemBoostPlan(boosts=[RemBoost(candidate_id=1, boost=0.25)]))


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
        readme = (REPO_ROOT / "README.md").read_text()

        self.assertIn("## Running the Project", readme)
        self.assertIn("<!-- memory-reliability-gate -->", readme)
        self.assertIn("<!-- memory-reliability-live-smoke -->", readme)


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
            patch("vexic.rem.build_rem_agent", return_value=_NoOpRemAgent()),
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
            await run_rem_phase(self.db_path, "glm")
            await run_deep_phase(self.db_path, "glm")
            await run_light_phase(
                self.db_path,
                "glm",
                embed=lambda texts: [_unit_vector(1.0) for _ in texts],
            )
            await run_rem_phase(self.db_path, "glm")
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

        with patch(
            "vexic.deep.build_contradiction_agent",
            return_value=_ContradictionAgent(contradicts=True),
        ):
            await run_deep_phase(self.db_path, "glm")

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
            patch("vexic.rem.build_rem_agent", return_value=_NoOpRemAgent()),
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
            await run_rem_phase(self.db_path, "glm")
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


if __name__ == "__main__":
    unittest.main()
