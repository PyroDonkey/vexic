"""Conformance tests for the LongMemEval harness (rehomed from Coalescent, COA-342)."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vexic.longmemeval import (
    _select_instances,
    drain_light_then_consolidate,
    drain_light_then_rem,
    ingest_instance,
    create_run_paths,
    parse_longmemeval_instance,
    question_db_path,
    run_longmemeval_subset,
)
from vexic.storage import init_db, save_messages, search_messages


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


if __name__ == "__main__":
    unittest.main()
