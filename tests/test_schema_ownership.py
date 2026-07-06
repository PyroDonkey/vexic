import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.models import FactCandidate
from vexic.storage.candidates import PromotionCandidate
from vexic.storage.longterm import LongTermFact
from vexic.storage.transcript import single_message_adapter


class VexicSchemaOwnershipTests(unittest.TestCase):
    def test_scoped_tables_have_nullable_agent_scope_columns(self) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                columns = {
                    table: {
                        row[1]: {"not_null": row[3], "default": row[4]}
                        for row in conn.execute(f"PRAGMA table_info({table})")
                    }
                    for table in (
                        "messages",
                        "messages_fts",
                        "source_transcript_ledger",
                        "memory_candidates",
                        "dream_runs",
                        "long_term_memory",
                        "retrieval_events",
                        "candidate_retrieval_events",
                        "session_summaries",
                    )
                }
                tombstone_columns = {
                    row[1]: {"not_null": row[3], "default": row[4]}
                    for row in conn.execute("PRAGMA table_info(scope_tombstones)")
                }

        for table, table_columns in columns.items():
            with self.subTest(table=table):
                self.assertIn("agent_id", table_columns)
                self.assertEqual(table_columns["agent_id"]["not_null"], 0)
                self.assertIsNone(table_columns["agent_id"]["default"])
        self.assertIn("target_agent_id", tombstone_columns)
        self.assertEqual(tombstone_columns["target_agent_id"]["not_null"], 0)

    def test_legacy_messages_migrate_to_shared_agent_scope_without_backfill(self) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            with closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT NOT NULL DEFAULT 'default',
                            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                            message_json TEXT NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO messages (session_id, message_json)
                        VALUES ('legacy-session', ?)
                        """,
                        (
                            single_message_adapter.dump_json(
                                ModelRequest(
                                    parts=[
                                        UserPromptPart(content="legacy cedar")
                                    ]
                                )
                            ).decode(),
                        ),
                    )

            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    """
                    SELECT id, session_id, message_json, agent_id
                    FROM messages
                    """
                ).fetchone()

        self.assertEqual(row[:2], (1, "legacy-session"))
        self.assertIsNone(row[3])
        self.assertIn("legacy cedar", row[2])

    def test_init_db_does_not_create_background_tool_audit(self) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertIn("messages", tables)
        self.assertNotIn("background_tool_audit", tables)

    def test_source_transcript_ledger_has_agent_scoped_unique_source_key(self) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                message_id = conn.execute(
                    "INSERT INTO messages (message_json) VALUES ('{}')"
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO source_transcript_ledger
                        (source_host, source_session_id, source_message_id, agent_id, message_id)
                    VALUES ('claude-code', 'session-1', 'uuid-1', 'agent-a', ?)
                    """,
                    (message_id,),
                )
                conn.execute(
                    """
                    INSERT INTO source_transcript_ledger
                        (source_host, source_session_id, source_message_id, agent_id, message_id)
                    VALUES ('claude-code', 'session-1', 'uuid-1', 'agent-b', ?)
                    """,
                    (message_id,),
                )
                conn.execute(
                    """
                    INSERT INTO source_transcript_ledger
                        (source_host, source_session_id, source_message_id, message_id)
                    VALUES ('claude-code', 'session-1', 'uuid-1', ?)
                    """,
                    (message_id,),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO source_transcript_ledger
                            (source_host, source_session_id, source_message_id, agent_id, message_id)
                        VALUES ('claude-code', 'session-1', 'uuid-1', 'agent-a', ?)
                        """,
                        (message_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO source_transcript_ledger
                            (source_host, source_session_id, source_message_id, message_id)
                        VALUES ('claude-code', 'session-1', 'uuid-1', ?)
                        """,
                        (message_id,),
                    )

    def test_legacy_source_transcript_ledger_unique_key_migrates_to_agent_scope(self) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            with closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            message_json TEXT NOT NULL
                        )
                        """
                    )
                    message_json = single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="legacy cedar")])
                    ).decode()
                    message_id = conn.execute(
                        "INSERT INTO messages (message_json) VALUES (?)",
                        (message_json,),
                    ).lastrowid
                    conn.execute(
                        """
                        CREATE TABLE source_transcript_ledger (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            source_host TEXT NOT NULL,
                            source_session_id TEXT NOT NULL,
                            source_message_id TEXT NOT NULL,
                            agent_id TEXT,
                            message_id INTEGER NOT NULL REFERENCES messages(id),
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE (source_host, source_session_id, source_message_id)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO source_transcript_ledger
                            (source_host, source_session_id, source_message_id, message_id)
                        VALUES ('claude-code', 'session-1', 'uuid-1', ?)
                        """,
                        (message_id,),
                    )

            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO source_transcript_ledger
                        (source_host, source_session_id, source_message_id, agent_id, message_id)
                    VALUES ('claude-code', 'session-1', 'uuid-1', 'agent-a', ?)
                    """,
                    (message_id,),
                )
                count = conn.execute(
                    "SELECT COUNT(*) FROM source_transcript_ledger"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO source_transcript_ledger
                            (source_host, source_session_id, source_message_id, agent_id, message_id)
                        VALUES ('claude-code', 'session-1', 'uuid-1', 'agent-a', ?)
                        """,
                        (message_id,),
                    )

        self.assertEqual(count, 2)

    def test_fresh_init_db_adds_occurred_at_to_memory_candidates_and_long_term_memory(
        self,
    ) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                candidate_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")
                }
                long_term_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(long_term_memory)")
                }

        self.assertIn("occurred_at", candidate_columns)
        self.assertIn("occurred_at", long_term_columns)

    def test_init_db_twice_is_idempotent_and_occurred_at_persists(self) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            # Re-running init_db against an already-migrated DB must not raise,
            # and the additive _ensure_column backfill must remain a no-op.
            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                candidate_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")
                }
                long_term_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(long_term_memory)")
                }

        self.assertIn("occurred_at", candidate_columns)
        self.assertIn("occurred_at", long_term_columns)


class OccurredAtFieldDefaultTests(unittest.TestCase):
    """occurred_at is a nullable, flexible event-time string that
    defaults to None so existing callers that construct these types without
    it keep working."""

    def test_long_term_fact_defaults_occurred_at_to_none(self) -> None:
        fact = LongTermFact(
            fact_id=1,
            fact_text="likes tea",
            subject="user",
            category="preference",
            importance=5,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
        )

        self.assertIsNone(fact.occurred_at)

    def test_long_term_fact_accepts_occurred_at(self) -> None:
        fact = LongTermFact(
            fact_id=1,
            fact_text="started new job",
            subject="user",
            category="event",
            importance=5,
            confidence=0.9,
            source_message_ids=[1],
            retrieved_count=0,
            used_count=0,
            occurred_at="2025-03",
        )

        self.assertEqual(fact.occurred_at, "2025-03")

    def test_promotion_candidate_defaults_occurred_at_to_none(self) -> None:
        candidate = PromotionCandidate(
            candidate_id=1,
            fact_text="likes tea",
            subject="user",
            category="preference",
            confidence=0.9,
            importance=5,
            hit_count=1,
            last_seen_at=datetime(2026, 1, 1),
            rem_boost=0.0,
            embedding=[0.0],
        )

        self.assertIsNone(candidate.occurred_at)

    def test_promotion_candidate_accepts_occurred_at(self) -> None:
        candidate = PromotionCandidate(
            candidate_id=1,
            fact_text="started new job",
            subject="user",
            category="event",
            confidence=0.9,
            importance=5,
            hit_count=1,
            last_seen_at=datetime(2026, 1, 1),
            rem_boost=0.0,
            embedding=[0.0],
            occurred_at="2025-03-14",
        )

        self.assertEqual(candidate.occurred_at, "2025-03-14")

    def test_fact_candidate_defaults_occurred_at_to_none(self) -> None:
        candidate = FactCandidate(
            fact_text="likes tea",
            subject="user",
            category="preference",
            importance=5,
            confidence=0.9,
        )

        self.assertIsNone(candidate.occurred_at)

    def test_fact_candidate_accepts_occurred_at(self) -> None:
        candidate = FactCandidate(
            fact_text="started new job",
            subject="user",
            category="event",
            importance=5,
            confidence=0.9,
            occurred_at="2025-03",
        )

        self.assertEqual(candidate.occurred_at, "2025-03")


if __name__ == "__main__":
    unittest.main()
