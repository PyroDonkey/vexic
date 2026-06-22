from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.embeddings import EMBEDDING_DIM
from vexic.deep import run_deep_phase
from vexic.models import FactCandidate, RemBoost, RemBoostPlan
from vexic.pipeline import _main, run_light_phase
from vexic.ports import HostPortNotConfigured
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

    async def test_light_phase_requires_explicit_embedding_port_before_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )
            agent_factory_called = False

            def agent_factory(model_group: str, secrets: object = None) -> object:
                nonlocal agent_factory_called
                agent_factory_called = True
                return SimpleNamespace()

            with self.assertRaisesRegex(HostPortNotConfigured, "Embeddings"):
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=agent_factory,
                )

            self.assertFalse(agent_factory_called)

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

            def embed(texts: list[str]) -> list[list[float]]:
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

            await run_light_phase(db_path, "glm", agent_id="agent-a", embed=embed)

            self.assertEqual(
                repaired_state(),
                [("agent-a", 1), ("agent-b", 0), (None, 0)],
            )

            await run_light_phase(db_path, "glm", agent_id=None, embed=embed)

            self.assertEqual(
                repaired_state(),
                [("agent-a", 1), ("agent-b", 0), (None, 1)],
            )

    async def test_rem_phase_boosts_only_requested_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for agent_id, fact_text, first in (
                ("agent-a", "Ryan agent a cedar candidate.", 1.0),
                ("agent-b", "Ryan agent b cedar candidate.", 0.5),
                (None, "Ryan shared cedar candidate.", 0.25),
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

            await run_rem_phase(
                db_path,
                "glm",
                agent_id="agent-a",
                rem_agent_factory=lambda *_args, **_kwargs: RemAgent(),
            )

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

        self.assertEqual(len(prompts), 1)
        self.assertIn("Ryan agent a cedar candidate.", prompts[0])
        self.assertNotIn("Ryan agent b cedar candidate.", prompts[0])
        self.assertNotIn("Ryan shared cedar candidate.", prompts[0])
        self.assertEqual(
            candidates,
            [
                ("agent-a", "Ryan agent a cedar candidate.", 0.5),
                ("agent-b", "Ryan agent b cedar candidate.", 0.0),
                (None, "Ryan shared cedar candidate.", 0.0),
            ],
        )
        self.assertEqual(rem_runs, [("agent-a", 1)])

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
    def test_cli_without_embedding_adapter_exits_with_configuration_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            stderr = StringIO()
            argv = ["vexic.pipeline", "--db", db_path, "--model-group", "glm"]

            with (
                patch.object(sys, "argv", argv),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as caught,
            ):
                _main()

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Embeddings requires a host-supplied model port", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
