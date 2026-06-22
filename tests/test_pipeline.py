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
from vexic.models import FactCandidate
from vexic.pipeline import _main, run_light_phase
from vexic.ports import HostPortNotConfigured
from vexic.storage import commit_dream_cycle, init_db, save_messages


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
