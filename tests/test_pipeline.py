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
from vexic.error_reporting import dream_failure_recorded
from vexic.deep import run_deep_phase
from vexic.models import ContradictionJudgment, FactCandidate
from vexic.pipeline import (
    _main,
    _plausible_years,
    apply_occurred_at_guards,
    render_transcript,
    rendered_message_ids,
    run_light_phase,
)
from vexic.ports import HostPortNotConfigured
from vexic.rem import REM_TOP_K, compute_centrality_boosts, run_rem_phase
from vexic.storage import (
    CandidateRetirementDecision,
    PromotionDecision,
    RemCandidate,
    backfill_missing_candidate_embeddings,
    commit_deep_cycle,
    commit_dream_cycle,
    get_watermark,
    init_db,
    load_candidates_missing_embeddings,
    load_messages_since,
    load_rem_candidates,
    save_messages,
)
from vexic.storage.candidates import claim_candidate_for_promotion
from vexic.storage.connection import connect
from vexic.storage.schema import _load_vec_extension


def _UPSTREAM_502() -> ValueError:
    # The libSQL/Hrana bare ValueError raised when the Turso edge cannot reach
    # its upstream primary (observed live 2026-07-13 as 502 Bad Gateway).
    # Classified retryable by vexic.storage.errors.is_retryable_operational_error.
    return ValueError(
        "Hrana: `api error: `status=502 Bad Gateway, "
        'body={"error":"connect to upstream failed"}``'
    )


def _unit_vector(first: float) -> list[float]:
    return [first] + [0.0] * (EMBEDDING_DIM - 1)


def _padded_vector(*components: float) -> list[float]:
    return [*components] + [0.0] * (EMBEDDING_DIM - len(components))


def _rem_candidate(candidate_id: int, embedding: list[float] | None) -> RemCandidate:
    return RemCandidate(candidate_id=candidate_id, embedding=embedding)


def user_message(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _fake_usage() -> SimpleNamespace:
    # Property-form usage (pydantic-ai >=1.102); summarize_agent_usage fails
    # loud on results that expose no usage, so every fake must carry one.
    return SimpleNamespace(requests=1, input_tokens=1, output_tokens=1, total_tokens=2)


class LightPhaseProvenanceTests(unittest.IsolatedAsyncioTestCase):
    """Invariant 5 (every fact carries real provenance) is enforced per
    candidate, not per batch: a candidate citing message ids outside the
    rendered window is dropped, and the rest of the extraction still lands.
    A model that miscites one fact must not cost the whole Light run -- that
    halts the chain, so REM and Deep never run and Tier 3 stops advancing."""

    async def test_light_phase_drops_candidate_citing_message_outside_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )[0]
            outside_window_id = message_id + 999

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
                                source_message_ids=[message_id],
                            ),
                            FactCandidate(
                                fact_text="Ryan lives on Mars.",
                                subject="Ryan",
                                category="fact",
                                importance=5,
                                confidence=0.8,
                                source_message_ids=[outside_window_id],
                            ),
                        ],
                        usage=_fake_usage(),
                    )

            def agent_factory(model_group: str, secrets: object = None) -> object:
                return ExtractionAgent()

            def embed(texts: list[str]) -> list[list[float]]:
                return [_unit_vector(1.0) for _ in texts]

            await run_light_phase(
                db_path,
                "glm",
                extraction_agent_factory=agent_factory,
                embed=embed,
            )

            with closing(sqlite3.connect(db_path)) as conn:
                persisted = [
                    row[0]
                    for row in conn.execute(
                        "SELECT fact_text FROM memory_candidates ORDER BY id"
                    ).fetchall()
                ]
                run_status = conn.execute(
                    "SELECT status, last_processed_message_id, candidates_dropped"
                    " FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()

        self.assertEqual(persisted, ["Ryan prefers compact reports."])
        # A run that kept candidates stays 'ok'; the drop is still counted.
        self.assertEqual(run_status, ("ok", message_id, 1))

    async def test_light_phase_drops_candidate_with_no_source_message_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )[0]

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
                                source_message_ids=[message_id],
                            ),
                            FactCandidate(
                                fact_text="Ryan dislikes long meetings.",
                                subject="Ryan",
                                category="preference",
                                importance=5,
                                confidence=0.8,
                                source_message_ids=[],
                            ),
                        ],
                        usage=_fake_usage(),
                    )

            def agent_factory(model_group: str, secrets: object = None) -> object:
                return ExtractionAgent()

            def embed(texts: list[str]) -> list[list[float]]:
                return [_unit_vector(1.0) for _ in texts]

            await run_light_phase(
                db_path,
                "glm",
                extraction_agent_factory=agent_factory,
                embed=embed,
            )

            with closing(sqlite3.connect(db_path)) as conn:
                persisted = [
                    row[0]
                    for row in conn.execute(
                        "SELECT fact_text FROM memory_candidates ORDER BY id"
                    ).fetchall()
                ]

        self.assertEqual(persisted, ["Ryan prefers compact reports."])

    async def test_light_phase_applies_occurred_at_guards_before_commit(self) -> None:
        """ADR 0038: apply_occurred_at_guards must run inside run_light_phase,
        before commit. A candidate with a fabricated occurred_at year that has
        no grounding in the window's observed dates or transcript text must
        never reach memory_candidates -- it is caught before persistence, not
        cleaned up afterward."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="We shipped the release.")])],
                timestamp="2023-11-15 12:00:00",
            )[0]

            class ExtractionAgent:
                async def run(self, transcript: str) -> object:
                    return SimpleNamespace(
                        output=[
                            FactCandidate(
                                fact_text="Ryan shipped the release.",
                                subject="Ryan",
                                category="event",
                                importance=6,
                                confidence=0.9,
                                source_message_ids=[message_id],
                                occurred_at="2025-03-01",
                            ),
                        ],
                        usage=_fake_usage(),
                    )

            def agent_factory(model_group: str, secrets: object = None) -> object:
                return ExtractionAgent()

            def embed(texts: list[str]) -> list[list[float]]:
                return [_unit_vector(1.0) for _ in texts]

            await run_light_phase(
                db_path,
                "glm",
                extraction_agent_factory=agent_factory,
                embed=embed,
            )

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT fact_text, occurred_at FROM memory_candidates ORDER BY id"
                ).fetchone()

        self.assertEqual(row[0], "Ryan shipped the release.")
        self.assertIsNone(row[1])

    async def test_light_phase_advances_watermark_when_every_candidate_is_dropped(
        self,
    ) -> None:
        # A run where the model miscites everything must still complete and
        # advance the watermark. Holding it would re-extract the same window
        # every tick -- paying the model over and over for a batch that can
        # never land -- and halt the chain behind it.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )[0]

            class ExtractionAgent:
                async def run(self, transcript: str) -> object:
                    return SimpleNamespace(
                        output=[
                            FactCandidate(
                                fact_text="Ryan lives on Mars.",
                                subject="Ryan",
                                category="fact",
                                importance=5,
                                confidence=0.8,
                                source_message_ids=[message_id + 999],
                            )
                        ],
                        usage=_fake_usage(),
                    )

            def agent_factory(model_group: str, secrets: object = None) -> object:
                return ExtractionAgent()

            def embed(texts: list[str]) -> list[list[float]]:
                return [_unit_vector(1.0) for _ in texts]

            output = StringIO()
            with redirect_stdout(output):
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=agent_factory,
                    embed=embed,
                )

            self.assertEqual(get_watermark(db_path, agent_id=None), message_id)
            with closing(sqlite3.connect(db_path)) as conn:
                candidate_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidates"
                ).fetchone()[0]
                run_row = conn.execute(
                    "SELECT status, candidates_dropped, error_detail"
                    " FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()

        self.assertEqual(candidate_count, 0)
        # ADR 0031 amendment: a run that extracted candidates and kept none is
        # durably 'partial' with the drop count, queryable without logs.
        self.assertEqual(run_row, ("partial", 1, None))
        # Content-free: the operator learns that provenance dropped candidates
        # and how many, never which facts or which message ids.
        self.assertIn("1 dropped", output.getvalue())
        self.assertNotIn("Mars", output.getvalue())

    async def test_error_after_filtering_still_records_known_drop_count(self) -> None:
        # A failure between provenance filtering and commit must not zero the
        # already-known drop count on the error audit row: the failed cycle's
        # drop telemetry is the ADR 0031 miscitation signal either way.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )[0]

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
                                source_message_ids=[message_id],
                            ),
                            FactCandidate(
                                fact_text="Ryan lives on Mars.",
                                subject="Ryan",
                                category="fact",
                                importance=5,
                                confidence=0.8,
                                source_message_ids=[message_id + 999],
                            ),
                        ],
                        usage=_fake_usage(),
                    )

            def failing_embed(texts: list[str]) -> list[list[float]]:
                raise RuntimeError("embedding backend down")

            output = StringIO()
            with redirect_stdout(output):
                with self.assertRaises(RuntimeError):
                    await run_light_phase(
                        db_path,
                        "glm",
                        extraction_agent_factory=lambda group, secrets=None: ExtractionAgent(),
                        embed=failing_embed,
                    )

            with closing(sqlite3.connect(db_path)) as conn:
                status, dropped = conn.execute(
                    "SELECT status, candidates_dropped FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()

        self.assertEqual(status, "error")
        self.assertEqual(dropped, 1)

    def test_dream_runs_schema_has_durable_drop_count(self) -> None:
        # ADR 0031's mitigation for silent systematic miscitation is the drop
        # count; stdout is not durable, so the count lives in dream_runs.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            with closing(sqlite3.connect(db_path)) as conn:
                columns = {
                    row[1]: row for row in conn.execute("PRAGMA table_info(dream_runs)")
                }

        self.assertIn("candidates_dropped", columns)
        _, _, _, not_null, default, _ = columns["candidates_dropped"]
        self.assertEqual(not_null, 1)
        self.assertEqual(default, "0")

    def test_commit_dream_cycle_persists_drop_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [],
                agent_id=None,
                status="partial",
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:01Z",
                messages_processed=3,
                last_processed_message_id=3,
                candidates_dropped=3,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT status, candidates_dropped FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()

        self.assertEqual(row, ("partial", 3))


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
                        ],
                        usage=_fake_usage(),
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

    async def test_light_phase_preflights_default_embedding_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )
            agent_calls = 0
            fastembed_imports = 0
            original_import = __import__

            class ExtractionAgent:
                async def run(self, transcript: str) -> object:
                    nonlocal agent_calls
                    agent_calls += 1
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
                        ],
                        usage=_fake_usage(),
                    )

            def blocked_import(name: str, *args: object, **kwargs: object) -> object:
                nonlocal fastembed_imports
                if name == "fastembed":
                    fastembed_imports += 1
                    raise ModuleNotFoundError("No module named 'fastembed'")
                return original_import(name, *args, **kwargs)

            with (
                patch("vexic.embeddings.find_spec", return_value=None, create=True),
                patch("builtins.__import__", side_effect=blocked_import),
                self.assertRaises(HostPortNotConfigured),
            ):
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=lambda *_args, **_kwargs: ExtractionAgent(),
                )

        self.assertEqual((agent_calls, fastembed_imports), (0, 0))

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
                        ],
                        usage=_fake_usage(),
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
            # agent-a gets two candidates with identical unit-vector embeddings
            # (centrality 1.0). Their categories differ so commit-time dedup
            # does not merge them. agent-b and the shared scope each hold a
            # single candidate, whose centrality is 0.0 by definition.
            for agent_id, fact_text, category in (
                ("agent-a", "Ryan agent a cedar candidate.", "fact"),
                ("agent-a", "Ryan agent a cedar twin.", "preference"),
                ("agent-b", "Ryan agent b cedar candidate.", "fact"),
                (None, "Ryan shared cedar candidate.", "fact"),
            ):
                commit_dream_cycle(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=fact_text,
                            subject="Ryan",
                            category=category,
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

            def rem_state() -> tuple[list[tuple[str | None, str, float]], list[tuple[str | None, int]]]:
                with closing(sqlite3.connect(db_path)) as conn:
                    candidates = conn.execute(
                        """
                        SELECT agent_id, fact_text, rem_boost
                        FROM memory_candidates
                        ORDER BY id
                        """
                    ).fetchall()
                    # candidates_boosted counts UPDATE rowcount, so 0.0 writes
                    # count too: the > 0 filter keeps every REM run that touched
                    # at least one candidate in its scope.
                    rem_runs = conn.execute(
                        """
                        SELECT agent_id, candidates_boosted
                        FROM dream_runs
                        WHERE candidates_boosted > 0
                        ORDER BY id
                        """
                    ).fetchall()
                return candidates, rem_runs

            await run_rem_phase(db_path, agent_id="agent-a")
            after_agent_a = rem_state()

            await run_rem_phase(db_path, agent_id=None)
            after_shared = rem_state()

        self.assertEqual(
            after_agent_a,
            (
                [
                    ("agent-a", "Ryan agent a cedar candidate.", 1.0),
                    ("agent-a", "Ryan agent a cedar twin.", 1.0),
                    ("agent-b", "Ryan agent b cedar candidate.", 0.0),
                    (None, "Ryan shared cedar candidate.", 0.0),
                ],
                [("agent-a", 2)],
            ),
        )
        self.assertEqual(
            after_shared,
            (
                [
                    ("agent-a", "Ryan agent a cedar candidate.", 1.0),
                    ("agent-a", "Ryan agent a cedar twin.", 1.0),
                    ("agent-b", "Ryan agent b cedar candidate.", 0.0),
                    (None, "Ryan shared cedar candidate.", 0.0),
                ],
                [("agent-a", 2), (None, 1)],
            ),
        )

    async def test_rem_phase_succeeds_when_fact_text_contains_forbidden_value(self) -> None:
        # REM builds no prompts and calls no model, so a forbidden secret in a
        # candidate's fact text is never egressed and must not fail the phase.
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

            await run_rem_phase(db_path, forbidden_secret_values=("cedar-secret",))

            with closing(sqlite3.connect(db_path)) as conn:
                boost = conn.execute(
                    "SELECT rem_boost FROM memory_candidates"
                ).fetchone()[0]
                rem_status = conn.execute(
                    "SELECT status FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]

        self.assertEqual(boost, 0.0)
        self.assertEqual(rem_status, "ok")

    async def test_rem_phase_resets_stale_boost_when_embedding_disappears(self) -> None:
        # A candidate boosted in an earlier cycle whose embedding later goes
        # missing (e.g. an interrupted repair) must be reset to 0.0, not keep
        # its stale boost -- the reason the loader LEFT JOINs embeddings.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar stale boost candidate.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
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
            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                conn.execute("UPDATE memory_candidates SET rem_boost = 0.7")
                conn.execute("DELETE FROM memory_candidate_embeddings")
                conn.commit()

            await run_rem_phase(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                boost = conn.execute(
                    "SELECT rem_boost FROM memory_candidates"
                ).fetchone()[0]

        self.assertEqual(boost, 0.0)

    async def test_rem_phase_commits_error_run_and_reraises_on_failure(self) -> None:
        # A failing cycle must leave boosts untouched, record an error
        # dream_run with no boosts, and re-raise.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar error path candidate.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
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
            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                conn.execute("UPDATE memory_candidates SET rem_boost = 0.7")
                conn.commit()

            with patch(
                "vexic.rem.compute_centrality_boosts",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertRaises(RuntimeError):
                    await run_rem_phase(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                boost = conn.execute(
                    "SELECT rem_boost FROM memory_candidates"
                ).fetchone()[0]
                status, boosted = conn.execute(
                    "SELECT status, candidates_boosted FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()

        self.assertEqual(boost, 0.7)
        self.assertEqual(status, "error")
        self.assertEqual(boosted, 0)

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
                        retired_fact_ids=(999,),
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
                        retired_fact_ids=(agent_b_fact_id,),
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

    def test_deep_commit_rejects_event_candidate_without_occurred_at(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan shipped the release on July 5.",
                        subject="Ryan",
                        category="event",
                        importance=6,
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

            with self.assertRaisesRegex(ValueError, r"candidate 1.*'event'"):
                commit_deep_cycle(
                    db_path,
                    [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                    started_at="2026-01-01T00:01:00Z",
                    finished_at="2026-01-01T00:01:01Z",
                )

            with closing(sqlite3.connect(db_path)) as conn:
                fact_count = conn.execute(
                    "SELECT COUNT(*) FROM long_term_memory"
                ).fetchone()[0]
                promoted = conn.execute(
                    "SELECT promoted FROM memory_candidates WHERE id = 1"
                ).fetchone()[0]

        self.assertEqual(fact_count, 0)
        self.assertEqual(promoted, 0)

    def test_deep_commit_rejects_event_candidate_with_blank_occurred_at(self) -> None:
        # Regression: `occurred_at is None` alone let a blank string through,
        # since "" is falsy but not None. The check must treat blank the same
        # as missing.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan shipped the release on July 5.",
                        subject="Ryan",
                        category="event",
                        importance=6,
                        confidence=0.9,
                        source_message_ids=[1],
                        occurred_at="",
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

            with self.assertRaisesRegex(ValueError, r"candidate 1.*'event'"):
                commit_deep_cycle(
                    db_path,
                    [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                    started_at="2026-01-01T00:01:00Z",
                    finished_at="2026-01-01T00:01:01Z",
                )

    def test_deep_commit_rejects_event_candidate_with_whitespace_only_dates(self) -> None:
        # Greptile P1 regression: a migrated or externally written row can
        # carry whitespace-only date strings, which are truthy — the guard
        # must .strip() both columns (matching the Deep selection filter) so
        # a blank-ish date never reaches Tier 3, where the NULLIF('')-based
        # retrieval ladder would treat it as a real temporal key.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan shipped the release on July 5.",
                        subject="Ryan",
                        category="event",
                        importance=6,
                        confidence=0.9,
                        source_message_ids=[1],
                        occurred_at="   ",
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
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE memory_candidates SET mentioned_at = '   ' WHERE id = 1"
                )
                conn.commit()

            with self.assertRaisesRegex(ValueError, r"candidate 1.*'event'"):
                commit_deep_cycle(
                    db_path,
                    [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                    started_at="2026-01-01T00:01:00Z",
                    finished_at="2026-01-01T00:01:01Z",
                )

            with closing(sqlite3.connect(db_path)) as conn:
                fact_count = conn.execute(
                    "SELECT COUNT(*) FROM long_term_memory"
                ).fetchone()[0]

        self.assertEqual(fact_count, 0)

    def test_deep_commit_normalizes_whitespace_occurred_at_instead_of_poisoning_tier3(self) -> None:
        # Grok 4.5 audit: with a real mentioned_at, a whitespace-only
        # occurred_at passes the OR-guard — but the raw "   " must never
        # reach Tier 3, where the NULLIF('')-based windowing ladder would
        # treat it as the temporal key (space sorts before every digit).
        # Blank-ish dates are normalized to NULL at write time.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="We got the mortgage sorted.")])],
                timestamp="2026-03-05T10:00:00+00:00",
            )[0]
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan updated the mortgage.",
                        subject="Ryan",
                        category="event",
                        importance=6,
                        confidence=0.9,
                        source_message_ids=[message_id],
                        occurred_at="   ",
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

            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                occurred_at, mentioned_at = conn.execute(
                    """
                    SELECT occurred_at, mentioned_at FROM long_term_memory
                    WHERE promoted_from_candidate_id = 1
                    """
                ).fetchone()

        self.assertIsNone(
            occurred_at,
            "whitespace occurred_at must be normalized to NULL, never stored",
        )
        self.assertEqual(mentioned_at, "2026-03-05")

    def test_deep_commit_is_idempotent_for_legacy_promoted_event_candidate(self) -> None:
        # Regression: the event/occurred_at check originally ran before the
        # `promoted` idempotency skip, so a candidate promoted before event-time support
        # shipped (occurred_at forever NULL, but already promoted=1 with a
        # linked Tier 3 fact) would raise ValueError on a benign rerun instead
        # of hitting the documented idempotent no-op.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan shipped the release on July 5.",
                        subject="Ryan",
                        category="event",
                        importance=6,
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

            # Simulate a legacy promotion that predates occurred_at: write the
            # Tier 3 fact and flip promoted=1 directly, bypassing the (now
            # occurred_at-checking) promotion path.
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO long_term_memory
                        (fact_text, subject, category, importance, confidence,
                         source_message_ids, promoted_from_candidate_id)
                    VALUES (
                        'Ryan shipped the release on July 5.', 'Ryan', 'event', 6, 0.9,
                        '[1]', 1
                    )
                    """
                )
                conn.execute(
                    "UPDATE memory_candidates SET promoted = 1, promoted_fact_id = 1 WHERE id = 1"
                )
                conn.commit()

            # Rerunning promotion on the same candidate must be a benign
            # no-op, not a ValueError.
            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:02:00Z",
                finished_at="2026-01-01T00:02:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                fact_count = conn.execute(
                    "SELECT COUNT(*) FROM long_term_memory"
                ).fetchone()[0]

        self.assertEqual(fact_count, 1, "rerun must not write a second Tier 3 fact")

    def test_deep_commit_promotes_event_candidate_with_occurred_at(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan shipped the release on July 5.",
                        subject="Ryan",
                        category="event",
                        importance=6,
                        confidence=0.9,
                        source_message_ids=[1],
                        occurred_at="2026-07-05",
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

            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                occurred_at, category = conn.execute(
                    """
                    SELECT occurred_at, category FROM long_term_memory
                    WHERE promoted_from_candidate_id = 1
                    """
                ).fetchone()

        self.assertEqual(category, "event")
        self.assertEqual(occurred_at, "2026-07-05")

    def test_deep_commit_normalizes_legacy_datetime_occurred_at(self) -> None:
        # A legacy or foreign-written memory_candidates row can hold a
        # datetime-shaped occurred_at that never passed the FactCandidate
        # validator (Deep promotion loads rows straight from SQL). Promotion
        # must canonicalize it to a partial-precision date, not copy the raw
        # datetime into Tier 3 (Memory Invariant 11: truncation, never
        # invention).
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan shipped the release.",
                        subject="Ryan",
                        category="fact",
                        importance=6,
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
            # Write a datetime-shaped occurred_at directly, bypassing the
            # validator (simulating a legacy/foreign writer).
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE memory_candidates SET occurred_at = '2026-07-05T00:00:00Z' WHERE id = 1"
                )
                conn.commit()

            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                (occurred_at,) = conn.execute(
                    """
                    SELECT occurred_at FROM long_term_memory
                    WHERE promoted_from_candidate_id = 1
                    """
                ).fetchone()

        self.assertEqual(occurred_at, "2026-07-05")

    def test_deep_commit_promotes_event_candidate_via_mentioned_at(self) -> None:
        # ADR 0037: an undated event whose source messages carry timestamps
        # promotes on mentioned_at provenance. occurred_at is never fabricated
        # from mention time — it stays NULL on the Tier 3 row.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="We got the mortgage sorted.")])],
                timestamp="2026-03-05T10:00:00+00:00",
            )[0]
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan updated the mortgage.",
                        subject="Ryan",
                        category="event",
                        importance=6,
                        confidence=0.9,
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

            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                occurred_at, mentioned_at, category = conn.execute(
                    """
                    SELECT occurred_at, mentioned_at, category FROM long_term_memory
                    WHERE promoted_from_candidate_id = 1
                    """
                ).fetchone()

        self.assertEqual(category, "event")
        self.assertIsNone(occurred_at, "mention time must never be written as event time")
        self.assertEqual(mentioned_at, "2026-03-05")

    def test_deep_commit_promotion_carries_mentioned_at_for_non_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_id = save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer dark mode.")])],
                timestamp="2026-02-01T08:00:00+00:00",
            )[0]
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan prefers dark mode editors.",
                        subject="Ryan",
                        category="preference",
                        importance=6,
                        confidence=0.9,
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

            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                mentioned_at = conn.execute(
                    """
                    SELECT mentioned_at FROM long_term_memory
                    WHERE promoted_from_candidate_id = 1
                    """
                ).fetchone()[0]

        self.assertEqual(mentioned_at, "2026-02-01")

    def test_deep_commit_promotes_non_event_candidate_without_occurred_at(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan prefers dark mode editors.",
                        subject="Ryan",
                        category="preference",
                        importance=5,
                        confidence=0.8,
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

            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                occurred_at, category, count = conn.execute(
                    """
                    SELECT occurred_at, category, COUNT(*) FROM long_term_memory
                    WHERE promoted_from_candidate_id = 1
                    """
                ).fetchone()

        self.assertEqual(category, "preference")
        self.assertIsNone(occurred_at)
        self.assertEqual(count, 1)


class DreamPhaseFailureRecordedTests(unittest.IsolatedAsyncioTestCase):
    """A failed dream phase must surface whether its terminal
    ``dream_runs`` error row was durably persisted, so the sweeper can refuse
    to advance the 24h retry clock over a silent failure."""

    def _seed_candidate(self, db_path: str) -> None:
        commit_dream_cycle(
            db_path,
            [
                FactCandidate(
                    fact_text="Ryan cedar recorded-bit candidate.",
                    subject="Ryan",
                    category="fact",
                    importance=5,
                    confidence=0.8,
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

    async def test_rem_failure_marks_recorded_when_error_row_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            self._seed_candidate(db_path)
            with patch(
                "vexic.rem.compute_centrality_boosts",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    await run_rem_phase(db_path)

            self.assertTrue(dream_failure_recorded(cm.exception))
            with closing(sqlite3.connect(db_path)) as conn:
                status = conn.execute(
                    "SELECT status FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            self.assertEqual(status, "error")

    async def test_deep_failure_marks_recorded_when_error_row_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            self._seed_candidate(db_path)
            with patch(
                "vexic.deep.load_promotion_candidates",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    await run_deep_phase(
                        db_path,
                        "glm",
                        contradiction_agent_factory=lambda *_a, **_k: SimpleNamespace(),
                    )

            self.assertTrue(dream_failure_recorded(cm.exception))
            with closing(sqlite3.connect(db_path)) as conn:
                status = conn.execute(
                    "SELECT status FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            self.assertEqual(status, "error")

    async def test_light_failure_marks_recorded_when_error_row_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="hello there")])],
            )

            class FailingExtractionAgent:
                async def run(self, transcript: str) -> object:
                    raise RuntimeError("boom")

            with self.assertRaises(RuntimeError) as cm:
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=lambda *_a, **_k: FailingExtractionAgent(),
                    embed=lambda texts: [_unit_vector(1.0) for _ in texts],
                )

            self.assertTrue(dream_failure_recorded(cm.exception))
            with closing(sqlite3.connect(db_path)) as conn:
                status = conn.execute(
                    "SELECT status FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            self.assertEqual(status, "error")

    async def test_error_row_retries_once_on_retryable_fault_then_records(
        self,
    ) -> None:
        # The terminal error-row write itself faults with a retryable Turso
        # 502, then succeeds on a fresh connection: recorded True, retried once.
        from vexic.storage import commit_rem_cycle as real_commit

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            self._seed_candidate(db_path)

            calls = {"n": 0}

            def flaky(*args: object, **kwargs: object) -> object:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise _UPSTREAM_502()
                return real_commit(*args, **kwargs)

            with patch(
                "vexic.rem.compute_centrality_boosts",
                side_effect=RuntimeError("boom"),
            ):
                with patch("vexic.rem.commit_rem_cycle", side_effect=flaky):
                    with self.assertRaises(RuntimeError) as cm:
                        await run_rem_phase(db_path)

            self.assertTrue(dream_failure_recorded(cm.exception))
            self.assertEqual(calls["n"], 2)
            with closing(sqlite3.connect(db_path)) as conn:
                status = conn.execute(
                    "SELECT status FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            self.assertEqual(status, "error")

    async def test_unrecorded_when_retryable_fault_persists(self) -> None:
        # The error-row write keeps faulting: the original error still surfaces
        # (never masked), no error row lands, and the failure is marked
        # unrecorded so the sweeper will not advance the retry clock over it.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            self._seed_candidate(db_path)

            def always_502(*args: object, **kwargs: object) -> object:
                raise _UPSTREAM_502()

            with patch(
                "vexic.rem.compute_centrality_boosts",
                side_effect=RuntimeError("boom"),
            ):
                with patch("vexic.rem.commit_rem_cycle", side_effect=always_502):
                    with self.assertRaises(RuntimeError) as cm:
                        await run_rem_phase(db_path)

            self.assertFalse(dream_failure_recorded(cm.exception))
            with closing(sqlite3.connect(db_path)) as conn:
                error_rows = conn.execute(
                    "SELECT COUNT(*) FROM dream_runs WHERE status = 'error'"
                ).fetchone()[0]
            self.assertEqual(error_rows, 0)

    async def test_non_retryable_write_fault_is_not_retried(self) -> None:
        # A non-retryable fault in the error-row write fails fast (one attempt),
        # marks unrecorded, and never masks the original error.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            self._seed_candidate(db_path)

            calls = {"n": 0}

            def non_retryable(*args: object, **kwargs: object) -> object:
                calls["n"] += 1
                raise ValueError("plain domain error, not a storage fault")

            with patch(
                "vexic.rem.compute_centrality_boosts",
                side_effect=RuntimeError("boom"),
            ):
                with patch("vexic.rem.commit_rem_cycle", side_effect=non_retryable):
                    with self.assertRaises(RuntimeError) as cm:
                        await run_rem_phase(db_path)

            self.assertFalse(dream_failure_recorded(cm.exception))
            self.assertEqual(calls["n"], 1)

    async def test_recorded_error_detail_is_content_free(self) -> None:
        # The persisted error_detail carries only the exception type + stack
        # shape (format_error_detail), never the exception message text.
        sentinel = "cedar-secret-message-sentinel"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            self._seed_candidate(db_path)
            with patch(
                "vexic.rem.compute_centrality_boosts",
                side_effect=RuntimeError(sentinel),
            ):
                with self.assertRaises(RuntimeError):
                    await run_rem_phase(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                error_detail = conn.execute(
                    "SELECT error_detail FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            self.assertIsNotNone(error_detail)
            self.assertNotIn(sentinel, error_detail)
            self.assertIn("RuntimeError", error_detail)


class RemCentralityBoostTests(unittest.TestCase):
    def test_empty_candidates_return_empty_dict(self) -> None:
        self.assertEqual(compute_centrality_boosts([]), {})

    def test_single_candidate_gets_zero_boost(self) -> None:
        boosts = compute_centrality_boosts([_rem_candidate(1, _unit_vector(1.0))])

        self.assertEqual(boosts, {1: 0.0})

    def test_candidate_without_embedding_scores_zero_and_is_not_a_neighbor(self) -> None:
        # If the missing embedding leaked in as a zero-similarity neighbor, the
        # two identical candidates would average (1.0 + 0.0) / 2 instead of 1.0.
        boosts = compute_centrality_boosts(
            [
                _rem_candidate(1, _unit_vector(1.0)),
                _rem_candidate(2, _unit_vector(1.0)),
                _rem_candidate(3, None),
            ]
        )

        self.assertEqual(boosts, {1: 1.0, 2: 1.0, 3: 0.0})

    def test_fewer_neighbors_than_top_k_means_over_available_only(self) -> None:
        # Three embedded candidates leave each with two neighbors, fewer than
        # REM_TOP_K + 1 embedded overall. Zero-padding to top_k would yield
        # (1.0 + 0.0 + 0.0) / 3 instead of (1.0 + 0.0) / 2.
        self.assertLess(3, REM_TOP_K + 1)
        boosts = compute_centrality_boosts(
            [
                _rem_candidate(1, _unit_vector(1.0)),
                _rem_candidate(2, _unit_vector(1.0)),
                _rem_candidate(3, _padded_vector(0.0, 1.0)),
            ]
        )

        self.assertAlmostEqual(boosts[1], 0.5)
        self.assertAlmostEqual(boosts[2], 0.5)
        self.assertAlmostEqual(boosts[3], 0.0)

    def test_identical_unit_vectors_boost_to_one(self) -> None:
        boosts = compute_centrality_boosts(
            [
                _rem_candidate(1, _unit_vector(1.0)),
                _rem_candidate(2, _unit_vector(1.0)),
            ]
        )

        self.assertEqual(boosts, {1: 1.0, 2: 1.0})

    def test_orthogonal_vectors_boost_to_zero(self) -> None:
        boosts = compute_centrality_boosts(
            [
                _rem_candidate(1, _unit_vector(1.0)),
                _rem_candidate(2, _padded_vector(0.0, 1.0)),
            ]
        )

        self.assertEqual(boosts, {1: 0.0, 2: 0.0})

    def test_negative_cosine_is_clamped_to_zero(self) -> None:
        boosts = compute_centrality_boosts(
            [
                _rem_candidate(1, _unit_vector(1.0)),
                _rem_candidate(2, _unit_vector(-1.0)),
            ]
        )

        self.assertEqual(boosts, {1: 0.0, 2: 0.0})

    def test_two_candidate_pair_scores_single_cosine_not_topk_dilution(self) -> None:
        # A pair has one neighbor each, so the boost is that single cosine.
        # Dividing by top_k instead of available neighbors would report 0.2
        # (0.6 / 3) and silently punish small scopes.
        boosts = compute_centrality_boosts(
            [
                _rem_candidate(1, _unit_vector(1.0)),
                _rem_candidate(2, _padded_vector(0.6, 0.8)),
            ]
        )

        self.assertAlmostEqual(boosts[1], 0.6)
        self.assertAlmostEqual(boosts[2], 0.6)

    def test_clustered_candidates_outrank_isolated_candidate(self) -> None:
        # Centrality rewards tight clusters -- including near-duplicates that
        # survived commit-time dedup (which only merges same subject+category).
        # That inflation is intentional and pinned here: the cluster maxes out
        # while the orthogonal outsider gets nothing.
        boosts = compute_centrality_boosts(
            [
                _rem_candidate(1, _unit_vector(1.0)),
                _rem_candidate(2, _unit_vector(1.0)),
                _rem_candidate(3, _unit_vector(1.0)),
                _rem_candidate(4, _unit_vector(1.0)),
                _rem_candidate(5, _padded_vector(0.0, 1.0)),
            ]
        )

        self.assertEqual(
            boosts, {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 0.0}
        )

    def test_mismatched_embedding_dimensions_raise(self) -> None:
        # zip(..., strict=True) fails loudly on corrupt vectors instead of
        # silently truncating the dot product.
        with self.assertRaises(ValueError):
            compute_centrality_boosts(
                [
                    _rem_candidate(1, [1.0, 0.0]),
                    _rem_candidate(2, [0.0, 1.0, 0.0]),
                ]
            )

    def test_same_input_yields_identical_boosts(self) -> None:
        candidates = [
            _rem_candidate(1, _padded_vector(0.6, 0.8)),
            _rem_candidate(2, _padded_vector(0.8, 0.6)),
            _rem_candidate(3, _padded_vector(0.0, 1.0)),
            _rem_candidate(4, _unit_vector(-1.0)),
            _rem_candidate(5, None),
        ]

        self.assertEqual(
            compute_centrality_boosts(candidates),
            compute_centrality_boosts(candidates),
        )

    def test_boosts_cover_every_candidate_and_stay_in_range(self) -> None:
        candidates = [
            _rem_candidate(1, _padded_vector(0.6, 0.8)),
            _rem_candidate(2, _padded_vector(0.8, 0.6)),
            _rem_candidate(3, _padded_vector(0.0, 1.0)),
            _rem_candidate(4, _unit_vector(1.0)),
            _rem_candidate(5, _unit_vector(-1.0)),
            _rem_candidate(6, None),
        ]

        boosts = compute_centrality_boosts(candidates)

        self.assertEqual(set(boosts), {candidate.candidate_id for candidate in candidates})
        for candidate_id, boost in boosts.items():
            with self.subTest(candidate_id=candidate_id):
                self.assertGreaterEqual(boost, 0.0)
                self.assertLessEqual(boost, 1.0)


class RemCandidateLoaderTests(unittest.TestCase):
    def test_load_rem_candidates_keeps_candidates_without_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for fact_text, embedding in (
                ("Ryan cedar fact one.", _unit_vector(1.0)),
                ("Ryan cedar fact two.", _padded_vector(0.0, 1.0)),
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
                    candidate_embeddings=[embedding],
                    agent_id=None,
                    status="ok",
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    messages_processed=1,
                    last_processed_message_id=1,
                )
            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                conn.execute("DELETE FROM memory_candidate_embeddings WHERE candidate_id = 2")
                conn.commit()

            candidates = load_rem_candidates(db_path, agent_id=None)

        self.assertEqual(
            [candidate.candidate_id for candidate in candidates], [1, 2]
        )
        self.assertEqual(candidates[0].embedding, _unit_vector(1.0))
        self.assertIsNone(candidates[1].embedding)


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


class LightPhaseErrorDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_dropped_candidate_content_never_reaches_diagnostics(self) -> None:
        # dream_runs.error_detail and the operator print are diagnostics; a
        # candidate dropped for bad provenance must not copy user memory text
        # into either. The drop replaces the old fail-the-batch ValueError, so
        # a run that kept nothing lands 'partial' with the miscited candidate
        # simply absent -- content-free either way.
        sentinel = "secret-medical-fact-sentinel"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="hello there")])],
            )

            class ExtractionAgent:
                async def run(self, transcript: str) -> object:
                    return SimpleNamespace(
                        output=[
                            FactCandidate(
                                fact_text=sentinel,
                                subject="Ryan",
                                category="fact",
                                importance=5,
                                confidence=0.8,
                                source_message_ids=[999_999],
                            )
                        ],
                        usage=lambda: SimpleNamespace(
                            requests=1,
                            input_tokens=1,
                            output_tokens=1,
                            total_tokens=2,
                        ),
                    )

            stdout = StringIO()
            with redirect_stdout(stdout):
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=lambda group, secrets=None: ExtractionAgent(),
                    embed=lambda texts: [
                        [1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts
                    ],
                )

            with closing(sqlite3.connect(db_path)) as conn:
                status, error_detail = conn.execute(
                    "SELECT status, error_detail FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                persisted = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidates"
                ).fetchone()[0]

        self.assertEqual(status, "partial")
        self.assertIsNone(error_detail)
        self.assertEqual(persisted, 0)
        self.assertNotIn(sentinel, stdout.getvalue())
        self.assertIn("1 dropped", stdout.getvalue())


def _cosine_vector(cos: float) -> list[float]:
    # A unit vector whose cosine similarity to _unit_vector(1.0) (i.e. the
    # [1, 0, 0, ...] axis) is exactly `cos`. Already unit-length, so
    # commit-time normalization is a no-op and the stored dot product == cos.
    return _padded_vector(cos, (1.0 - cos * cos) ** 0.5)


class PipelineCorrectnessRegressionTests(unittest.TestCase):
    def _commit(
        self,
        db_path: str,
        candidates: list[FactCandidate],
        embeddings: list[list[float]],
        *,
        last_processed_message_id: int,
        observed_watermark: int | None = None,
        agent_id: str | None = None,
        started_at: str = "2026-01-01T00:00:00Z",
    ) -> None:
        commit_dream_cycle(
            db_path,
            candidates,
            candidate_embeddings=embeddings,
            agent_id=agent_id,
            status="ok",
            started_at=started_at,
            finished_at=started_at,
            messages_processed=len(candidates),
            last_processed_message_id=last_processed_message_id,
            observed_watermark=observed_watermark,
        )

    def test_superseded_partial_commit_keeps_partial_status(self) -> None:
        # A superseded all-dropped run must persist the status the run actually
        # had ('partial'), not a hardcoded 'ok': the durable row and the
        # contract result must agree, or status-based telemetry misses the
        # all-dropped run. The audit row's last_processed_message_id stays 0,
        # so a 'partial' audit row still cannot lift the watermark.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            # A first run advances the watermark to 5.
            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar window one.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[5],
                    )
                ],
                [_unit_vector(1.0)],
                last_processed_message_id=5,
            )

            # A stale all-dropped run (observed watermark 0) commits 'partial'.
            commit_dream_cycle(
                db_path,
                [],
                agent_id=None,
                status="partial",
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
                messages_processed=1,
                last_processed_message_id=5,
                observed_watermark=0,
                candidates_dropped=2,
            )

            with closing(sqlite3.connect(db_path)) as conn:
                status, dropped, watermark_claim = conn.execute(
                    "SELECT status, candidates_dropped, last_processed_message_id"
                    " FROM dream_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()

            self.assertEqual(status, "partial")
            self.assertEqual(dropped, 2)
            self.assertEqual(watermark_claim, 0)
            self.assertEqual(get_watermark(db_path, agent_id=None), 5)

    def test_superseded_watermark_commit_does_not_double_write(self) -> None:
        # Finding 1: two Light runs reading the same watermark must not both
        # process the window. A commit whose observed watermark no longer
        # matches (a concurrent run advanced it between read and commit) aborts
        # as an audit-only no-op instead of re-writing candidates / re-advancing.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            # A first run advances the watermark to 5.
            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar window one.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[5],
                    )
                ],
                [_unit_vector(1.0)],
                last_processed_message_id=5,
            )
            self.assertEqual(get_watermark(db_path, agent_id=None), 5)

            # A caller observes watermark=5, but a concurrent run advances it to
            # 10 before the caller commits.
            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar window two.",
                        subject="Ryan",
                        category="goal",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[10],
                    )
                ],
                [_padded_vector(0.0, 1.0)],
                last_processed_message_id=10,
                started_at="2026-01-01T00:01:00Z",
            )
            self.assertEqual(get_watermark(db_path, agent_id=None), 10)

            # The stale caller commits with observed_watermark=5. It must NOT
            # write its candidate nor re-advance the watermark.
            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar superseded duplicate.",
                        subject="Ryan",
                        category="skill",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[3],
                    )
                ],
                [_padded_vector(0.0, 0.0, 1.0)],
                last_processed_message_id=5,
                observed_watermark=5,
                started_at="2026-01-01T00:02:00Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                candidate_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidates"
                ).fetchone()[0]
                superseded = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidates WHERE fact_text LIKE '%superseded%'"
                ).fetchone()[0]

            self.assertEqual(candidate_count, 2, "superseded commit must not write a third candidate")
            self.assertEqual(superseded, 0)
            self.assertEqual(
                get_watermark(db_path, agent_id=None),
                10,
                "superseded commit must not re-advance the watermark",
            )

    def test_matching_watermark_commit_proceeds(self) -> None:
        # Positive control for Finding 1: when the observed watermark still
        # matches, the compare-and-set lets the cycle write normally.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar first window.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[4],
                    )
                ],
                [_unit_vector(1.0)],
                last_processed_message_id=4,
                observed_watermark=0,
            )

            self.assertEqual(get_watermark(db_path, agent_id=None), 4)
            with closing(sqlite3.connect(db_path)) as conn:
                candidate_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidates"
                ).fetchone()[0]
            self.assertEqual(candidate_count, 1)

    def test_same_subject_duplicate_outside_global_topk_still_merges(self) -> None:
        # Finding 2: dedup must find the nearest MERGE-ELIGIBLE neighbor (same
        # subject+category), not the nearest global vector. Pad the store with
        # 12 other-subject candidates all CLOSER to the incoming vector than the
        # one same-subject candidate, so that same-subject row ranks ~13th
        # globally -- outside any top-10 KNN window. A KNN-before-filter dedup
        # would miss it and wrongly insert a duplicate; filter-first merges.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            # One same-subject "existing" candidate at cosine 0.86 (>= the 0.85
            # merge threshold, but the FARTHEST of everything staged).
            existing = FactCandidate(
                fact_text="Ryan enjoys trail running on weekends.",
                subject="Ryan",
                category="fact",
                importance=5,
                confidence=0.8,
                source_message_ids=[1],
            )
            existing_embedding = _cosine_vector(0.86)

            # 12 other-subject candidates at cosine 0.88..0.99 -- all closer to
            # the incoming [1, 0, ...] axis than the same-subject 0.86 row.
            other_cosines = [0.88 + 0.01 * i for i in range(12)]
            others = [
                FactCandidate(
                    fact_text=f"Subject {i} note.",
                    subject=f"Subject-{i}",
                    category="fact",
                    importance=5,
                    confidence=0.8,
                    source_message_ids=[100 + i],
                )
                for i in range(12)
            ]
            other_embeddings = [_cosine_vector(cos) for cos in other_cosines]

            self._commit(
                db_path,
                [existing, *others],
                [existing_embedding, *other_embeddings],
                last_processed_message_id=1,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                seeded = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
            self.assertEqual(seeded, 13)

            # Incoming candidate: same subject+category as `existing`, on the
            # [1, 0, ...] axis -> cosine 0.86 to the existing row.
            incoming = FactCandidate(
                fact_text="Ryan goes trail running most weekends.",
                subject="Ryan",
                category="fact",
                importance=5,
                confidence=0.8,
                source_message_ids=[2],
            )
            self._commit(
                db_path,
                [incoming],
                [_unit_vector(1.0)],
                last_processed_message_id=2,
                started_at="2026-01-01T00:01:00Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                total = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
                hit_count, source_ids = conn.execute(
                    """
                    SELECT hit_count, source_message_ids
                    FROM memory_candidates
                    WHERE subject = 'Ryan'
                    """
                ).fetchone()

            self.assertEqual(total, 13, "same-subject duplicate must merge, not insert a 14th row")
            self.assertEqual(hit_count, 2, "merge must reinforce the existing candidate")
            self.assertEqual(source_ids, "[1, 2]")

    def test_needs_review_candidate_cannot_win_promotion_claim(self) -> None:
        # Finding 3: a candidate flagged needs_review after selection must lose
        # the atomic promotion claim -- both at the claim primitive and end to
        # end, with no partial Tier 3 write.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar review candidate.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[1],
                    )
                ],
                [_unit_vector(1.0)],
                last_processed_message_id=1,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("UPDATE memory_candidates SET needs_review = 1 WHERE id = 1")
                conn.commit()

            # Direct claim: the WHERE guard must reject a needs_review row.
            with closing(connect(db_path)) as conn:
                with conn:
                    self.assertFalse(claim_candidate_for_promotion(conn, 1))

            # End to end: a promotion decision must abort cleanly, writing no
            # Tier 3 fact and leaving promoted = 0.
            commit_deep_cycle(
                db_path,
                [PromotionDecision(candidate_id=1, embedding=_unit_vector(1.0))],
                started_at="2026-01-01T00:01:00Z",
                finished_at="2026-01-01T00:01:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                fact_count = conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]
                promoted = conn.execute(
                    "SELECT promoted FROM memory_candidates WHERE id = 1"
                ).fetchone()[0]

            self.assertEqual(fact_count, 0, "needs_review candidate must not reach Tier 3")
            self.assertEqual(promoted, 0)

    def test_missing_embedding_loader_skips_non_live_candidates(self) -> None:
        # Finding 4: embedding repair targets only live staging candidates. A
        # promoted / retired / needs_review row missing an embedding must NOT be
        # returned for backfill (which could merge it into an active neighbor and
        # mutate an already-promoted row).
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            for text, category in (
                ("Ryan live candidate.", "fact"),
                ("Ryan promoted candidate.", "goal"),
                ("Ryan retired candidate.", "skill"),
                ("Ryan review candidate.", "context"),
            ):
                self._commit(
                    db_path,
                    [
                        FactCandidate(
                            fact_text=text,
                            subject="Ryan",
                            category=category,
                            importance=5,
                            confidence=0.8,
                            source_message_ids=[1],
                        )
                    ],
                    [_unit_vector(1.0)],
                    last_processed_message_id=1,
                )

            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                # Strip every embedding so the loader would return all four if it
                # did not filter by lifecycle.
                conn.execute("DELETE FROM memory_candidate_embeddings")
                conn.execute("UPDATE memory_candidates SET promoted = 1 WHERE id = 2")
                conn.execute("UPDATE memory_candidates SET retired = 1 WHERE id = 3")
                conn.execute("UPDATE memory_candidates SET needs_review = 1 WHERE id = 4")
                conn.commit()

            missing = load_candidates_missing_embeddings(db_path, agent_id=None)

            self.assertEqual(
                [candidate_id for candidate_id, _ in missing],
                [1],
                "only the live staging candidate is eligible for embedding repair",
            )

    def test_backfill_skips_candidate_whose_embedding_was_backfilled_concurrently(
        self,
    ) -> None:
        # Concurrent embedding backfill: two concurrent Light runs both load
        # candidate 1 as missing its embedding. Run A backfills it; run B then
        # calls backfill with its now-stale (candidate_id, embedding) list.
        # Backfill must re-check, under the write transaction, that the
        # candidate is still missing an embedding before mutating it. Otherwise
        # run B's _nearest_candidate matches candidate 1's own freshly-inserted
        # embedding (self-merge: hit_count drift, the live row wrongly staled).
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar window.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[1],
                    )
                ],
                [_unit_vector(1.0)],
                last_processed_message_id=1,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                # Strip the embedding so candidate 1 is eligible for repair.
                conn.execute("DELETE FROM memory_candidate_embeddings")
                conn.commit()

            # Both runs captured the same stale list before either wrote.
            missing = load_candidates_missing_embeddings(db_path, agent_id=None)
            self.assertEqual([cid for cid, _ in missing], [1])
            stale_list = [(1, _unit_vector(1.0))]

            # Run A repairs candidate 1.
            self.assertEqual(
                backfill_missing_candidate_embeddings(db_path, stale_list),
                1,
                "run A backfills the one missing embedding",
            )
            with closing(sqlite3.connect(db_path)) as conn:
                baseline_hit_count = conn.execute(
                    "SELECT hit_count FROM memory_candidates WHERE id = 1"
                ).fetchone()[0]

            # Run B replays the same stale list. Candidate 1 is no longer
            # missing its embedding, so backfill must skip it entirely.
            self.assertEqual(
                backfill_missing_candidate_embeddings(db_path, stale_list),
                0,
                "run B must skip the already-repaired candidate",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                embedding_rows = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidate_embeddings "
                    "WHERE candidate_id = 1"
                ).fetchone()[0]
                hit_count, stale = conn.execute(
                    "SELECT hit_count, stale FROM memory_candidates WHERE id = 1"
                ).fetchone()

            self.assertEqual(embedding_rows, 1, "candidate keeps exactly one embedding")
            self.assertEqual(
                hit_count, baseline_hit_count, "no self-merge hit_count drift"
            )
            self.assertEqual(
                stale, 0, "live candidate must not be staled by a stale replay"
            )

    def test_backfill_skips_candidate_staled_by_concurrent_run(self) -> None:
        # Backfill lifecycle recheck: a concurrent Light run may merge/stale (or
        # promote / retire / flag-for-review) the candidate between the loader
        # read and this backfill commit. Backfill must not embed a row that has
        # left live staging — that would resurrect an already-superseded
        # candidate.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            self._commit(
                db_path,
                [
                    FactCandidate(
                        fact_text="Ryan cedar window.",
                        subject="Ryan",
                        category="fact",
                        importance=5,
                        confidence=0.8,
                        source_message_ids=[1],
                    )
                ],
                [_unit_vector(1.0)],
                last_processed_message_id=1,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                conn.execute("DELETE FROM memory_candidate_embeddings")
                conn.commit()

            missing = load_candidates_missing_embeddings(db_path, agent_id=None)
            self.assertEqual([cid for cid, _ in missing], [1])
            stale_list = [(1, _unit_vector(1.0))]

            # A concurrent run staled the candidate after this run built its list.
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("UPDATE memory_candidates SET stale = 1 WHERE id = 1")
                conn.commit()

            self.assertEqual(
                backfill_missing_candidate_embeddings(db_path, stale_list),
                0,
                "a candidate no longer in live staging must be skipped",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                _load_vec_extension(conn)
                embedding_rows = conn.execute(
                    "SELECT COUNT(*) FROM memory_candidate_embeddings "
                    "WHERE candidate_id = 1"
                ).fetchone()[0]
            self.assertEqual(
                embedding_rows, 0, "a staled candidate must not be re-embedded"
            )


class LoadMessagesSinceTimestampTests(unittest.TestCase):
    """load_messages_since must surface each message's stored ISO-8601
    timestamp so the Light phase can eventually give the extraction agent
    per-message observed-time context, instead of the bare (id, message)
    pairs it returned before."""

    def test_load_messages_since_returns_iso_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I ran the race last Sunday")])],
                session_id="s1",
                agent_id=None,
                timestamp="2023-11-17T09:30:00+00:00",
            )

            rows = load_messages_since(db_path, 0)

            self.assertEqual(len(rows), 1)
            message_id, timestamp, msg = rows[0]
            self.assertIsInstance(message_id, int)
            self.assertEqual(timestamp, "2023-11-17T09:30:00+00:00")


class RenderTranscriptObservedTimeTests(unittest.TestCase):
    """render_transcript must label each rendered line with the message's
    observed date and weekday when a valid timestamp is available, and omit
    the label entirely when the timestamp is missing or malformed. The label
    is transient prompt scaffolding only (Memory Invariant 2)."""

    def test_render_transcript_labels_observed_date_and_weekday(self) -> None:
        rows = [(7, "2023-11-17T09:30:00+00:00", user_message("hello"))]
        self.assertEqual(
            render_transcript(rows),
            "[message_id=7 observed=2023-11-17 Fri] User: hello",
        )

    def test_render_transcript_omits_observed_when_timestamp_missing_or_malformed(
        self,
    ) -> None:
        rows = [
            (7, None, user_message("a")),
            (8, "not-a-date", user_message("b")),
        ]
        self.assertEqual(
            render_transcript(rows),
            "[message_id=7] User: a\n[message_id=8] User: b",
        )

    def test_rendered_message_ids_unchanged_semantics(self) -> None:
        rows = [(7, "2023-11-17T09:30:00+00:00", user_message("hello"))]
        self.assertEqual(rendered_message_ids(rows), [7])

    def test_render_transcript_fail_soft_on_non_string_timestamp(self) -> None:
        # SQLite can yield int/bytes timestamps from foreign writers; the
        # observed-time label must render unlabeled rather than raise.
        rows = [
            (7, 20231117, user_message("a")),
            (8, b"2023-11-17", user_message("b")),
        ]
        self.assertEqual(
            render_transcript(rows),
            "[message_id=7] User: a\n[message_id=8] User: b",
        )

    def test_plausible_years_fail_soft_on_non_string_timestamp(self) -> None:
        # _plausible_years slices timestamp[:10]; a foreign int/bytes value
        # must be skipped, not raise, so the year guard degrades to
        # transcript-literal grounding only.
        rows = [
            (7, 20231117, user_message("we met")),
            (8, b"2023-11-17", user_message("later")),
        ]
        self.assertEqual(_plausible_years(rows, "we met later"), set())
        rows_with_year = [(9, None, user_message("back in 2019"))]
        self.assertIn(2019, _plausible_years(rows_with_year, "back in 2019"))


class FactCandidateOccurredAtValidatorTests(unittest.TestCase):
    """FactCandidate.occurred_at validator accepts YYYY, YYYY-MM, YYYY-MM-DD
    with real calendar values, strips whitespace, and degrades junk to None
    (fail-safe; never drop a candidate for a bad date)."""

    def test_occurred_at_validator_accepts_partial_iso_and_nulls_junk(self) -> None:
        test_cases = [
            ("2023-11-17", "2023-11-17"),
            ("2023-11", "2023-11"),
            ("2023", "2023"),
            ("  2023-11 ", "2023-11"),
            ("", None),
            ("   ", None),
            ("March 2023", None),
            ("2023-13", None),
            ("2023-02-30", None),
            ("2023-11-17T09:00:00", "2023-11-17"),
            ("2026-07-05T00:00:00Z", "2026-07-05"),
            ("2026-07-05 09:30:00", "2026-07-05"),
            ("2026-02-30T00:00:00Z", None),
            ("9999-99-99T00:00:00", None),
            # The separator must be followed by a digit: a date-shaped prefix
            # with non-datetime trailing text is junk, not a truncatable value.
            ("2023-09-24Tnot-a-datetime", None),
            ("2023-09-24 not-a-datetime", None),
            ("2023-09-24T", None),
        ]
        for raw, expected in test_cases:
            with self.subTest(raw=raw):
                c = FactCandidate(
                    fact_text="x",
                    subject="user",
                    category="event",
                    importance=5,
                    confidence=0.9,
                    occurred_at=raw,
                )
                self.assertEqual(c.occurred_at, expected)

    def test_occurred_at_revalidated_on_assignment(self) -> None:
        # validate_assignment: a post-construction assignment of an invalid
        # date must re-run the validator and degrade to None, not smuggle the
        # bad value onto the row.
        c = FactCandidate(
            fact_text="x",
            subject="user",
            category="event",
            importance=5,
            confidence=0.9,
            occurred_at="2023-11-17",
        )
        c.occurred_at = "2023-02-30"
        self.assertIsNone(c.occurred_at)
        # A valid reassignment survives, and None (the guard's canonical
        # "undated" assignment) is accepted.
        c.occurred_at = "2024-01"
        self.assertEqual(c.occurred_at, "2024-01")
        c.occurred_at = None
        self.assertIsNone(c.occurred_at)


def _event_candidate(**overrides: object) -> FactCandidate:
    fields: dict[str, object] = {
        "fact_text": "Ryan did something.",
        "subject": "Ryan",
        "category": "event",
        "importance": 5,
        "confidence": 0.8,
        "source_message_ids": [1],
    }
    fields.update(overrides)
    return FactCandidate(**fields)


def _rows_nov_2023() -> list[tuple[int, str, ModelRequest]]:
    return [(1, "2023-11-17T09:00:00+00:00", user_message("we talked"))]


class OccurredAtGuardTests(unittest.TestCase):
    """apply_occurred_at_guards is the deterministic anti-fabrication layer
    for Tier 2 event candidates (ADR 0038): a year-plausibility
    check that nulls out occurred_at years unmoored from the transcript
    window, and an in-text date copy-backfill for event candidates the model
    left undated but which state exactly one absolute date in fact_text."""

    def test_guard_nulls_year_not_grounded_in_window(self) -> None:
        c = _event_candidate(occurred_at="2025-03-01")
        apply_occurred_at_guards(
            [c],
            _rows_nov_2023(),
            "[message_id=1 observed=2023-11-17 Fri] User: we talked",
        )
        self.assertIsNone(c.occurred_at)

    def test_guard_keeps_observed_year_and_adjacent_years(self) -> None:
        for kept in ("2023-03-01", "2022-12", "2024"):
            with self.subTest(kept=kept):
                c = _event_candidate(occurred_at=kept)
                apply_occurred_at_guards([c], _rows_nov_2023(), "irrelevant")
                self.assertEqual(c.occurred_at, kept)

    def test_guard_keeps_year_stated_in_transcript(self) -> None:
        c = _event_candidate(occurred_at="1999")
        apply_occurred_at_guards(
            [c], _rows_nov_2023(), "User: I graduated in 1999"
        )
        self.assertEqual(c.occurred_at, "1999")

    def test_guard_copies_single_intext_absolute_date_into_occurred_at(self) -> None:
        c = _event_candidate(
            fact_text="User ran the Berlin half on 2023-09-24", occurred_at=None
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.occurred_at, "2023-09-24")

    def test_guard_copies_month_year_at_stated_precision(self) -> None:
        c = _event_candidate(
            fact_text="User moved to Lisbon in March 2023", occurred_at=None
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.occurred_at, "2023-03")

    def test_guard_skips_copy_when_multiple_or_zero_dates(self) -> None:
        c = _event_candidate(
            fact_text="Trips on 2023-05-01 and 2023-06-01", occurred_at=None
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertIsNone(c.occurred_at)

    def test_guard_never_copies_for_non_event_categories(self) -> None:
        c = _event_candidate(
            fact_text="Prefers the 2023-09-24 build",
            occurred_at=None,
            category="preference",
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertIsNone(c.occurred_at)

    def test_guard_copies_full_month_day_year_date(self) -> None:
        c = _event_candidate(
            fact_text="User ran the Berlin half on September 24, 2023",
            occurred_at=None,
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.occurred_at, "2023-09-24")

    def test_guard_rejects_calendar_invalid_intext_date(self) -> None:
        c = _event_candidate(
            fact_text="User claimed it happened on February 30, 2023",
            occurred_at=None,
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertIsNone(c.occurred_at)

    def test_guard_nulls_backfilled_date_with_implausible_year(self) -> None:
        # fact_text is model output too: a fabricated year copied in by the
        # backfill must not escape the same year-plausibility check a
        # model-supplied occurred_at gets.
        c = _event_candidate(
            fact_text="User ran the Berlin race on March 1, 2025",
            occurred_at=None,
        )
        apply_occurred_at_guards(
            [c],
            _rows_nov_2023(),
            "[message_id=1 observed=2023-11-17 Fri] User: we talked",
        )
        self.assertIsNone(c.occurred_at)

    def test_guard_keeps_backfilled_date_with_plausible_observed_year(self) -> None:
        # Positive control: a legitimately copied date's year sits inside the
        # observed window and must survive the re-check.
        c = _event_candidate(
            fact_text="User ran the Berlin race on September 24, 2023",
            occurred_at=None,
        )
        apply_occurred_at_guards(
            [c],
            _rows_nov_2023(),
            "[message_id=1 observed=2023-11-17 Fri] User: we talked",
        )
        self.assertEqual(c.occurred_at, "2023-09-24")

    def test_guard_keeps_backfilled_date_with_year_literal_in_transcript(self) -> None:
        # Positive control: a copied date's year grounded in the transcript
        # text (not the observed window) must also survive the re-check.
        c = _event_candidate(
            fact_text="Graduated May 2019",
            occurred_at=None,
        )
        apply_occurred_at_guards(
            [c],
            _rows_nov_2023(),
            "[message_id=1 observed=2023-11-17 Fri] User: I mentioned 2019 before",
        )
        self.assertEqual(c.occurred_at, "2019-05")

    def test_guard_ignores_marker_message_id_as_grounding_year(self) -> None:
        # A 4-digit message_id inside a [message_id=...] marker must not
        # ground a year: markers are transient scaffolding, not transcript
        # text. Here observed=2024 grounds 2023-2025; 1999 (the marker id)
        # must not.
        c = _event_candidate(occurred_at="1999")
        apply_occurred_at_guards(
            [c],
            [(1, "2024-01-10T09:00:00+00:00", user_message("we talked"))],
            "[message_id=1999 observed=2024-01-10 Wed] User: we talked",
        )
        self.assertIsNone(c.occurred_at)

    def test_guard_grounds_bare_year_in_user_text_despite_marker(self) -> None:
        # Positive control for the marker strip: a bare year in the user's own
        # text still grounds, even with a marker on the same line.
        c = _event_candidate(occurred_at="1999")
        apply_occurred_at_guards(
            [c],
            [(1, "2024-01-10T09:00:00+00:00", user_message("we talked"))],
            "[message_id=5 observed=2024-01-10 Wed] User: I graduated in 1999",
        )
        self.assertEqual(c.occurred_at, "1999")

    def test_guard_ignores_lowercase_modal_may_year(self) -> None:
        # Modal "may 2024" is not a month reference; the month regex is
        # case-sensitive so lowercase "may" degrades safely to undated.
        c = _event_candidate(
            fact_text="User said they may 2024 relocate", occurred_at=None
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertIsNone(c.occurred_at)

    def test_guard_copies_capitalized_may_year(self) -> None:
        # Capitalized month usage still backfills.
        c = _event_candidate(
            fact_text="User relocated in May 2024", occurred_at=None
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.occurred_at, "2024-05")

    def test_guard_caps_occurred_at_to_intext_precision(self) -> None:
        # Model emits a full date but fact_text only states month precision:
        # truncate to the in-text precision (precision reduction, never
        # extension).
        c = _event_candidate(
            fact_text="Ryan moved in March 2023", occurred_at="2023-03-01"
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.occurred_at, "2023-03")

    def test_guard_keeps_equal_precision_intext_date(self) -> None:
        # In-text date at equal precision: no cap.
        c = _event_candidate(
            fact_text="Ryan moved on March 14, 2023", occurred_at="2023-03-14"
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.occurred_at, "2023-03-14")

    def test_guard_precision_cap_requires_intext_date(self) -> None:
        # No in-text date to compare against: occurred_at is left untouched.
        c = _event_candidate(
            fact_text="Ryan did something.", occurred_at="2023-05-01"
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.occurred_at, "2023-05-01")

    def test_guard_strips_marker_echo_from_fact_text(self) -> None:
        # An extractor that echoes a [message_id=... observed=...] marker into
        # fact_text must not persist it into Tier 2 text/FTS. The marker is
        # stripped and whitespace collapsed before embedding/commit.
        c = _event_candidate(
            fact_text="[message_id=3 observed=2023-11-17 Fri] User prefers uv",
            occurred_at=None,
            category="preference",
        )
        apply_occurred_at_guards([c], _rows_nov_2023(), "...")
        self.assertEqual(c.fact_text, "User prefers uv")


if __name__ == "__main__":
    unittest.main()
