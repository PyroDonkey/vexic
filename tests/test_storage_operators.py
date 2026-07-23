import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.storage import create_memory_rebuild_copy, init_db, save_messages
from vexic.storage.connection import connect


class RebuildCopyRedactionTests(unittest.TestCase):
    """Invariant 9: the file-copy guard must fail closed on any stored secret.

    SQLite is dynamically typed, so a column's declared type says nothing about
    what it actually holds. Host-owned extension tables in particular carry
    column types Vexic does not control.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source_db = self.root / "source.db"
        self.copy_db = self.root / "copy.db"
        init_db(str(self.source_db))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with closing(sqlite3.connect(self.source_db)) as conn:
            with conn:
                conn.execute(sql, params)

    def test_rebuild_copy_fails_closed_on_secret_in_host_extension_column(self) -> None:
        # A host-owned extension table Vexic must preserve but does not own the
        # schema of. `STRING` is not one of SQLite's type affinities keywords the
        # old declared-type heuristic recognised, yet it stores text fine.
        self._execute(
            "CREATE TABLE background_tool_audit ("
            "  id INTEGER PRIMARY KEY,"
            "  payload STRING"
            ")"
        )
        self._execute(
            "INSERT INTO background_tool_audit (payload) VALUES (?)",
            ("tool call used sk-secret-value",),
        )

        with self.assertRaises(ValueError):
            create_memory_rebuild_copy(
                str(self.source_db),
                str(self.copy_db),
                forbidden_secret_values=("sk-secret-value",),
            )

        self.assertFalse(self.copy_db.exists())

    def test_rebuild_copy_fails_closed_on_secret_in_blob_column(self) -> None:
        self._execute(
            "CREATE TABLE background_tool_audit (id INTEGER PRIMARY KEY, payload BLOB)"
        )
        self._execute(
            "INSERT INTO background_tool_audit (payload) VALUES (?)",
            (b"tool call used sk-secret-value",),
        )

        with self.assertRaises(ValueError):
            create_memory_rebuild_copy(
                str(self.source_db),
                str(self.copy_db),
                forbidden_secret_values=("sk-secret-value",),
            )

        self.assertFalse(self.copy_db.exists())

    def test_rebuild_copy_still_skips_retrieval_event_fact_id_lists(self) -> None:
        # These three columns hold JSON arrays of integer fact ids, never free
        # text, so they stay exempt: scanning them can only produce spurious
        # digit-substring matches.
        self._execute(
            "INSERT INTO long_term_memory (fact_text, subject, category, "
            "importance, confidence, source_message_ids, "
            "promoted_from_candidate_id) "
            "VALUES ('a fact', 'user', 'fact', 3, 0.9, '[1]', 1)"
        )
        self._execute(
            "INSERT INTO retrieval_events (fact_id, session_id, query, "
            "keyword_fact_ids, vector_fact_ids, fused_fact_ids) "
            "VALUES (1, 'session-a', 'a query', '[424242]', '[424242]', '[424242]')"
        )

        report = create_memory_rebuild_copy(
            str(self.source_db),
            str(self.copy_db),
            forbidden_secret_values=("424242",),
        )

        self.assertTrue(report.output_path.exists())

    def test_rebuild_copy_succeeds_on_clean_host_extension_table(self) -> None:
        self._execute(
            "CREATE TABLE background_tool_audit (id INTEGER PRIMARY KEY, payload STRING)"
        )
        self._execute(
            "INSERT INTO background_tool_audit (payload) VALUES ('nothing secret here')"
        )

        report = create_memory_rebuild_copy(
            str(self.source_db),
            str(self.copy_db),
            forbidden_secret_values=("sk-secret-value",),
        )

        self.assertTrue(report.output_path.exists())
        with closing(connect(str(self.copy_db))) as conn:
            preserved = conn.execute(
                "SELECT payload FROM background_tool_audit"
            ).fetchone()
        self.assertEqual(preserved[0], "nothing secret here")


class RebuildCopyIncompleteSourceTests(unittest.TestCase):
    """A half-restored source must fail loud, not be silently completed.

    `create_memory_rebuild_copy` initializes the schema on the *copy*, which is
    correct for a rebuildable projection but would otherwise substitute an empty
    canonical table for one the source lost.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source_db = self.root / "source.db"
        self.copy_db = self.root / "copy.db"
        init_db(str(self.source_db))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _execute(self, sql: str) -> None:
        with closing(sqlite3.connect(self.source_db)) as conn:
            with conn:
                conn.execute(sql)

    def _seed_one_message(self) -> None:
        save_messages(
            str(self.source_db),
            [ModelRequest(parts=[UserPromptPart(content="a recorded turn")])],
            session_id="session-a",
            agent_id="agent-a",
        )

    def test_rebuild_copy_rejects_source_missing_a_canonical_table(self) -> None:
        # `session_summaries` has no FTS or vector projection hanging off it, so
        # without an explicit check the copy simply gets an empty replacement and
        # the operator is told the rebuild succeeded.
        self._seed_one_message()
        self._execute("DROP TABLE session_summaries")

        with self.assertRaises(ValueError) as caught:
            create_memory_rebuild_copy(str(self.source_db), str(self.copy_db))

        self.assertIn("session_summaries", str(caught.exception))
        self.assertFalse(self.copy_db.exists())

    def test_rebuild_copy_names_every_missing_canonical_table(self) -> None:
        self._seed_one_message()
        self._execute("DROP TABLE long_term_memory")
        self._execute("DROP TABLE promotion_labels")

        with self.assertRaises(ValueError) as caught:
            create_memory_rebuild_copy(str(self.source_db), str(self.copy_db))

        message = str(caught.exception)
        self.assertIn("long_term_memory", message)
        self.assertIn("promotion_labels", message)
        self.assertFalse(self.copy_db.exists())

    def test_rebuild_copy_accepts_a_freshly_initialized_empty_source(self) -> None:
        report = create_memory_rebuild_copy(str(self.source_db), str(self.copy_db))

        self.assertTrue(report.output_path.exists())
        with closing(connect(str(self.copy_db))) as conn:
            facts = conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()
        self.assertEqual(facts[0], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
