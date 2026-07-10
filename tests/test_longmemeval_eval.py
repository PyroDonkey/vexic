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
    ingest_instance,
    create_run_paths,
    parse_longmemeval_instance,
    question_db_path,
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


if __name__ == "__main__":
    unittest.main()
