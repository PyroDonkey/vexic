from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.embeddings import EMBEDDING_DIM
from vexic.deep import run_deep_phase
from vexic.models import ContradictionJudgment, FactCandidate, RemBoost, RemBoostPlan
from vexic.pipeline import _main, run_light_phase
from vexic.rem import run_rem_phase
from vexic.storage import (
    CandidateRetirementDecision,
    PromotionDecision,
    commit_deep_cycle,
    commit_dream_cycle,
    init_db,
    save_messages,
)
from vexic.storage.schema import _load_vec_extension


def _unit_vector(first: float) -> list[float]:
    return [first] + [0.0] * (EMBEDDING_DIM - 1)


class PipelineEmbeddingPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_light_phase_keeps_agent_scoped_windows_and_watermarks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            agent_a_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="cedar agent a")])],
                agent_id="agent-a",
            )[0]
            agent_b_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="cedar agent b")])],
                agent_id="agent-b",
            )[0]
            transcripts: list[str] = []

            class ExtractionAgent:
                async def run(self, transcript: str) -> object:
                    transcripts.append(transcript)
                    message_id = int(re.search(r"message_id=(\d+)", transcript).group(1))
                    return SimpleNamespace(
                        output=[
                            FactCandidate(
                                fact_text=f"Fact from message {message_id}.",
                                subject="Ryan",
                                category="fact",
                                importance=5,
                                confidence=0.8,
                                source_message_ids=[message_id],
                            )
                        ],
                        usage=lambda: SimpleNamespace(
                            requests=1,
                            input_tokens=1,
                            output_tokens=1,
                            total_tokens=2,
                        ),
                    )

            def agent_factory(model_group: str, secrets: object = None) -> object:
                return ExtractionAgent()

            def embed(texts: list[str]) -> list[list[float]]:
                return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]

            await run_light_phase(
                db_path,
                "glm",
                agent_id="agent-a",
                extraction_agent_factory=agent_factory,
                embed=embed,
            )
            await run_light_phase(
                db_path,
                "glm",
                agent_id="agent-b",
                extraction_agent_factory=agent_factory,
                embed=embed,
            )

            with closing(sqlite3.connect(db_path)) as conn:
                runs = conn.execute(
                    """
                    SELECT agent_id, messages_processed, last_processed_message_id
                    FROM dream_runs
                    WHERE status = 'ok'
                    ORDER BY id
                    """
                ).fetchall()

        self.assertEqual(len(transcripts), 2)
        self.assertIn(f"message_id={agent_a_id}", transcripts[0])
        self.assertNotIn(f"message_id={agent_b_id}", transcripts[0])
        self.assertIn(f"message_id={agent_b_id}", transcripts[1])
        self.assertNotIn(f"message_id={agent_a_id}", transcripts[1])
        self.assertEqual(
            runs,
            [
                ("agent-a", 1, agent_a_id),
                ("agent-b", 1, agent_b_id),
            ],
        )

    async def test_light_phase_uses_default_embedding_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )
            agent_factory_called = False
            embedded_texts: list[str] = []

            class ExtractionAgent:
                async def run(self, transcript: str) -> object:
                    return SimpleNamespace(
                        output=[
                            FactCandidate(
                                fact_text="Ryan prefers compact reports.",
                                subject="Ryan",
                                category="preference",
                                importance=7,
                                confidence=0.9,
                                source_message_ids=[1],
                            )
                        ]
                    )

            def agent_factory(model_group: str, secrets: object = None) -> object:
                nonlocal agent_factory_called
                agent_factory_called = True
                return ExtractionAgent()

            def default_embed(texts: list[str]) -> list[list[float]]:
                embedded_texts.extend(texts)
                return [_unit_vector(1.0) for _ in texts]

            with patch("vexic.pipeline.embed_texts", side_effect=default_embed, create=True):
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=agent_factory,
                )

            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                embedded_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidate_embeddings"
                ).fetchone()[0]

            self.assertTrue(agent_factory_called)
            self.assertEqual(embedded_texts, ["Ryan prefers compact reports."])
            self.assertEqual(embedded_count, 1)

    async def test_light_phase_redaction_failure_does_not_call_embedder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )
            embed_calls = 0

            class ExtractionAgent:
                async def run(self, transcript: str) -> object:
                    return SimpleNamespace(
                        output=[
                            FactCandidate(
                                fact_text="Ryan stores cedar-secret outside embeddings.",
                                subject="Ryan",
                                category="constraint",
                                importance=8,
                                confidence=0.9,
                                source_message_ids=[1],
                            )
                        ]
                    )

            def embed(texts: list[str]) -> list[list[float]]:
                nonlocal embed_calls
                embed_calls += 1
                return [_unit_vector(1.0) for _ in texts]

            with self.assertRaises(ValueError):
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=lambda *_args, **_kwargs: ExtractionAgent(),
                    embed=embed,
                    forbidden_secret_values=("cedar-secret",),
                )

        self.assertEqual(embed_calls, 0)

    async def test_light_phase_repairs_only_requested_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for agent_id, fact_text in (
                ("agent-a", "agent a cedar candidate"),
                ("agent-b", "agent b cedar candidate"),
                (None, "shared cedar candidate"),
            ):
                commit_dream_cycle(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=fact_text,
                            subject="Ryan",
                            category="fact",
                            importance=5,
                            confidence=0.8,
                            source_message_ids=[1],
                        )
                    ],
                    candidate_embeddings=[_unit_vector(1.0)],
                    agent_id=agent_id,
                    status="ok",
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    messages_processed=0,
                    last_processed_message_id=0,
                )
            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                conn.execute("DELETE FROM memory_candidate_embeddings")
                conn.commit()

            def default_embed(texts: list[str]) -> list[list[float]]:
                return [_unit_vector(1.0) for _ in texts]

            def repaired_state() -> list[tuple[str | None, int]]:
                with closing(sqlite3.connect(db_path)) as conn:
                    _load_vec_extension(conn)
                    return conn.execute(
                        """
                        SELECT c.agent_id, e.candidate_id IS NOT NULL
                        FROM memory_candidates AS c
                        LEFT JOIN memory_candidate_embeddings AS e
                            ON e.candidate_id = c.id
                        ORDER BY c.id
                        """
                    ).fetchall()

            with patch("vexic.pipeline.embed_texts", side_effect=default_embed, create=True):
                await run_light_phase(db_path, "glm", agent_id="agent-a")

            self.assertEqual(
                repaired_state(),
                [("agent-a", 1), ("agent-b", 0), (None, 0)],
            )

            with patch("vexic.pipeline.embed_texts", side_effect=default_embed, create=True):
                await run_light_phase(db_path, "glm", agent_id=None)

            self.assertEqual(
                repaired_state(),
                [("agent-a", 1), ("agent-b", 0), (None, 1)],
            )

    async def test_rem_phase_boosts_only_requested_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for agent_id, fact_text in (
                ("agent-a", "Ryan agent a cedar candidate."),
                ("agent-b", "Ryan agent b cedar candidate."),
                (None, "Ryan shared cedar candidate."),
            ):
                commit_dream_cycle(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=fact_text,
                            subject="Ryan",
                            category="fact",
                            importance=5,
                            confidence=0.8,
                            source_message_ids=[1],
                        )
                    ],
                    candidate_embeddings=[_unit_vector(1.0)],
                    agent_id=agent_id,
                    status="ok",
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    messages_processed=0,
                    last_processed_message_id=0,
                )
            prompts: list[str] = []

            class RemAgent:
                async def run(self, prompt: str) -> object:
                    prompts.append(prompt)
                    candidate_ids = [
                        int(candidate_id)
                        for candidate_id in re.findall(r"candidate_id=(\d+)", prompt)
                    ]
                    if not candidate_ids:
                        raise AssertionError("REM prompt did not include scoped candidates.")
                    return SimpleNamespace(
                        output=RemBoostPlan(
                            boosts=[
                                RemBoost(candidate_id=candidate_id, boost=0.5)
                                for candidate_id in candidate_ids
                            ]
                        ),
                        usage=lambda: SimpleNamespace(
                            requests=1,
                            input_tokens=1,
                            output_tokens=1,
                            total_tokens=2,
                        ),
                    )

            def rem_state() -> tuple[list[tuple[str | None, str, float]], list[tuple[str | None, int]]]:
                with closing(sqlite3.connect(db_path)) as conn:
                    candidates = conn.execute(
                        """
                        SELECT agent_id, fact_text, rem_boost
                        FROM memory_candidates
                        ORDER BY id
                        """
                    ).fetchall()
                    rem_runs = conn.execute(
                        """
                        SELECT agent_id, candidates_boosted
                        FROM dream_runs
                        WHERE candidates_boosted > 0
                        ORDER BY id
                        """
                    ).fetchall()
                return candidates, rem_runs

            await run_rem_phase(
                db_path,
                "glm",
                agent_id="agent-a",
                rem_agent_factory=lambda *_args, **_kwargs: RemAgent(),
            )
            after_agent_a = rem_state()

            await run_rem_phase(
                db_path,
                "glm",
                agent_id=None,
                rem_agent_factory=lambda *_args, **_kwargs: RemAgent(),
            )
            after_shared = rem_state()

        self.assertEqual(len(prompts), 2)
        self.assertIn("Ryan agent a cedar candidate.", prompts[0])
        self.assertNotIn("Ryan agent b cedar candidate.", prompts[0])
        self.assertNotIn("Ryan shared cedar candidate.", prompts[0])
        self.assertIn("Ryan shared cedar candidate.", prompts[1])
        self.assertNotIn("Ryan agent a cedar candidate.", prompts[1])
        self.assertNotIn("Ryan agent b cedar candidate.", prompts[1])
        self.assertEqual(
            after_agent_a,
            (
                [
                    ("agent-a", "Ryan agent a cedar candidate.", 0.5),
                    ("agent-b", "Ryan agent b cedar candidate.", 0.0),
                    (None, "Ryan shared cedar candidate.", 0.0),
                ],
                [("agent-a", 1)],
            ),
        )
        self.assertEqual(
            after_shared,
            (
                [
                    ("agent-a", "Ryan agent a cedar candidate.", 0.5),
                    ("agent-b", "Ryan agent b cedar candidate.", 0.0),
                    (None, "Ryan shared cedar candidate.", 0.5),
                ],
                [("agent-a", 1), (None, 1)],
            ),
        )

    async def test_rem_phase_redaction_failure_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan keeps cedar-secret out of model prompts.",
                        subject="Ryan",
                        category="constraint",
                        importance=8,
                        confidence=0.9,
                        source_message_ids=[1],
                    )
                ],
                candidate_embeddings=[_unit_vector(1.0)],
                agent_id=None,
                status="ok",
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:01Z",
                messages_processed=1,
                last_processed_message_id=1,
            )
            factory_calls = 0

            def rem_agent_factory(*_args: object, **_kwargs: object) -> object:
                nonlocal factory_calls
                factory_calls += 1
                return SimpleNamespace()

            with self.assertRaises(ValueError):
                await run_rem_phase(
                    db_path,
                    "glm",
                    rem_agent_factory=rem_agent_factory,
                    forbidden_secret_values=("cedar-secret",),
                )

        self.assertEqual(factory_calls, 0)

    async def test_deep_phase_promotes_only_requested_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for agent_id, fact_text, first in (
                ("agent-a", "Ryan agent a cedar fact.", 1.0),
                ("agent-b", "Ryan agent b cedar fact.", 0.5),
            ):
                commit_dream_cycle(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=fact_text,
                            subject="Ryan",
                            category="fact",
                            importance=5,
                            confidence=0.8,
                            source_message_ids=[1],
                        )
                    ],
                    candidate_embeddings=[_unit_vector(first)],
                    agent_id=agent_id,
                    status="ok",
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    messages_processed=1,
                    last_processed_message_id=1,
                )

            await run_deep_phase(
                db_path,
                "glm",
                agent_id="agent-a",
                contradiction_agent_factory=lambda *_args, **_kwargs: SimpleNamespace(),
            )

            with closing(sqlite3.connect(db_path)) as conn:
                facts = conn.execute(
                    "SELECT agent_id, fact_text FROM long_term_memory ORDER BY id"
                ).fetchall()
                candidates = conn.execute(
                    "SELECT agent_id, promoted FROM memory_candidates ORDER BY id"
                ).fetchall()
                deep_runs = conn.execute(
                    "SELECT agent_id, promotions FROM dream_runs WHERE promotions > 0"
                ).fetchall()

        self.assertEqual(facts, [("agent-a", "Ryan agent a cedar fact.")])
        self.assertEqual(candidates, [("agent-a", 1), ("agent-b", 0)])
        self.assertEqual(deep_runs, [("agent-a", 1)])

    async def test_deep_phase_defers_contradiction_without_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for fact_text, confidence, message_id in (
                ("Ryan lives in Seattle.", 0.7, 1),
                ("Ryan lives in Austin.", 0.9, 2),
            ):
                commit_dream_cycle(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=fact_text,
                            subject="Ryan",
                            category="fact",
                            importance=6,
                            confidence=confidence,
                            source_message_ids=[message_id],
                        )
                    ],
                    candidate_embeddings=[_unit_vector(1.0)],
                    agent_id=None,
                    status="ok",
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    messages_processed=1,
                    last_processed_message_id=message_id,
                )
                if message_id == 1:
                    commit_deep_cycle(
                        db_path,
                        [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                        status="ok",
                        started_at="2026-01-01T00:01:00Z",
                        finished_at="2026-01-01T00:01:01Z",
                    )

            with patch(
                "vexic.deep.build_contradiction_agent",
                side_effect=AssertionError("contradiction judge should be deferred"),
            ):
                await run_deep_phase(db_path, "glm")

            with closing(sqlite3.connect(db_path)) as conn:
                facts = conn.execute(
                    """
                    SELECT fact_text, retired, retired_by_fact_id
                    FROM long_term_memory
                    ORDER BY id
                    """
                ).fetchall()
                candidates = conn.execute(
                    "SELECT id, promoted, retired FROM memory_candidates ORDER BY id"
                ).fetchall()

        self.assertEqual(
            facts,
            [
                ("Ryan lives in Seattle.", 0, None),
                ("Ryan lives in Austin.", 0, None),
            ],
        )
        self.assertEqual(candidates, [(1, 1, 0), (2, 1, 0)])

    async def test_deep_phase_redaction_failure_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            second_vector = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
            for fact_text, embedding, message_id in (
                ("Ryan stores cedar-secret outside model prompts.", _unit_vector(1.0), 1),
                ("Ryan prefers compact reports.", second_vector, 2),
            ):
                commit_dream_cycle(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=fact_text,
                            subject="Ryan",
                            category="constraint",
                            importance=8,
                            confidence=0.9,
                            source_message_ids=[message_id],
                        )
                    ],
                    candidate_embeddings=[embedding],
                    agent_id=None,
                    status="ok",
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    messages_processed=1,
                    last_processed_message_id=message_id,
                )
            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                status="ok",
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )
            agent_calls = 0

            class ContradictionAgent:
                async def run(self, prompt: str) -> object:
                    nonlocal agent_calls
                    agent_calls += 1
                    return SimpleNamespace(
                        output=ContradictionJudgment(
                            contradicts=False,
                            confidence=0.9,
                        )
                    )

            with self.assertRaises(ValueError):
                await run_deep_phase(
                    db_path,
                    "glm",
                    contradiction_agent_factory=lambda *_args, **_kwargs: ContradictionAgent(),
                    forbidden_secret_values=("cedar-secret",),
                )

            with closing(sqlite3.connect(db_path)) as conn:
                promoted = conn.execute(
                    "SELECT promoted FROM memory_candidates WHERE id = 2"
                ).fetchone()[0]

        self.assertEqual(agent_calls, 0)
        self.assertEqual(promoted, 0)

    def test_deep_commit_rejects_decisions_outside_requested_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for agent_id, fact_text, first in (
                ("agent-a", "Ryan agent a cedar fact.", 1.0),
                ("agent-b", "Ryan agent b cedar fact.", 0.5),
            ):
                commit_dream_cycle(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=fact_text,
                            subject="Ryan",
                            category="fact",
                            importance=5,
                            confidence=0.8,
                            source_message_ids=[1],
                        )
                    ],
                    candidate_embeddings=[_unit_vector(first)],
                    agent_id=agent_id,
                    status="ok",
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    messages_processed=1,
                    last_processed_message_id=1,
                )
            with closing(sqlite3.connect(db_path)) as conn:
                candidate_ids = {
                    row[1]: int(row[0])
                    for row in conn.execute(
                        "SELECT id, agent_id FROM memory_candidates ORDER BY id"
                    )
                }

            with self.assertRaisesRegex(ValueError, "agent scope"):
                commit_deep_cycle(
                    db_path,
                    [
                        PromotionDecision(
                            candidate_ids["agent-a"],
                            _unit_vector(1.0),
                        ),
                        PromotionDecision(
                            candidate_ids["agent-b"],
                            _unit_vector(0.5),
                        ),
                    ],
                    agent_id="agent-a",
                    started_at="2026-01-01T00:01:00Z",
                    finished_at="2026-01-01T00:01:01Z",
                )

            with self.assertRaisesRegex(ValueError, "agent scope"):
                commit_deep_cycle(
                    db_path,
                    [
                        CandidateRetirementDecision(
                            candidate_ids["agent-b"],
                            retired_by_fact_id=999,
                        )
                    ],
                    agent_id="agent-a",
                    started_at="2026-01-01T00:02:00Z",
                    finished_at="2026-01-01T00:02:01Z",
                )

            with closing(sqlite3.connect(db_path)) as conn:
                fact_count = conn.execute(
                    "SELECT COUNT(*) FROM long_term_memory"
                ).fetchone()[0]
                promotion_runs = conn.execute(
                    "SELECT agent_id, promotions FROM dream_runs WHERE promotions > 0"
                ).fetchall()

        self.assertEqual(fact_count, 0)
        self.assertEqual(promotion_runs, [])

    def test_deep_commit_rejects_fact_references_outside_requested_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan agent a cedar fact.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[1],
                    )
                ],
                candidate_embeddings=[_unit_vector(1.0)],
                agent_id="agent-a",
                status="ok",
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:01Z",
                messages_processed=1,
                last_processed_message_id=1,
            )
            with closing(sqlite3.connect(db_path)) as conn, conn:
                agent_a_candidate_id = int(
                    conn.execute(
                        "SELECT id FROM memory_candidates WHERE agent_id = 'agent-a'"
                    ).fetchone()[0]
                )
                agent_b_fact_id = int(
                    conn.execute(
                        """
                        INSERT INTO long_term_memory
                            (fact_text, subject, category, importance, confidence,
                             source_message_ids, agent_id, promoted_from_candidate_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "Ryan agent b cedar fact.",
                            "Ryan",
                            "fact",
                            5,
                            0.8,
                            "[1]",
                            "agent-b",
                            999,
                        ),
                    ).lastrowid
                )

            for started_at, decision, message in (
                (
                    "2026-01-01T00:01:00Z",
                    PromotionDecision(
                        agent_a_candidate_id,
                        _unit_vector(1.0),
                        retired_fact_id=999,
                    ),
                    "Missing retiring fact",
                ),
                (
                    "2026-01-01T00:02:00Z",
                    CandidateRetirementDecision(
                        agent_a_candidate_id,
                        retired_by_fact_id=999,
                    ),
                    "Missing retiring fact",
                ),
                (
                    "2026-01-01T00:03:00Z",
                    PromotionDecision(
                        agent_a_candidate_id,
                        _unit_vector(1.0),
                        retired_fact_id=agent_b_fact_id,
                    ),
                    "agent scope",
                ),
                (
                    "2026-01-01T00:04:00Z",
                    CandidateRetirementDecision(
                        agent_a_candidate_id,
                        retired_by_fact_id=agent_b_fact_id,
                    ),
                    "agent scope",
                ),
            ):
                with self.subTest(started_at=started_at):
                    with self.assertRaisesRegex(ValueError, message):
                        commit_deep_cycle(
                            db_path,
                            [decision],
                            agent_id="agent-a",
                            started_at=started_at,
                            finished_at=started_at,
                        )

            with closing(sqlite3.connect(db_path)) as conn:
                invalid_run_count = conn.execute(
                    """
                    SELECT COUNT(*) FROM dream_runs
                    WHERE agent_id = 'agent-a'
                        AND status = 'ok'
                        AND started_at >= '2026-01-01T00:01:00Z'
                    """
                ).fetchone()[0]
                agent_a_promoted = conn.execute(
                    """
                    SELECT promoted FROM memory_candidates
                    WHERE id = ?
                    """,
                    (agent_a_candidate_id,),
                ).fetchone()[0]

        self.assertEqual(invalid_run_count, 0)
        self.assertEqual(agent_a_promoted, 0)


class PipelineCliTests(unittest.TestCase):
    def test_cli_empty_database_noops_without_embedding_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            stdout = StringIO()
            argv = ["vexic.pipeline", "--db", db_path, "--model-group", "glm"]

            with (
                patch.object(sys, "argv", argv),
                redirect_stdout(stdout),
            ):
                _main()

        self.assertIn("Light phase: no new messages. No-op.", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
