import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

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

    def test_source_transcript_ledger_has_unique_source_key(self) -> None:
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
                        (source_host, source_session_id, source_message_id, message_id)
                    VALUES ('claude-code', 'session-1', 'uuid-1', ?)
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


if __name__ == "__main__":
    unittest.main()
