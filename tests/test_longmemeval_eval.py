"""Conformance tests for the LongMemEval harness in vexic.longmemeval."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vexic.longmemeval import (
    build_parser,
    main as longmemeval_main,
    LONGMEMEVAL_RECALL_JUDGE_PREFERENCE_PROMPT_VERSION,
    LONGMEMEVAL_RECALL_JUDGE_PROMPT_VERSION,
    LongMemEvalRecallJudgeInput,
    LongMemEvalRecallJudgeVerdict,
    PREFERENCE_QUESTION_TYPES,
    _PREFERENCE_RUBRIC_GUIDANCE,
    _answer_variants,
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

    async def test_zero_max_light_cycles_runs_no_light_phase(self) -> None:
        light = AsyncMock()
        rem = AsyncMock()
        deep = AsyncMock()

        with (
            patch("vexic.longmemeval.get_watermark", return_value=0),
            patch("vexic.longmemeval.run_light_phase", light),
            patch("vexic.longmemeval.run_rem_phase", rem),
            patch("vexic.longmemeval.run_deep_phase", deep),
        ):
            consolidate_result = await drain_light_then_consolidate(
                "memory.db",
                "glm",
                message_count=100,
                max_light_cycles=0,
            )
            rem_result = await drain_light_then_rem(
                "memory.db",
                "glm",
                message_count=100,
                max_light_cycles=0,
            )

        light.assert_not_awaited()
        rem.assert_not_awaited()
        deep.assert_not_awaited()
        self.assertEqual(consolidate_result.status, "incomplete")
        self.assertEqual(consolidate_result.light_cycles, 0)
        self.assertEqual(rem_result.status, "incomplete")
        self.assertEqual(rem_result.light_cycles, 0)


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

    def _typed_rows(self) -> list[dict]:
        rows = []
        for question_type in ("single-session-user", "multi-session", "knowledge-update"):
            for index in range(3):
                rows.append(
                    {
                        "question_id": f"{question_type}-{index}",
                        "question_type": question_type,
                    }
                )
        return rows

    def test_type_weight_defaults_preserve_equal_round_robin(self) -> None:
        rows = self._typed_rows()
        self.assertEqual(
            _select_instances(rows, limit=6, selection="stratified"),
            _select_instances(
                rows,
                limit=6,
                selection="stratified",
                type_weights=None,
            ),
        )

    def test_stratified_selection_with_type_weights_biases_selected_types(self) -> None:
        selected = _select_instances(
            self._typed_rows(),
            limit=6,
            selection="stratified",
            type_weights={"multi-session": 3, "knowledge-update": 2},
        )
        self.assertEqual(
            [row["question_id"] for row in selected],
            [
                "single-session-user-0",
                "multi-session-0",
                "multi-session-1",
                "multi-session-2",
                "knowledge-update-0",
                "knowledge-update-1",
            ],
        )

    def test_type_weight_exhausted_group_yields_slots_to_others(self) -> None:
        selected = _select_instances(
            self._typed_rows(),
            limit=9,
            selection="stratified",
            type_weights={"multi-session": 3, "knowledge-update": 2},
        )
        self.assertEqual(
            [row["question_id"] for row in selected],
            [
                "single-session-user-0",
                "multi-session-0",
                "multi-session-1",
                "multi-session-2",
                "knowledge-update-0",
                "knowledge-update-1",
                "single-session-user-1",
                "knowledge-update-2",
                "single-session-user-2",
            ],
        )

    def test_select_instances_rejects_type_weights_with_first_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "type weights require stratified"):
            _select_instances(
                self._typed_rows(),
                limit=3,
                selection="first",
                type_weights={"multi-session": 2},
            )

    def test_select_instances_rejects_non_positive_type_weight(self) -> None:
        with self.assertRaisesRegex(ValueError, "type weight.*at least 1"):
            _select_instances(
                self._typed_rows(),
                limit=3,
                selection="stratified",
                type_weights={"multi-session": 0},
            )

    def test_parser_accepts_repeated_type_weight_and_rejects_malformed(self) -> None:
        parser = build_parser()
        base_args = [
            "--dataset",
            "dataset.json",
            "--split",
            "s",
            "--output-dir",
            "runs",
        ]
        args = parser.parse_args(
            [
                *base_args,
                "--type-weight",
                "multi-session=3",
                "--type-weight",
                "knowledge-update=2",
            ]
        )
        self.assertEqual(
            args.type_weight,
            [("multi-session", 3), ("knowledge-update", 2)],
        )
        for malformed in ("foo", "x=0", "x=-1", "=3", "x=y"):
            with self.subTest(malformed=malformed), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([*base_args, "--type-weight", malformed])

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
        # Gap 1: both the raw and filter-surviving rank fields are emitted.
        row = json.loads(diagnostics)
        self.assertIn("answer_candidate_rank", row)
        self.assertIn("answer_candidate_rank_filtered", row)
        self.assertIn("answer_candidate_rank_filtered_bucket", row)

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

    # Exact v1 render bytes, written by hand from the renderer spec and
    # verified against current output before any behavior change. Any drift in
    # the base render must break this golden.
    _V1_GOLDEN = (
        "Question:\n"
        "What kind of Premiere Pro resource suggestions do I prefer?\n\n"
        "Gold answer:\n"
        '"tailored Premiere-Pro-specific resource suggestions"\n\n'
        "Retrieved facts:\n"
        "[fact 1] Ryan edits video in Adobe Premiere Pro.\n"
        "[unverified note] Ryan likes concise, tailored tips.\n"
        "(category: preference, recently noted, not yet confirmed, source messages: 2)"
    )

    def _golden_judge_input(
        self, question_type: str | None = None
    ) -> LongMemEvalRecallJudgeInput:
        kwargs: dict[str, object] = {}
        if question_type is not None:
            kwargs["question_type"] = question_type
        return LongMemEvalRecallJudgeInput(
            question="What kind of Premiere Pro resource suggestions do I prefer?",
            gold_answer="tailored Premiere-Pro-specific resource suggestions",
            retrieved_fact_texts=(
                "Ryan edits video in Adobe Premiere Pro.",
                "[unverified note] Ryan likes concise, tailored tips.\n"
                "(category: preference, recently noted, not yet confirmed, "
                "source messages: 2)",
            ),
            **kwargs,
        )

    def test_recall_judge_render_non_preference_byte_equals_v1_golden(self) -> None:
        self.assertEqual(
            _render_recall_judge_input(
                self._golden_judge_input(question_type="single-session-user")
            ),
            self._V1_GOLDEN,
        )
        self.assertEqual(
            _render_recall_judge_input(self._golden_judge_input()),
            self._V1_GOLDEN,
        )

    def test_recall_judge_render_preference_appends_rubric_instruction(self) -> None:
        rendered = _render_recall_judge_input(
            self._golden_judge_input(question_type="single-session-preference")
        )
        self.assertEqual(
            rendered,
            self._V1_GOLDEN + "\n\n" + _PREFERENCE_RUBRIC_GUIDANCE,
        )

    def test_recall_judge_input_question_type_defaults_none(self) -> None:
        judge_input = LongMemEvalRecallJudgeInput(
            question="q",
            gold_answer="a",
            retrieved_fact_texts=(),
        )
        self.assertIsNone(judge_input.question_type)

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

    async def test_preference_row_records_rubric_prompt_version(self) -> None:
        pref_fact = LongTermFact(
            fact_id=1,
            fact_text="Ryan asks for Premiere-Pro-specific how-to walkthroughs.",
            subject="Ryan",
            category="preference",
            importance=6,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        _, pref_diag, _, _ = await self._run_case(
            question_id="q-pref",
            question_type="single-session-preference",
            question="What kind of editing resources should suggestions favor?",
            answer=(
                "Suggestions should favor resources tailored to Adobe Premiere "
                "Pro rather than generic video-editing tips."
            ),
            transcript="When I ask for editing help I edit in Adobe Premiere Pro.",
            facts=[pref_fact],
            judge_verdict=LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="The fact satisfies the tailored-to-Premiere-Pro rubric.",
                confidence=0.9,
            ),
        )

        base_fact = LongTermFact(
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
        _, base_diag, _, _ = await self._run_case(
            question_id="q-base",
            question_type="single-session-user",
            question="What is my personal best 5K time?",
            answer="25 minutes and 50 seconds",
            transcript="My personal best 5K is 25:50.",
            facts=[base_fact],
            judge_verdict=LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="The fact states the duration.",
                confidence=0.95,
            ),
        )

        self.assertEqual(
            pref_diag["judge_prompt_version"],
            LONGMEMEVAL_RECALL_JUDGE_PREFERENCE_PROMPT_VERSION,
        )
        self.assertEqual(
            pref_diag["judge_prompt_version"],
            "longmemeval-recall-judge-v1+preference-rubric-v1",
        )
        self.assertEqual(
            base_diag["judge_prompt_version"],
            LONGMEMEVAL_RECALL_JUDGE_PROMPT_VERSION,
        )
        self.assertEqual(
            base_diag["judge_prompt_version"], "longmemeval-recall-judge-v1"
        )

    async def test_preference_rubric_satisfying_fact_scored_supported(self) -> None:
        # Fact satisfies the rubric criterion without restating the gold prose.
        fact = LongTermFact(
            fact_id=1,
            fact_text="Ryan edits every project in Adobe Premiere Pro.",
            subject="Ryan",
            category="preference",
            importance=6,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )
        _, diagnostics, judge, _ = await self._run_case(
            question_id="q-pref-rubric",
            question_type="single-session-preference",
            question="What should editing-resource suggestions be tailored to?",
            answer=(
                "Suggestions should be tailored to Adobe Premiere Pro rather "
                "than generic editing advice."
            ),
            transcript="I do all my editing in Adobe Premiere Pro.",
            facts=[fact],
            judge_verdict=LongMemEvalRecallJudgeVerdict(
                verdict="supported",
                reason="Premiere Pro usage satisfies the tailored-resource rubric.",
                confidence=0.9,
            ),
        )

        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(judge.calls[0].question_type, "single-session-preference")
        self.assertIs(diagnostics["judged_recall_pass"], True)

    def test_preference_fixture_parses(self) -> None:
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "longmemeval_oracle_preference_smoke.json"
        )
        rows = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        instance = parse_longmemeval_instance(rows[0])
        self.assertEqual(instance.question_type, "single-session-preference")
        self.assertIn(instance.question_type, PREFERENCE_QUESTION_TYPES)
        self.assertIsInstance(instance.answer, str)

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

    async def test_judged_recall_judge_failure_preserves_retrieved_hypothesis(
        self,
    ) -> None:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-judge-error-hypothesis",
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

        prediction = json.loads(
            summary.paths.predictions_path.read_text(encoding="utf-8")
        )

        self.assertEqual(summary.questions_failed, 1)
        self.assertIn("cedar", prediction["hypothesis"])

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


class LongMemEvalAnswerVariantTests(unittest.TestCase):
    def test_numeric_scalar_gold_answers_are_matchable(self) -> None:
        self.assertEqual(_answer_variants(2015), ("2015",))
        self.assertEqual(_answer_variants(3.5), ("3.5",))

    def test_sequence_gold_answers_include_numeric_items(self) -> None:
        self.assertEqual(_answer_variants(["cedar", 2015]), ("cedar", "2015"))

    def test_unmatchable_gold_answers_stay_empty(self) -> None:
        self.assertEqual(_answer_variants(None), ())
        self.assertEqual(_answer_variants({"answer": "cedar"}), ())


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
        self.assertEqual(args.max_transient_retries, 2)
        self.assertFalse(args.skip_dream)
        self.assertFalse(args.allow_live)

    def test_cli_accepts_max_transient_retries_override(self) -> None:
        args = build_parser().parse_args(
            self._base_argv("--max-transient-retries", "5")
        )
        self.assertEqual(args.max_transient_retries, 5)

    def test_cli_skip_dream_runs_without_adapter_or_allow_live(self) -> None:
        summary = unittest.mock.MagicMock()
        summary.judged_recall_total = None
        summary.judged_recall_supported = None
        summary.questions_started = 1
        summary.questions_completed = 1
        summary.questions_failed = 0
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

        self.assertEqual(exit_code, 2)
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

    def test_cli_returns_nonzero_when_any_question_incomplete(self) -> None:
        summary = unittest.mock.MagicMock()
        summary.judged_recall_total = None
        summary.judged_recall_supported = None
        summary.questions_started = 2
        summary.questions_completed = 1
        summary.questions_failed = 0
        runner = AsyncMock(return_value=summary)

        with patch("vexic.longmemeval.run_longmemeval_subset", new=runner):
            exit_code = longmemeval_main(self._base_argv("--skip-dream"))

        self.assertEqual(exit_code, 1)

    def test_cli_returns_nonzero_when_any_question_failed(self) -> None:
        summary = unittest.mock.MagicMock()
        summary.judged_recall_total = None
        summary.judged_recall_supported = None
        summary.questions_started = 2
        summary.questions_completed = 1
        summary.questions_failed = 1
        runner = AsyncMock(return_value=summary)

        with patch("vexic.longmemeval.run_longmemeval_subset", new=runner):
            exit_code = longmemeval_main(self._base_argv("--skip-dream"))

        self.assertEqual(exit_code, 1)

    def test_cli_rejects_non_positive_max_light_cycles(self) -> None:
        for value in ("0", "-1"):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    self._base_argv("--max-light-cycles", value)
                )

    def test_cli_dream_run_requires_adapter_path(self) -> None:
        runner = AsyncMock()

        with patch("vexic.longmemeval.run_longmemeval_subset", new=runner):
            exit_code = longmemeval_main(self._base_argv("--allow-live"))

        self.assertNotEqual(exit_code, 0)
        runner.assert_not_awaited()


def _finish_reason_error() -> Exception:
    """Reproduce OpenRouter's `finish_reason='error'` pydantic ValidationError.

    The real failure is a pydantic ValidationError raised when the OpenAI-shaped
    ChatCompletion is validated and a choice carries the non-enum
    `finish_reason='error'` (loc ``choices.0.finish_reason``, literal_error).
    """
    from typing import Literal

    from pydantic import BaseModel, ValidationError

    class _Choice(BaseModel):
        finish_reason: Literal[
            "stop", "length", "tool_calls", "content_filter", "function_call"
        ]

    class _ChatCompletion(BaseModel):
        choices: list[_Choice]

    try:
        _ChatCompletion(choices=[{"finish_reason": "error"}])
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


class TransientRetryRowRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Gap 2: a bounded transient provider fault recovers instead of failing."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_dataset(self) -> Path:
        dataset_path = self.root / "longmemeval_oracle.json"
        dataset_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q-retry",
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
        return dataset_path

    async def _run(self, judge_scorer, *, max_transient_retries: int = 2):
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
            patch("vexic.longmemeval.asyncio.sleep", new=AsyncMock()),
        ):
            summary = await run_longmemeval_subset(
                self._write_dataset(),
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
                judge_scorer=judge_scorer,
                max_transient_retries=max_transient_retries,
            )
        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )
        return summary, diagnostics

    async def test_transient_judge_fault_recovers_within_the_retry_budget(self) -> None:
        calls = {"n": 0}

        async def flaky_judge(
            _judge_input: LongMemEvalRecallJudgeInput,
        ) -> LongMemEvalRecallJudgeVerdict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _finish_reason_error()
            return LongMemEvalRecallJudgeVerdict(
                verdict="supported", reason="ok", confidence=0.9
            )

        summary, diagnostics = await self._run(flaky_judge)

        self.assertEqual(diagnostics["status"], "ok")
        self.assertEqual(diagnostics["transient_retry_count"], 1)
        self.assertEqual(summary.questions_failed, 0)
        # The recovered row is scored exactly once (no double counting).
        self.assertEqual(summary.judged_recall_total, 1)
        self.assertEqual(calls["n"], 2)

    async def test_non_transient_judge_fault_is_not_retried(self) -> None:
        calls = {"n": 0}

        async def broken_judge(
            _judge_input: LongMemEvalRecallJudgeInput,
        ) -> LongMemEvalRecallJudgeVerdict:
            calls["n"] += 1
            raise RuntimeError("Judge exploded.")

        summary, diagnostics = await self._run(broken_judge)

        self.assertEqual(diagnostics["status"], "error")
        self.assertEqual(diagnostics["transient_retry_count"], 0)
        self.assertEqual(summary.questions_failed, 1)
        self.assertEqual(calls["n"], 1)

    async def test_persistent_transient_fault_is_bounded_then_recorded(self) -> None:
        calls = {"n": 0}

        async def always_transient(
            _judge_input: LongMemEvalRecallJudgeInput,
        ) -> LongMemEvalRecallJudgeVerdict:
            calls["n"] += 1
            raise _finish_reason_error()

        summary, diagnostics = await self._run(
            always_transient, max_transient_retries=2
        )

        self.assertEqual(diagnostics["status"], "error")
        self.assertEqual(summary.questions_failed, 1)
        # Bounded: initial attempt + max_transient_retries, no more.
        self.assertEqual(calls["n"], 3)
        # A row that exhausted its budget must report the retries it consumed,
        # not zero.
        self.assertEqual(diagnostics["transient_retry_count"], 2)

    async def test_transient_dream_fault_recovers_after_db_reset(self) -> None:
        # Real ingest runs (not mocked), so this exercises _reset_question_db +
        # init-memo eviction: the re-dream must rebuild the wiped per-question
        # DB and re-ingest, or save_messages raises `no such table: messages`.
        drain_calls = {"n": 0}

        async def flaky_drain(*_args, **_kwargs):
            drain_calls["n"] += 1
            if drain_calls["n"] == 1:
                # pydantic-ai re-raises the provider fault wrapped; the classifier
                # must walk __cause__ to see the finish_reason ValidationError.
                raise RuntimeError("wrapped provider fault") from _finish_reason_error()
            return _fake_dream_result()

        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported", reason="ok", confidence=0.9
            )
        )
        retrieve = AsyncMock(
            return_value=[
                LongTermFact(
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
            ]
        )
        with (
            patch(
                "vexic.longmemeval.drain_light_then_consolidate",
                new=AsyncMock(side_effect=flaky_drain),
            ),
            patch("vexic.longmemeval.retrieve_long_term_facts", new=retrieve),
            patch("vexic.longmemeval.asyncio.sleep", new=AsyncMock()),
        ):
            summary = await run_longmemeval_subset(
                self._write_dataset(),
                split="oracle",
                output_dir=self.root / "runs",
                limit=1,
                model_group="glm",
                answer_mode="judged-recall",
                judge_model_group="claude",
                judge_scorer=judge,
            )
        diagnostics = json.loads(
            summary.paths.diagnostics_path.read_text(encoding="utf-8")
        )

        self.assertEqual(diagnostics["status"], "ok")
        self.assertEqual(diagnostics["transient_retry_count"], 1)
        self.assertEqual(summary.questions_failed, 0)
        self.assertEqual(drain_calls["n"], 2)


class TransientProviderErrorClassifierTests(unittest.TestCase):
    """Gap 2: only provider-shape faults are treated as transient/retryable."""

    def test_finish_reason_error_validation_error_is_transient(self) -> None:
        from vexic.longmemeval import _is_transient_provider_error

        self.assertTrue(_is_transient_provider_error(_finish_reason_error()))

    def test_wrapped_finish_reason_error_is_transient(self) -> None:
        # pydantic-ai re-raises the ValidationError wrapped, e.g.
        # `raise UnexpectedModelBehavior(...) from validation_error`; the
        # classifier must follow __cause__ to still recognize it.
        from vexic.longmemeval import _is_transient_provider_error

        try:
            raise RuntimeError("Invalid response from ... chat completions") \
                from _finish_reason_error()
        except RuntimeError as exc:
            self.assertTrue(_is_transient_provider_error(exc))

    def test_wrapped_json_decode_error_is_transient(self) -> None:
        from vexic.longmemeval import _is_transient_provider_error

        try:
            try:
                json.loads("not json {")
            except json.JSONDecodeError as decode_exc:
                raise RuntimeError("wrapped") from decode_exc
        except RuntimeError as exc:
            self.assertTrue(_is_transient_provider_error(exc))

    def test_malformed_json_decode_error_is_transient(self) -> None:
        from vexic.longmemeval import _is_transient_provider_error

        try:
            json.loads("not json {")
        except json.JSONDecodeError as exc:
            self.assertTrue(_is_transient_provider_error(exc))
        else:
            self.fail("expected JSONDecodeError")

    def test_ordinary_bugs_are_not_transient(self) -> None:
        from vexic.longmemeval import _is_transient_provider_error

        for exc in (
            ValueError("boom"),
            KeyError("missing"),
            RuntimeError("Tier 3 retrieval failed."),
        ):
            self.assertFalse(_is_transient_provider_error(exc))

    def test_unrelated_validation_error_is_not_transient(self) -> None:
        from pydantic import BaseModel, ValidationError

        from vexic.longmemeval import _is_transient_provider_error

        class _Model(BaseModel):
            count: int

        try:
            _Model(count="not-an-int")
        except ValidationError as exc:
            self.assertFalse(_is_transient_provider_error(exc))
        else:
            self.fail("expected ValidationError")


def _diag_candidate(**overrides: object) -> object:
    from datetime import datetime, timezone

    from vexic.longmemeval import _DiagnosticCandidate

    defaults: dict[str, object] = {
        "candidate_id": 1,
        "fact_text": "Ryan prefers dark roast coffee.",
        "importance": 5,
        "hit_count": 1,
        "last_seen_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "rem_boost": 0.0,
        "promoted": False,
        "promoted_fact_id": None,
        "category": "preference",
        "occurred_at": None,
        "mentioned_at": None,
        "has_embedding": True,
    }
    defaults.update(overrides)
    return _DiagnosticCandidate(**defaults)


class FilteredCandidateRankDiagnosticsTests(unittest.TestCase):
    """Gap 1: answer_candidate_rank_filtered ranks the promotion-eligible pool."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        init_db(str(self.db_path))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _commit(self, candidates: list, vectors: list[list[float]]) -> None:
        commit_dream_cycle(
            str(self.db_path),
            candidates,
            agent_id=None,
            status="ok",
            started_at="2026-01-02T00:00:00+00:00",
            finished_at="2026-01-02T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
            candidate_embeddings=vectors,
        )

    def _instance(self, answer: str):
        from vexic.longmemeval import LongMemEvalInstance

        return LongMemEvalInstance(
            question_id="q-rank",
            question_type="single-session-user",
            question="Where does Ryan live?",
            question_date="2026-01-03",
            sessions=(),
            answer=answer,
        )

    def test_filtered_rank_beats_raw_when_undated_event_outranks_answer(self) -> None:
        from vexic.longmemeval import _answer_diagnostics

        answer_fact = FactCandidate(
            fact_text="Ryan lives in Helsinki.",
            subject="Ryan",
            category="fact",
            importance=3,
            confidence=0.8,
            source_message_ids=[1],
        )
        undated_event = FactCandidate(
            fact_text="Attended a large conference.",
            subject="Ryan",
            category="event",
            importance=9,
            confidence=0.8,
            source_message_ids=[1],
            occurred_at=None,
        )
        self._commit(
            [answer_fact, undated_event],
            [_basis_vector(0), _basis_vector(1)],
        )

        diagnostics = _answer_diagnostics(
            db_path=self.db_path,
            instance=self._instance("Helsinki"),
            retrieved_long_term_fact_texts=(),
            candidate_scoring_time=None,
        )

        # Raw pool: the importance-9 undated event outranks the answer fact.
        self.assertEqual(diagnostics.answer_candidate_rank, 2)
        # Filtered pool: the undated event is dropped, answer rises to rank 1.
        self.assertEqual(diagnostics.answer_candidate_rank_filtered, 1)

    def test_answer_candidate_that_is_filtered_has_no_filtered_rank(self) -> None:
        from vexic.longmemeval import _answer_diagnostics

        # The answer itself is an undated event -> dropped from the pool.
        answer_event = FactCandidate(
            fact_text="Ryan moved to Helsinki.",
            subject="Ryan",
            category="event",
            importance=5,
            confidence=0.8,
            source_message_ids=[1],
            occurred_at=None,
        )
        self._commit([answer_event], [_basis_vector(0)])

        diagnostics = _answer_diagnostics(
            db_path=self.db_path,
            instance=self._instance("Helsinki"),
            retrieved_long_term_fact_texts=(),
            candidate_scoring_time=None,
        )

        self.assertEqual(diagnostics.answer_candidate_rank, 1)
        self.assertIsNone(diagnostics.answer_candidate_rank_filtered)


class DeepEligibleFilterTests(unittest.TestCase):
    """Gap 1: the filter-surviving population mirrors Deep's promotion pool."""

    def _ids(self, candidates: list) -> list[int]:
        return [candidate.candidate_id for candidate in candidates]

    def test_keeps_a_dated_embedded_unpromoted_candidate(self) -> None:
        from vexic.longmemeval import _deep_eligible

        keep = _diag_candidate(candidate_id=10)
        self.assertEqual(self._ids(_deep_eligible([keep])), [10])

    def test_drops_promoted_candidates(self) -> None:
        from vexic.longmemeval import _deep_eligible

        by_flag = _diag_candidate(candidate_id=1, promoted=True)
        by_fact_id = _diag_candidate(candidate_id=2, promoted_fact_id=99)
        kept = _diag_candidate(candidate_id=3)
        survivors = _deep_eligible([by_flag, by_fact_id, kept])
        self.assertEqual(self._ids(survivors), [3])

    def test_drops_candidates_without_an_embedding(self) -> None:
        from vexic.longmemeval import _deep_eligible

        no_vector = _diag_candidate(candidate_id=1, has_embedding=False)
        embedded = _diag_candidate(candidate_id=2)
        self.assertEqual(self._ids(_deep_eligible([no_vector, embedded])), [2])

    def test_drops_undated_event_but_keeps_dated_event(self) -> None:
        from vexic.longmemeval import _deep_eligible

        undated = _diag_candidate(
            candidate_id=1,
            category="event",
            occurred_at=None,
            mentioned_at="   ",
        )
        dated = _diag_candidate(
            candidate_id=2, category="event", occurred_at="2026-01-01"
        )
        mentioned = _diag_candidate(
            candidate_id=3,
            category="event",
            occurred_at=None,
            mentioned_at="2026-01-02T00:00:00+00:00",
        )
        survivors = _deep_eligible([undated, dated, mentioned])
        self.assertEqual(self._ids(survivors), [2, 3])

    def test_non_event_undated_candidate_is_kept(self) -> None:
        from vexic.longmemeval import _deep_eligible

        pref = _diag_candidate(candidate_id=1, category="preference", occurred_at=None)
        self.assertEqual(self._ids(_deep_eligible([pref])), [1])


if __name__ == "__main__":
    unittest.main()
