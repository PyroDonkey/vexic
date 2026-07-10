"""Conformance tests for the LongMemEval harness in vexic.longmemeval."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vexic.longmemeval import (
    build_parser,
    main as longmemeval_main,
    LongMemEvalRecallJudgeInput,
    LongMemEvalRecallJudgeVerdict,
    _render_recall_judge_input,
    _select_instances,
    drain_light_then_consolidate,
    drain_light_then_rem,
    ingest_instance,
    create_run_paths,
    parse_longmemeval_instance,
    question_db_path,
    run_longmemeval_subset,
)
from vexic.embeddings import EMBEDDING_DIM
from vexic.models import FactCandidate
from vexic.storage import (
    CandidateNote,
    LongTermFact,
    commit_dream_cycle,
    init_db,
    save_messages,
    search_messages,
)


def _basis_vector(axis: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    return vector


class _FakeRecallJudge:
    def __init__(self, verdict: LongMemEvalRecallJudgeVerdict) -> None:
        self.verdict = verdict
        self.calls: list[LongMemEvalRecallJudgeInput] = []

    async def __call__(
        self,
        judge_input: LongMemEvalRecallJudgeInput,
    ) -> LongMemEvalRecallJudgeVerdict:
        self.calls.append(judge_input)
        return self.verdict


class TimestampedTranscriptIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_save_messages_can_insert_benchmark_timestamp_with_fts_and_redaction(self) -> None:
        timestamp = "2026-01-02T03:04:05+00:00"

        save_messages(
            self.db_path,
            [
                ModelRequest(parts=[UserPromptPart(content="My benchmark code is cedar.")]),
                ModelResponse(parts=[TextPart(content="I will remember cedar.")]),
            ],
            session_id="longmemeval:q1:s1",
            timestamp=timestamp,
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT timestamp FROM messages ORDER BY id ASC"
            ).fetchall()

        self.assertEqual(rows, [(timestamp,), (timestamp,)])
        hits = search_messages(
            self.db_path,
            "cedar",
            session_id="longmemeval:q1:s1",
        )
        self.assertEqual(len(hits), 2)
        self.assertEqual({hit.timestamp for hit in hits}, {timestamp})

        with self.assertRaisesRegex(ValueError, "forbidden secret"):
            save_messages(
                self.db_path,
                [ModelRequest(parts=[UserPromptPart(content="secret-token")])],
                session_id="longmemeval:q1:s2",
                timestamp=timestamp,
                forbidden_secret_values=["secret-token"],
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]

        self.assertEqual(count, 2)
        self.assertEqual(fts_count, 2)


class LongMemEvalSanitizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ingest_strips_evaluator_labels_before_persisting_transcript(self) -> None:
        raw = {
            "question_id": "q-labels",
            "question_type": "single-session-user",
            "question": "What code did the user choose?",
            "answer": "top-level oracle answer",
            "question_date": "2026-01-03",
            "answer_session_ids": ["session-1"],
            "haystack_session_ids": ["session-1"],
            "haystack_dates": ["2026-01-02T03:04:05Z"],
            "haystack_sessions": [
                [
                    {
                        "role": "user",
                        "content": "My benchmark code is cedar.",
                        "has_answer": True,
                    },
                    {
                        "role": "assistant",
                        "content": "I will remember cedar.",
                        "has_answer": False,
                    },
                ]
            ],
        }

        instance = parse_longmemeval_instance(raw)
        ingest_instance(self.db_path, instance)

        with closing(sqlite3.connect(self.db_path)) as conn:
            persisted = "\n".join(
                row[0]
                for row in conn.execute(
                    "SELECT message_json FROM messages ORDER BY id ASC"
                ).fetchall()
            )
            timestamps = conn.execute(
                "SELECT DISTINCT timestamp FROM messages"
            ).fetchall()

        self.assertIn("cedar", persisted)
        self.assertNotIn("top-level oracle answer", persisted)
        self.assertNotIn("answer_session_ids", persisted)
        self.assertNotIn("has_answer", persisted)
        self.assertEqual(timestamps, [("2026-01-02T03:04:05+00:00",)])

    def test_ingest_persists_sessions_in_chronological_order(self) -> None:
        raw = {
            "question_id": "q-chronology",
            "question_type": "knowledge-update",
            "question": "Which plan is current?",
            "answer": "current plan",
            "question_date": "2026-01-04",
            "answer_session_ids": ["session-new"],
            "haystack_session_ids": ["session-new", "session-old"],
            "haystack_dates": [
                "2026-01-03T09:00:00Z",
                "2026-01-01T09:00:00-05:00",
            ],
            "haystack_sessions": [
                [{"role": "user", "content": "The current plan is blue."}],
                [{"role": "user", "content": "The old plan was amber."}],
            ],
        }

        instance = parse_longmemeval_instance(raw)
        ingest_instance(self.db_path, instance)

        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT session_id, timestamp FROM messages ORDER BY id ASC"
            ).fetchall()

        self.assertEqual(
            rows,
            [
                (
                    "longmemeval:q-chronology:session-old",
                    "2026-01-01T14:00:00+00:00",
                ),
                (
                    "longmemeval:q-chronology:session-new",
                    "2026-01-03T09:00:00+00:00",
                ),
            ],
        )

    def test_parse_accepts_cleaned_longmemeval_timestamp_format(self) -> None:
        raw = {
            "question_id": "q-cleaned-date",
            "question_type": "single-session-user",
            "question": "What code did the user choose?",
            "answer": "cedar",
            "question_date": "2023/05/21 (Sun) 08:00",
            "answer_session_ids": ["session-1"],
            "haystack_session_ids": ["session-1"],
            "haystack_dates": ["2023/05/20 (Sat) 02:21"],
            "haystack_sessions": [
                [{"role": "user", "content": "The code is cedar."}],
            ],
        }

        instance = parse_longmemeval_instance(raw)

        self.assertEqual(instance.sessions[0].timestamp, "2023-05-20T02:21:00+00:00")


class LongMemEvalIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name) / "eval-runs"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _instance(self, question_id: str, detail: str) -> object:
        return parse_longmemeval_instance(
            {
                "question_id": question_id,
                "question_type": "single-session-user",
                "question": "What detail was mentioned?",
                "answer": detail,
                "question_date": "2026-01-03",
                "answer_session_ids": ["session-1"],
                "haystack_session_ids": ["session-1"],
                "haystack_dates": ["2026-01-02T03:04:05Z"],
                "haystack_sessions": [
                    [{"role": "user", "content": f"The detail is {detail}."}]
                ],
            }
        )

    def test_each_question_uses_an_isolated_memory_database(self) -> None:
        paths = create_run_paths(self.output_dir, run_id="test-run")
        cedar_db = question_db_path(paths.run_dir, "q/cedar")
        ruby_db = question_db_path(paths.run_dir, "q/ruby")

        ingest_instance(str(cedar_db), self._instance("q/cedar", "cedar"))
        ingest_instance(str(ruby_db), self._instance("q/ruby", "ruby"))

        self.assertNotEqual(cedar_db, ruby_db)
        self.assertTrue(cedar_db.exists())
        self.assertTrue(ruby_db.exists())
        cedar_session = "longmemeval:q/cedar:session-1"
        ruby_session = "longmemeval:q/ruby:session-1"
        self.assertEqual(search_messages(str(cedar_db), "ruby", session_id=cedar_session), [])
        self.assertEqual(search_messages(str(ruby_db), "cedar", session_id=ruby_session), [])
        self.assertEqual(len(search_messages(str(cedar_db), "cedar", session_id=cedar_session)), 1)
        self.assertEqual(len(search_messages(str(ruby_db), "ruby", session_id=ruby_session)), 1)

    def test_question_database_paths_do_not_collide_for_lossy_safe_names(self) -> None:
        paths = create_run_paths(self.output_dir, run_id="collision-run")

        slash_db = question_db_path(paths.run_dir, "q/a")
        colon_db = question_db_path(paths.run_dir, "q:a")

        self.assertNotEqual(slash_db, colon_db)
        self.assertTrue(slash_db.parent.exists())
        self.assertTrue(colon_db.parent.exists())

    def test_duplicate_question_id_fails_before_reusing_database_directory(self) -> None:
        paths = create_run_paths(self.output_dir, run_id="duplicate-run")

        question_db_path(paths.run_dir, "q-duplicate")

        with self.assertRaisesRegex(ValueError, "already exists"):
            question_db_path(paths.run_dir, "q-duplicate")


class LongMemEvalDreamDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_light_drains_to_watermark_fixpoint_before_rem_and_deep(self) -> None:
        light = AsyncMock()
        rem = AsyncMock()
        deep = AsyncMock()

        with (
            patch("vexic.longmemeval.get_watermark", side_effect=[0, 50, 100, 100]),
            patch("vexic.longmemeval.run_light_phase", light),
            patch("vexic.longmemeval.run_rem_phase", rem),
            patch("vexic.longmemeval.run_deep_phase", deep),
        ):
            result = await drain_light_then_consolidate(
                "memory.db",
                "glm",
                message_count=100,
                deep_top_n=7,
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.light_cycles, 3)
        self.assertIsNotNone(result.candidate_scoring_time)
        self.assertEqual(light.await_count, 3)
        rem.assert_awaited_once_with("memory.db")
        deep.assert_awaited_once()
        self.assertEqual(deep.await_args.args, ("memory.db", "glm"))
        self.assertEqual(deep.await_args.kwargs["secrets"], None)
        self.assertEqual(deep.await_args.kwargs["top_n"], 7)
        self.assertIsNotNone(deep.await_args.kwargs["now"])

    async def test_light_drain_stops_incomplete_when_watermark_never_stabilizes(self) -> None:
        light = AsyncMock()
        rem = AsyncMock()
        deep = AsyncMock()

        with (
            patch("vexic.longmemeval.get_watermark", side_effect=[0, 1, 2]),
            patch("vexic.longmemeval.run_light_phase", light),
            patch("vexic.longmemeval.run_rem_phase", rem),
            patch("vexic.longmemeval.run_deep_phase", deep),
        ):
            result = await drain_light_then_consolidate(
                "memory.db",
                "glm",
                message_count=100,
                max_light_cycles=2,
            )

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.light_cycles, 2)
        self.assertFalse(result.rem_ran)
        self.assertFalse(result.deep_ran)
        rem.assert_not_awaited()
        deep.assert_not_awaited()

    async def test_light_drain_can_stop_after_rem_for_tier2_diagnostics(self) -> None:
        light = AsyncMock()
        rem = AsyncMock()
        deep = AsyncMock()

        with (
            patch("vexic.longmemeval.get_watermark", side_effect=[0, 50, 50]),
            patch("vexic.longmemeval.run_light_phase", light),
            patch("vexic.longmemeval.run_rem_phase", rem),
            patch("vexic.longmemeval.run_deep_phase", deep),
        ):
            result = await drain_light_then_rem(
                "memory.db",
                "glm",
                message_count=50,
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.light_cycles, 2)
        self.assertTrue(result.rem_ran)
        self.assertFalse(result.deep_ran)
        self.assertIsNotNone(result.candidate_scoring_time)
        rem.assert_awaited_once_with("memory.db")
        deep.assert_not_awaited()

    async def test_drain_passes_host_ports_through_to_phases(self) -> None:
        light = AsyncMock()
        rem = AsyncMock()
        deep = AsyncMock()
        extraction_factory = object()
        contradiction_factory = object()
        embed = object()

        with (
            patch("vexic.longmemeval.get_watermark", side_effect=[0, 0]),
            patch("vexic.longmemeval.run_light_phase", light),
            patch("vexic.longmemeval.run_rem_phase", rem),
            patch("vexic.longmemeval.run_deep_phase", deep),
        ):
            result = await drain_light_then_consolidate(
                "memory.db",
                "glm",
                message_count=10,
                extraction_agent_factory=extraction_factory,
                embed=embed,
                contradiction_agent_factory=contradiction_factory,
            )

        self.assertEqual(result.status, "ok")
        self.assertIs(
            light.await_args.kwargs["extraction_agent_factory"], extraction_factory
        )
        self.assertIs(light.await_args.kwargs["embed"], embed)
        self.assertIs(
            deep.await_args.kwargs["contradiction_agent_factory"],
            contradiction_factory,
        )


def _fake_dream_result(**overrides: object) -> object:
    fields: dict[str, object] = {
        "status": "ok",
        "light_cycles": 1,
        "rem_ran": True,
        "deep_ran": True,
        "final_watermark": 1,
        "error": None,
    }
    fields.update(overrides)
    return type("DreamResult", (), fields)()


class LongMemEvalArtifactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _dataset_row(self, question_id: str, question_type: str, detail: str) -> dict:
        return {
            "question_id": question_id,
            "question_type": question_type,
            "question": "What benchmark code was mentioned?",
            "answer": detail,
            "question_date": "2026-01-03",
            "answer_session_ids": ["session-1"],
            "haystack_session_ids": ["session-1"],
            "haystack_dates": ["2026-01-02T03:04:05Z"],
            "haystack_sessions": [
                [
                    {
                        "role": "user",
                        "content": f"The benchmark code was {detail}.",
                    }
                ]
            ],
        }

    async def test_subset_can_select_a_stratified_sample_by_question_type(self) -> None:
        rows = []
        for question_type in ("single-session-user", "multi-session", "knowledge-update"):
            for index in range(3):
                rows.append(
                    self._dataset_row(
                        f"{question_type}-{index}",
                        question_type,
                        f"code-{question_type}-{index}",
                    )
                )
        dataset_path = self.root / "longmemeval_s_cleaned.json"
        dataset_path.write_text(json.dumps(rows), encoding="utf-8")

        with patch(
            "vexic.longmemeval.drain_light_then_consolidate",
            new=AsyncMock(return_value=_fake_dream_result()),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="s",
                output_dir=self.root / "runs",
                limit=6,
                model_group="glm",
                selection="stratified",
            )

        diagnostics = [
            json.loads(line)
            for line in summary.paths.diagnostics_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertEqual(summary.questions_started, 6)
        self.assertEqual(
            [row["question_id"] for row in diagnostics],
            [
                "single-session-user-0",
                "multi-session-0",
                "knowledge-update-0",
                "single-session-user-1",
                "multi-session-1",
                "knowledge-update-1",
            ],
        )
        self.assertEqual(
            [row["question_type"] for row in diagnostics],
            [
                "single-session-user",
                "multi-session",
                "knowledge-update",
                "single-session-user",
                "multi-session",
                "knowledge-update",
            ],
        )

    async def test_subset_rejects_non_positive_dream_session_batch_size(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "dream_session_batch_size must be at least 1",
        ):
            await run_longmemeval_subset(
                self.root / "missing.json",
                split="s",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                dream_session_batch_size=0,
            )

    def test_subset_rejects_unsupported_selection(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported LongMemEval selection: random",
        ):
            _select_instances(
                [{"question_id": "q-1"}],
                limit=1,
                selection="random",
            )

    async def test_subset_can_retry_specific_question_ids(self) -> None:
        rows = [
            self._dataset_row(f"q-{index}", "single-session-user", f"code-{index}")
            for index in range(3)
        ]
        dataset_path = self.root / "longmemeval_s_cleaned.json"
        dataset_path.write_text(json.dumps(rows), encoding="utf-8")

        with patch(
            "vexic.longmemeval.drain_light_then_consolidate",
            new=AsyncMock(return_value=_fake_dream_result()),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="s",
                output_dir=self.root / "runs",
                limit=3,
                model_group="glm",
                question_ids=("q-1", "q-2"),
            )

        diagnostics = [
            json.loads(line)
            for line in summary.paths.diagnostics_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertEqual(summary.questions_started, 2)
        self.assertEqual([row["question_id"] for row in diagnostics], ["q-1", "q-2"])

    async def test_subset_rejects_question_ids_missing_from_selected_subset(self) -> None:
        rows = [
            self._dataset_row(f"q-{index}", "single-session-user", f"code-{index}")
            for index in range(3)
        ]
        dataset_path = self.root / "longmemeval_s_cleaned.json"
        dataset_path.write_text(json.dumps(rows), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "q-2.*not in the selected subset"):
            await run_longmemeval_subset(
                dataset_path,
                split="s",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                selection="first",
                question_ids=("q-0", "q-2"),
            )

    async def test_subset_can_resume_after_completed_rows_from_prior_run(self) -> None:
        rows = [
            self._dataset_row(f"q-{index}", "single-session-user", f"code-{index}")
            for index in range(3)
        ]
        dataset_path = self.root / "longmemeval_s_cleaned.json"
        dataset_path.write_text(json.dumps(rows), encoding="utf-8")
        prior_run = self.root / "runs" / "prior"
        prior_run.mkdir(parents=True)
        prior_diagnostics = prior_run / "diagnostics.jsonl"
        prior_diagnostics.write_text(
            "\n".join(
                [
                    json.dumps({"question_id": "q-0", "status": "ok"}),
                    json.dumps({"question_id": "q-1", "status": "error"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch(
            "vexic.longmemeval.drain_light_then_consolidate",
            new=AsyncMock(return_value=_fake_dream_result()),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="s",
                output_dir=self.root / "runs",
                limit=3,
                model_group="glm",
                resume_from_run=prior_run,
            )

        diagnostics = [
            json.loads(line)
            for line in summary.paths.diagnostics_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertEqual(summary.questions_started, 2)
        self.assertEqual([row["question_id"] for row in diagnostics], ["q-1", "q-2"])

    async def test_subset_smoke_writes_prediction_and_diagnostics_jsonl(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps([self._dataset_row("q-artifact", "single-session-user", "cedar")]),
            encoding="utf-8",
        )

        with patch(
            "vexic.longmemeval.drain_light_then_consolidate",
            new=AsyncMock(return_value=_fake_dream_result()),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
            )

        predictions = summary.paths.predictions_path.read_text(encoding="utf-8")
        diagnostics = summary.paths.diagnostics_path.read_text(encoding="utf-8")

        self.assertIn('"question_id": "q-artifact"', predictions)
        self.assertIn('"hypothesis":', predictions)
        self.assertIn("cedar", predictions)
        self.assertIn('"split": "oracle"', diagnostics)
        self.assertIn('"status": "ok"', diagnostics)
        self.assertIn('"deep_top_n": 15', diagnostics)
        self.assertIn('"candidate_fallback_used": false', diagnostics)

    async def test_subset_tier3_debug_dreams_then_retrieves_long_term_facts(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps([self._dataset_row("q-tier3", "single-session-user", "cedar")]),
            encoding="utf-8",
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        facts = [
            LongTermFact(
                fact_id=7,
                fact_text="The benchmark code was cedar.",
                subject="benchmark",
                category="fact",
                importance=5,
                confidence=0.9,
                source_message_ids=[1],
                retrieved_count=0,
                used_count=0,
            )
        ]
        retrieve = AsyncMock(return_value=facts)
        secrets = {"OPENROUTER_API_KEY": "tenant-openrouter"}

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="claude",
                answer_mode="tier3-debug",
                secrets=secrets,
                deep_top_n=3,
            )

        drain.assert_awaited_once()
        retrieve.assert_awaited_once()
        self.assertTrue(
            retrieve.await_args.args[0].endswith("memory.db"),
            retrieve.await_args.args[0],
        )
        self.assertEqual(retrieve.await_args.args[1], "What benchmark code was mentioned?")
        self.assertEqual(retrieve.await_args.kwargs["model_group"], "claude")
        self.assertEqual(retrieve.await_args.kwargs["secrets"], secrets)
        self.assertEqual(retrieve.await_args.kwargs["session_id"], "longmemeval:q-tier3:answer")

        prediction = json.loads(
            summary.paths.predictions_path.read_text(encoding="utf-8")
        )
        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertIn("[fact 7]", prediction["hypothesis"])
        self.assertIn("cedar", prediction["hypothesis"])
        self.assertEqual(diagnostics["answer_mode"], "tier3-debug")
        self.assertFalse(diagnostics["dream_skipped"])
        self.assertEqual(diagnostics["deep_top_n"], 3)
        self.assertEqual(diagnostics["retrieved_long_term_fact_count"], 1)
        self.assertTrue(diagnostics["answer_found_in_tier1"])
        self.assertTrue(diagnostics["answer_retrieved_from_tier3"])

    async def test_subset_tier3_debug_returns_explicit_empty_when_no_facts_retrieved(
        self,
    ) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [self._dataset_row("q-tier3-empty", "single-session-user", "cedar")]
            ),
            encoding="utf-8",
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=[])
        fallback = AsyncMock(return_value=[])

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
            patch("vexic.longmemeval.retrieve_candidate_fallback", new=fallback),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="claude",
                answer_mode="tier3-debug",
            )

        prediction = json.loads(
            summary.paths.predictions_path.read_text(encoding="utf-8")
        )
        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertEqual(prediction["hypothesis"], "No long-term memories found.")
        self.assertEqual(diagnostics["retrieved_long_term_fact_count"], 0)
        self.assertEqual(diagnostics["retrieved_candidate_note_count"], 0)
        self.assertFalse(diagnostics["candidate_fallback_used"])
        fallback.assert_not_awaited()

    async def test_judged_recall_uses_candidate_fallback_when_tier3_is_empty(
        self,
    ) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [self._dataset_row("q-judged-fallback", "single-session-user", "cedar")]
            ),
            encoding="utf-8",
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=[])
        fallback = AsyncMock(
            return_value=[
                CandidateNote(
                    candidate_id=1,
                    fact_text="The benchmark code was cedar.",
                    category="fact",
                    source_message_ids=[1],
                    created_at="2026-01-02T03:04:05+00:00",
                )
            ]
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="The retrieved note states the benchmark code.",
                confidence=0.95,
            )
        )

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
            patch("vexic.longmemeval.retrieve_candidate_fallback", new=fallback),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="claude",
                answer_mode="judged-recall",
                judge_scorer=judge,
            )

        prediction = json.loads(
            summary.paths.predictions_path.read_text(encoding="utf-8")
        )
        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertIn("[unverified note]", prediction["hypothesis"])
        self.assertIn("The benchmark code was cedar.", prediction["hypothesis"])
        self.assertTrue(diagnostics["candidate_fallback_used"])
        self.assertEqual(diagnostics["retrieved_long_term_fact_count"], 0)
        self.assertEqual(diagnostics["retrieved_candidate_note_count"], 1)
        self.assertFalse(diagnostics["answer_retrieved_from_tier3"])
        self.assertEqual(
            judge.calls[0].retrieved_fact_texts,
            (
                "[unverified note] The benchmark code was cedar.\n"
                "(category: fact, recently noted, not yet confirmed, source messages: 1)",
            ),
        )

    async def test_subset_retrieval_debug_does_not_call_tier3_retrieval(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [self._dataset_row("q-tier1-only", "single-session-user", "cedar")]
            ),
            encoding="utf-8",
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock()

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="claude",
                answer_mode="retrieval-debug",
            )

        retrieve.assert_not_awaited()
        prediction = json.loads(
            summary.paths.predictions_path.read_text(encoding="utf-8")
        )

        self.assertIn("cedar", prediction["hypothesis"])

    async def test_subset_refuses_artifacts_bearing_loaded_model_secrets(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [self._dataset_row("q-secret", "single-session-user", "secret-token")]
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "forbidden secret"):
            await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                secrets={"OPENROUTER_API_KEY": "secret-token"},
            )

    async def test_subset_skip_dream_refuses_secret_bearing_artifacts(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [self._dataset_row("q-secret", "single-session-user", "secret-token")]
            ),
            encoding="utf-8",
        )
        drain = AsyncMock()

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            self.assertRaisesRegex(ValueError, "forbidden secret"),
        ):
            await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                forbidden_secret_values=["secret-token"],
                skip_dream=True,
            )

        drain.assert_not_awaited()

    async def test_subset_smoke_records_malformed_row_and_continues(self) -> None:
        good_row = self._dataset_row("q-good", "single-session-user", "cedar")
        bad_row = self._dataset_row("q-bad", "single-session-user", "bad")
        del bad_row["question_id"]
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(json.dumps([bad_row, good_row]), encoding="utf-8")

        with patch(
            "vexic.longmemeval.drain_light_then_consolidate",
            new=AsyncMock(return_value=_fake_dream_result()),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=2,
                model_group="glm",
            )

        predictions = [
            json.loads(line)
            for line in summary.paths.predictions_path.read_text(encoding="utf-8").splitlines()
        ]
        diagnostics = [
            json.loads(line)
            for line in summary.paths.diagnostics_path.read_text(encoding="utf-8").splitlines()
        ]

        self.assertEqual(summary.questions_started, 2)
        self.assertEqual(summary.questions_completed, 1)
        self.assertEqual(summary.questions_failed, 1)
        self.assertEqual(len(predictions), 2)
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(predictions[0]["question_id"], "row-1:<unknown>")
        self.assertEqual(diagnostics[0]["question_id"], "row-1:<unknown>")
        self.assertEqual(diagnostics[0]["status"], "error")
        self.assertIn("requires non-empty", diagnostics[0]["error"])
        self.assertEqual(predictions[1]["question_id"], "q-good")
        self.assertEqual(diagnostics[1]["question_id"], "q-good")
        self.assertEqual(diagnostics[1]["status"], "ok")


class LongMemEvalJudgedRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_recall_judge_render_preserves_unverified_note_label(self) -> None:
        rendered = _render_recall_judge_input(
            LongMemEvalRecallJudgeInput(
                question="What benchmark code was mentioned?",
                gold_answer="cedar",
                retrieved_fact_texts=(
                    "[unverified note] The benchmark code was cedar.\n"
                    "(category: fact, recently noted, not yet confirmed, source messages: 1)",
                ),
            )
        )

        self.assertIn("[unverified note] The benchmark code was cedar.", rendered)
        self.assertNotIn("[fact 1] [unverified note]", rendered)

    async def _run_case(
        self,
        *,
        question_id: str,
        question_type: str,
        question: str,
        answer: str,
        transcript: str,
        facts: list[LongTermFact],
        judge_verdict: LongMemEvalRecallJudgeVerdict,
        judge_model_group: str = "claude",
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        _FakeRecallJudge,
        object,
    ]:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": question_id,
                        "question_type": question_type,
                        "question": question,
                        "answer": answer,
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": transcript}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=facts)
        judge = _FakeRecallJudge(judge_verdict)

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group=judge_model_group,
                judge_scorer=judge,
            )

        prediction = json.loads(
            summary.paths.predictions_path.read_text(encoding="utf-8")
        )
        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )
        return prediction, diagnostics, judge, summary

    async def test_judged_recall_supports_reformatted_duration_answer(self) -> None:
        fact = LongTermFact(
            fact_id=1,
            fact_text="Ryan's personal best 5K is 25:50.",
            subject="Ryan",
            category="fact",
            importance=7,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )

        _, diagnostics, judge, summary = await self._run_case(
            question_id="q-5k",
            question_type="single-session-user",
            question="What is my personal best 5K time?",
            answer="25 minutes and 50 seconds",
            transcript="My personal best 5K is 25:50.",
            facts=[fact],
            judge_verdict=LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="The retrieved fact states the same duration as 25:50.",
                confidence=0.95,
            ),
        )

        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(diagnostics["answer_mode"], "judged-recall")
        self.assertFalse(diagnostics["answer_retrieved_from_tier3"])
        self.assertEqual(diagnostics["judge_verdict"], "supported")
        self.assertTrue(diagnostics["judged_recall_pass"])
        self.assertEqual(diagnostics["judge_model_group"], "claude")
        self.assertEqual(summary.judged_recall_supported, 1)
        self.assertEqual(summary.judged_recall_total, 1)

    async def test_judged_recall_partial_does_not_count_as_recall_pass(self) -> None:
        fact = LongTermFact(
            fact_id=1,
            fact_text="Ryan mentioned running a 5K recently.",
            subject="Ryan",
            category="fact",
            importance=5,
            confidence=0.8,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )

        _, diagnostics, _, summary = await self._run_case(
            question_id="q-partial",
            question_type="single-session-user",
            question="What is my personal best 5K time?",
            answer="25 minutes and 50 seconds",
            transcript="My personal best 5K is 25:50.",
            facts=[fact],
            judge_verdict=LongMemEvalRecallJudgeVerdict(
                verdict="partial",
                reason="The fact mentions a 5K but not the time.",
                confidence=0.6,
            ),
        )

        self.assertEqual(diagnostics["judge_verdict"], "partial")
        self.assertFalse(diagnostics["judged_recall_pass"])
        self.assertEqual(summary.judged_recall_supported, 0)
        self.assertEqual(summary.judged_recall_total, 1)
        self.assertEqual(
            summary.judged_recall_by_question_type,
            {"single-session-user": {"supported": 0, "total": 1}},
        )

    async def test_judged_recall_stops_when_dream_is_incomplete(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-judged-incomplete",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "The benchmark code was cedar."}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        drain = AsyncMock(
            return_value=_fake_dream_result(
                status="incomplete",
                light_cycles=3,
                rem_ran=False,
                deep_ran=False,
                error="Light phase did not reach a stable watermark.",
            )
        )
        retrieve = AsyncMock(return_value=[])
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="Should not be used.",
                confidence=0.99,
            )
        )

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
                judge_scorer=judge,
            )

        retrieve.assert_not_awaited()
        self.assertEqual(judge.calls, [])
        prediction = json.loads(
            summary.paths.predictions_path.read_text(encoding="utf-8")
        )
        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertEqual(
            prediction["hypothesis"],
            (
                "Tier 3 diagnostics incomplete: "
                "Light phase did not reach a stable watermark."
            ),
        )
        self.assertEqual(diagnostics["status"], "incomplete")
        self.assertIsNone(diagnostics["judge_verdict"])
        self.assertFalse(diagnostics["judged_recall_pass"])
        self.assertEqual(diagnostics["retrieved_long_term_fact_count"], 0)

    async def test_judged_recall_counts_pipeline_failures_as_recall_misses(
        self,
    ) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-supported",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [
                                {
                                    "role": "user",
                                    "content": "The benchmark code was cedar.",
                                }
                            ]
                        ],
                    },
                    {
                        "question_id": "q-retrieval-error",
                        "question_type": "knowledge-update",
                        "question": "What color did I switch to?",
                        "answer": "green",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "I switched to green."}]
                        ],
                    },
                ]
            ),
            encoding="utf-8",
        )
        fact = LongTermFact(
            fact_id=1,
            fact_text="The benchmark code was cedar.",
            subject="benchmark",
            category="fact",
            importance=7,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(
            side_effect=[[fact], RuntimeError("Tier 3 retrieval failed.")]
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="The retrieved fact states the benchmark code.",
                confidence=0.95,
            )
        )

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=2,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
                judge_scorer=judge,
            )

        diagnostics = [
            json.loads(line)
            for line in summary.paths.diagnostics_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertEqual(summary.questions_completed, 1)
        self.assertEqual(summary.questions_failed, 1)
        self.assertEqual(summary.judged_recall_supported, 1)
        self.assertEqual(summary.judged_recall_total, 2)
        self.assertEqual(
            summary.judged_recall_by_question_type,
            {
                "knowledge-update": {"supported": 0, "total": 1},
                "single-session-user": {"supported": 1, "total": 1},
            },
        )
        self.assertTrue(diagnostics[0]["judged_recall_pass"])
        self.assertFalse(diagnostics[1]["judged_recall_pass"])
        self.assertEqual(diagnostics[1]["status"], "error")
        self.assertEqual(diagnostics[1]["error"], "Tier 3 retrieval failed.")
        self.assertIsNone(diagnostics[1]["judge_verdict"])
        self.assertIsNone(diagnostics[1]["judge_error"])

    async def test_judged_recall_records_judge_error_as_recall_miss(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-judge-error",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "The benchmark code was cedar."}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        fact = LongTermFact(
            fact_id=1,
            fact_text="The benchmark code was cedar.",
            subject="benchmark",
            category="fact",
            importance=7,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=[fact])

        async def judge_error(
            _judge_input: LongMemEvalRecallJudgeInput,
        ) -> LongMemEvalRecallJudgeVerdict:
            raise RuntimeError("Judge timed out.")

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
                judge_scorer=judge_error,
            )

        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertEqual(summary.questions_completed, 0)
        self.assertEqual(summary.questions_failed, 1)
        self.assertEqual(summary.judged_recall_supported, 0)
        self.assertEqual(summary.judged_recall_total, 1)
        self.assertEqual(diagnostics["status"], "error")
        self.assertEqual(diagnostics["error"], "Judge timed out.")
        self.assertEqual(diagnostics["judge_error"], "Judge timed out.")
        self.assertFalse(diagnostics["judged_recall_pass"])

    async def test_judged_recall_does_not_blend_candidates_when_tier3_hits(
        self,
    ) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-judged-tier3-hit",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "The benchmark code was cedar."}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        fact = LongTermFact(
            fact_id=1,
            fact_text="The benchmark code was cedar.",
            subject="benchmark",
            category="fact",
            importance=7,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=[fact])
        fallback = AsyncMock(
            return_value=[
                CandidateNote(
                    candidate_id=2,
                    fact_text="A candidate note should not be mixed in.",
                    category="fact",
                    source_message_ids=[2],
                    created_at="2026-01-02T03:04:05+00:00",
                )
            ]
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="The durable fact states the benchmark code.",
                confidence=0.95,
            )
        )

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
            patch("vexic.longmemeval.retrieve_candidate_fallback", new=fallback),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="claude",
                answer_mode="judged-recall",
                judge_scorer=judge,
            )

        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        fallback.assert_not_awaited()
        self.assertFalse(diagnostics["candidate_fallback_used"])
        self.assertEqual(diagnostics["retrieved_long_term_fact_count"], 1)
        self.assertEqual(diagnostics["retrieved_candidate_note_count"], 0)
        self.assertEqual(
            judge.calls[0].retrieved_fact_texts,
            ("The benchmark code was cedar.",),
        )

    async def test_judged_recall_candidate_fallback_logs_retrieval_event(
        self,
    ) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-judged-event",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "The benchmark code was cedar."}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )

        async def seed_candidate(
            db_path: str,
            _model_group: str,
            **_: object,
        ) -> object:
            commit_dream_cycle(
                db_path,
                [
                    FactCandidate(
                        fact_text="The benchmark code was cedar.",
                        subject="benchmark",
                        category="fact",
                        importance=7,
                        confidence=1.0,
                        source_message_ids=[1],
                    )
                ],
                agent_id=None,
                candidate_embeddings=[_basis_vector(0)],
                status="ok",
                started_at="2026-01-03T00:00:00+00:00",
                finished_at="2026-01-03T00:00:01+00:00",
                messages_processed=1,
                last_processed_message_id=1,
            )
            return _fake_dream_result()

        retrieve = AsyncMock(return_value=[])
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="The retrieved note states the benchmark code.",
                confidence=0.95,
            )
        )

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=seed_candidate),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
            patch(
                "vexic.subagents.retrieval.embed_texts",
                return_value=[_basis_vector(0)],
            ),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="claude",
                answer_mode="judged-recall",
                judge_scorer=judge,
            )

        db_path = next(summary.paths.run_dir.glob("q-judged-event-*/memory.db"))
        with closing(sqlite3.connect(db_path)) as conn:
            event_row = conn.execute(
                """
                SELECT candidate_id, session_id, query
                FROM candidate_retrieval_events
                """
            ).fetchone()
            retrieved_count = conn.execute(
                "SELECT retrieved_count FROM memory_candidates WHERE id = 1"
            ).fetchone()[0]

        self.assertEqual(event_row[0], 1)
        self.assertEqual(event_row[1], "longmemeval:q-judged-event:answer")
        self.assertEqual(event_row[2], "What benchmark code was mentioned?")
        self.assertEqual(retrieved_count, 1)

    async def test_judged_recall_records_factory_judge_error_in_diagnostics(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-factory-judge-error",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "The benchmark code was cedar."}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        fact = LongTermFact(
            fact_id=1,
            fact_text="The benchmark code was cedar.",
            subject="benchmark",
            category="fact",
            importance=7,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=[fact])

        class _FailingJudgeAgent:
            model = type("Model", (), {"model_name": "fake/judge-model"})()

            async def run(self, prompt: str) -> object:
                raise RuntimeError("Provider judge exploded.")

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
                judge_agent_factory=lambda group, secrets=None: _FailingJudgeAgent(),
            )

        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertEqual(summary.questions_failed, 1)
        self.assertEqual(diagnostics["status"], "error")
        self.assertEqual(diagnostics["judge_error"], "Provider judge exploded.")
        self.assertEqual(diagnostics["error"], "Provider judge exploded.")
        self.assertEqual(diagnostics["judge_model_id"], "fake/judge-model")
        self.assertFalse(diagnostics["judged_recall_pass"])

    async def test_judged_recall_fails_closed_without_judge_port(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-no-judge-port",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "The benchmark code was cedar."}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        fact = LongTermFact(
            fact_id=1,
            fact_text="The benchmark code was cedar.",
            subject="benchmark",
            category="fact",
            importance=7,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=[fact])

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
            )

        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertEqual(summary.questions_completed, 0)
        self.assertEqual(summary.questions_failed, 1)
        self.assertEqual(diagnostics["status"], "error")
        self.assertIn("judge_agent_factory", diagnostics["error"])
        self.assertFalse(diagnostics["judged_recall_pass"])

    async def test_judged_recall_uses_host_supplied_judge_agent_factory(self) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-judge-factory",
                        "question_type": "single-session-user",
                        "question": "What benchmark code was mentioned?",
                        "answer": "cedar",
                        "question_date": "2026-01-03",
                        "answer_session_ids": ["session-1"],
                        "haystack_session_ids": ["session-1"],
                        "haystack_dates": ["2026-01-02T03:04:05Z"],
                        "haystack_sessions": [
                            [{"role": "user", "content": "The benchmark code was cedar."}]
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        fact = LongTermFact(
            fact_id=1,
            fact_text="The benchmark code was cedar.",
            subject="benchmark",
            category="fact",
            importance=7,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        drain = AsyncMock(return_value=_fake_dream_result())
        retrieve = AsyncMock(return_value=[fact])
        verdict = LongMemEvalRecallJudgeVerdict(
            verdict="supported",
            reason="The retrieved fact states the benchmark code.",
            confidence=0.9,
        )

        class _FakeJudgeAgent:
            def __init__(self) -> None:
                self.model = type("Model", (), {"model_name": "fake/judge-model"})()
                self.prompts: list[str] = []

            async def run(self, prompt: str) -> object:
                self.prompts.append(prompt)
                return type("Result", (), {"output": verdict})()

        agent = _FakeJudgeAgent()
        factory_calls: list[tuple[str, object]] = []

        def factory(model_group: str, secrets: object = None) -> _FakeJudgeAgent:
            factory_calls.append((model_group, secrets))
            return agent

        with (
            patch("vexic.longmemeval.drain_light_then_consolidate", new=drain),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
        ):
            summary = await run_longmemeval_subset(
                dataset_path,
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
                judge_agent_factory=factory,
            )

        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertEqual(factory_calls, [("claude", None)])
        self.assertEqual(len(agent.prompts), 1)
        self.assertIn("Retrieved facts:", agent.prompts[0])
        self.assertEqual(diagnostics["judge_verdict"], "supported")
        self.assertTrue(diagnostics["judged_recall_pass"])
        self.assertEqual(diagnostics["judge_model_id"], "fake/judge-model")
        self.assertEqual(summary.judged_recall_supported, 1)


_FAKE_ADAPTER_SOURCE = '''
"""Fake eval adapter for CLI wiring tests."""

PROVIDER = "fake"


class _Agent:
    async def run(self, prompt, *args, **kwargs):
        raise RuntimeError("fake adapter agent should not run in CLI tests")


def build_extraction_agent(model_group, secrets=None):
    return _Agent()


def build_contradiction_agent(model_group, secrets=None):
    return _Agent()


def build_longmemeval_recall_judge_agent(model_group, secrets=None):
    return _Agent()


def embed_texts(texts):
    return [[0.0] * 4 for _ in texts]
'''


class LongMemEvalCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _base_argv(self, *extra: str) -> list[str]:
        return [
            "--dataset",
            str(self.root / "longmemeval_oracle.json"),
            "--split",
            "oracle",
            "--output-dir",
            str(self.root / "runs"),
            *extra,
        ]

    def test_cli_parses_dream_session_batch_size(self) -> None:
        args = build_parser().parse_args(
            self._base_argv("--dream-session-batch-size", "3")
        )
        self.assertEqual(args.dream_session_batch_size, 3)

    def test_cli_rejects_non_positive_dream_session_batch_size(self) -> None:
        for value in ("0", "-1"):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    self._base_argv("--dream-session-batch-size", value)
                )

    def test_cli_defaults_match_documented_smoke_run(self) -> None:
        args = build_parser().parse_args(self._base_argv())
        self.assertEqual(args.limit, 12)
        self.assertEqual(args.selection, "stratified")
        self.assertEqual(args.model_group, "glm")
        self.assertEqual(args.answer_mode, "retrieval-debug")
        self.assertEqual(args.judge_model_group, "claude")
        self.assertEqual(args.deep_top_n, 15)
        self.assertFalse(args.skip_dream)
        self.assertFalse(args.allow_live)

    def test_cli_skip_dream_runs_without_adapter_or_allow_live(self) -> None:
        summary = unittest.mock.MagicMock()
        summary.judged_recall_total = None
        summary.judged_recall_supported = None
        runner = AsyncMock(return_value=summary)

        with patch("vexic.longmemeval.run_longmemeval_subset", new=runner):
            exit_code = longmemeval_main(self._base_argv("--skip-dream"))

        self.assertEqual(exit_code, 0)
        runner.assert_awaited_once()
        self.assertTrue(runner.await_args.kwargs["skip_dream"])
        self.assertIsNone(runner.await_args.kwargs["extraction_agent_factory"])
        self.assertIsNone(runner.await_args.kwargs["judge_agent_factory"])

    def test_cli_dream_run_requires_allow_live(self) -> None:
        runner = AsyncMock()

        with patch("vexic.longmemeval.run_longmemeval_subset", new=runner):
            exit_code = longmemeval_main(self._base_argv())

        self.assertEqual(exit_code, 0)
        runner.assert_not_awaited()

    def test_cli_wires_adapter_factories_for_judged_recall(self) -> None:
        adapter_path = self.root / "fake_adapter.py"
        adapter_path.write_text(_FAKE_ADAPTER_SOURCE, encoding="utf-8")
        summary = unittest.mock.MagicMock()
        summary.judged_recall_total = 1
        summary.judged_recall_supported = 1
        summary.judged_recall_by_question_type = {
            "single-session-user": {"supported": 1, "total": 1}
        }
        summary.questions_started = 1
        summary.questions_completed = 1
        summary.questions_failed = 0
        runner = AsyncMock(return_value=summary)

        with patch("vexic.longmemeval.run_longmemeval_subset", new=runner):
            exit_code = longmemeval_main(
                self._base_argv(
                    "--allow-live",
                    "--adapter",
                    str(adapter_path),
                    "--answer-mode",
                    "judged-recall",
                )
            )

        self.assertEqual(exit_code, 0)
        runner.assert_awaited_once()
        kwargs = runner.await_args.kwargs
        self.assertEqual(kwargs["answer_mode"], "judged-recall")
        self.assertTrue(callable(kwargs["extraction_agent_factory"]))
        self.assertTrue(callable(kwargs["contradiction_agent_factory"]))
        self.assertTrue(callable(kwargs["judge_agent_factory"]))
        self.assertTrue(callable(kwargs["embed"]))

    def test_cli_dream_run_requires_adapter_path(self) -> None:
        runner = AsyncMock()

        with patch("vexic.longmemeval.run_longmemeval_subset", new=runner):
            exit_code = longmemeval_main(self._base_argv("--allow-live"))

        self.assertNotEqual(exit_code, 0)
        runner.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
