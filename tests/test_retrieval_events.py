import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from vexic.storage import init_db, record_fact_use_verdict, record_long_term_retrieval


class RetrievalEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_fact(self, fact_text: str, candidate_id: int) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO long_term_memory
                        (fact_text, subject, category, importance, confidence,
                         source_message_ids, promoted_from_candidate_id)
                    VALUES (?, 'Ryan', 'fact', 5, 0.8, '[1]', ?)
                    """,
                    (fact_text, candidate_id),
                )
                return int(cursor.lastrowid)

    def test_retrieval_records_one_event_per_fact_and_increments_counter(self) -> None:
        first = self._insert_fact("Ryan likes Python.", 1)
        second = self._insert_fact("Ryan dislikes meetings.", 2)

        record_long_term_retrieval(
            self.db_path,
            [first, second],
            session_id="telegram:42",
            query="what language does Ryan like?",
            forbidden_secret_values=[],
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            events = conn.execute(
                """
                SELECT fact_id, session_id, query, used, judged_at
                FROM retrieval_events
                ORDER BY fact_id
                """
            ).fetchall()
            counts = dict(
                conn.execute(
                    "SELECT id, retrieved_count FROM long_term_memory"
                ).fetchall()
            )

        self.assertEqual(len(events), 2)
        for row, fact_id in zip(events, [first, second]):
            self.assertEqual(row[0], fact_id)
            self.assertEqual(row[1], "telegram:42")
            self.assertEqual(row[2], "what language does Ryan like?")
            self.assertIsNone(row[3])  # unjudged, distinguishable from judged-unused
            self.assertIsNone(row[4])
        self.assertEqual(counts[first], 1)
        self.assertEqual(counts[second], 1)

    def test_retrieval_events_table_has_diagnostic_columns(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            columns = {
                row[1]: row[4]
                for row in conn.execute("PRAGMA table_info(retrieval_events)")
            }

        self.assertIn("rewritten_query", columns)
        self.assertEqual(columns["keyword_fact_ids"], "'[]'")
        self.assertEqual(columns["vector_fact_ids"], "'[]'")
        self.assertEqual(columns["fused_fact_ids"], "'[]'")

    def test_existing_retrieval_events_migrate_to_diagnostic_defaults(self) -> None:
        old_db_path = str(Path(self.temp_dir.name) / "old-memory.db")
        with closing(sqlite3.connect(old_db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE retrieval_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fact_id INTEGER NOT NULL,
                        session_id TEXT NOT NULL,
                        query TEXT NOT NULL,
                        retrieved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        used INTEGER CHECK (used IN (0, 1)),
                        judged_at DATETIME
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO retrieval_events
                        (fact_id, session_id, query, used, judged_at)
                    VALUES (123, 'legacy-session', 'legacy query', 1, '2026-06-01')
                    """
                )

        init_db(old_db_path)
        init_db(old_db_path)

        with closing(sqlite3.connect(old_db_path)) as conn:
            row = conn.execute(
                """
                SELECT fact_id, session_id, query, used, judged_at,
                       rewritten_query, keyword_fact_ids, vector_fact_ids, fused_fact_ids
                FROM retrieval_events
                WHERE id = 1
                """
            ).fetchone()
            columns = {
                column[1]: {"not_null": column[3], "default": column[4]}
                for column in conn.execute("PRAGMA table_info(retrieval_events)")
            }
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO long_term_memory
                        (fact_text, subject, category, importance, confidence,
                         source_message_ids, promoted_from_candidate_id)
                    VALUES ('Ryan likes Python.', 'Ryan', 'fact', 5, 0.8, '[1]', 1)
                    """
                )
                fact_id = int(cursor.lastrowid)

        self.assertEqual(
            tuple(row),
            (
                123,
                "legacy-session",
                "legacy query",
                1,
                "2026-06-01",
                None,
                "[]",
                "[]",
                "[]",
            ),
        )
        self.assertEqual(json.loads(row[6]), [])
        self.assertEqual(json.loads(row[7]), [])
        self.assertEqual(json.loads(row[8]), [])
        self.assertEqual(columns["keyword_fact_ids"], {"not_null": 1, "default": "'[]'"})
        self.assertEqual(columns["vector_fact_ids"], {"not_null": 1, "default": "'[]'"})
        self.assertEqual(columns["fused_fact_ids"], {"not_null": 1, "default": "'[]'"})

        event_ids = record_long_term_retrieval(
            old_db_path,
            [fact_id],
            session_id="default",
            query="python",
            rewritten_query="python language",
            keyword_fact_ids=[fact_id],
            vector_fact_ids=[],
            fused_fact_ids=[fact_id],
            forbidden_secret_values=[],
        )

        with closing(sqlite3.connect(old_db_path)) as conn:
            new_row = conn.execute(
                """
                SELECT rewritten_query, keyword_fact_ids, vector_fact_ids, fused_fact_ids
                FROM retrieval_events
                WHERE id = ?
                """,
                (event_ids[0],),
            ).fetchone()
            retrieved_count = conn.execute(
                "SELECT retrieved_count FROM long_term_memory WHERE id = ?",
                (fact_id,),
            ).fetchone()[0]

        self.assertEqual(new_row[0], "python language")
        self.assertEqual(json.loads(new_row[1]), [fact_id])
        self.assertEqual(json.loads(new_row[2]), [])
        self.assertEqual(json.loads(new_row[3]), [fact_id])
        self.assertEqual(retrieved_count, 1)

    def test_records_retrieval_diagnostics_on_each_event(self) -> None:
        first = self._insert_fact("Ryan likes Python.", 1)
        second = self._insert_fact("Ryan dislikes meetings.", 2)

        record_long_term_retrieval(
            self.db_path,
            [first, second],
            session_id="telegram:42",
            query="what language does Ryan like?",
            rewritten_query="python language",
            keyword_fact_ids=[first],
            vector_fact_ids=[second, first],
            fused_fact_ids=[first, second],
            forbidden_secret_values=[],
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            events = conn.execute(
                """
                SELECT rewritten_query, keyword_fact_ids, vector_fact_ids, fused_fact_ids
                FROM retrieval_events
                ORDER BY fact_id
                """
            ).fetchall()

        self.assertEqual(len(events), 2)
        for row in events:
            self.assertEqual(row[0], "python language")
            self.assertEqual(json.loads(row[1]), [first])
            self.assertEqual(json.loads(row[2]), [second, first])
            self.assertEqual(json.loads(row[3]), [first, second])

    def test_secret_in_query_fails_closed_and_writes_nothing(self) -> None:
        fact_id = self._insert_fact("Ryan likes Python.", 1)

        with self.assertRaises(ValueError):
            record_long_term_retrieval(
                self.db_path,
                [fact_id],
                session_id="default",
                query="my key is sk-secret-token-123",
                forbidden_secret_values=["sk-secret-token-123"],
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM retrieval_events"
            ).fetchone()[0]
            retrieved = conn.execute(
                "SELECT retrieved_count FROM long_term_memory WHERE id = ?",
                (fact_id,),
            ).fetchone()[0]
        self.assertEqual(event_count, 0)
        self.assertEqual(retrieved, 0)

    def test_secret_in_rewritten_query_fails_closed_and_writes_nothing(self) -> None:
        fact_id = self._insert_fact("Ryan likes Python.", 1)

        with self.assertRaises(ValueError):
            record_long_term_retrieval(
                self.db_path,
                [fact_id],
                session_id="default",
                query="python",
                rewritten_query="python sk-secret-token-123",
                forbidden_secret_values=["sk-secret-token-123"],
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM retrieval_events"
            ).fetchone()[0]
            retrieved = conn.execute(
                "SELECT retrieved_count FROM long_term_memory WHERE id = ?",
                (fact_id,),
            ).fetchone()[0]
        self.assertEqual(event_count, 0)
        self.assertEqual(retrieved, 0)

    def test_use_verdict_updates_events_and_used_count_together(self) -> None:
        used_fact = self._insert_fact("Ryan likes Python.", 1)
        unused_fact = self._insert_fact("Ryan dislikes meetings.", 2)
        event_ids = record_long_term_retrieval(
            self.db_path,
            [used_fact, unused_fact],
            session_id="default",
            query="what does Ryan like?",
            forbidden_secret_values=[],
        )

        record_fact_use_verdict(
            self.db_path,
            used_event_ids=[event_ids[0]],
            unused_event_ids=[event_ids[1]],
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            events = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT id, used, judged_at FROM retrieval_events"
                ).fetchall()
            }
            counts = dict(
                conn.execute("SELECT id, used_count FROM long_term_memory").fetchall()
            )
        self.assertEqual(events[event_ids[0]][0], 1)
        self.assertEqual(events[event_ids[1]][0], 0)
        self.assertIsNotNone(events[event_ids[0]][1])
        self.assertIsNotNone(events[event_ids[1]][1])
        self.assertEqual(counts[used_fact], 1)
        self.assertEqual(counts[unused_fact], 0)

    def test_use_verdict_counts_each_used_event_for_same_fact(self) -> None:
        fact_id = self._insert_fact("Ryan likes Python.", 1)
        first_event = record_long_term_retrieval(
            self.db_path,
            [fact_id],
            session_id="default",
            query="python",
            forbidden_secret_values=[],
        )[0]
        second_event = record_long_term_retrieval(
            self.db_path,
            [fact_id],
            session_id="default",
            query="python again",
            forbidden_secret_values=[],
        )[0]

        record_fact_use_verdict(
            self.db_path,
            used_event_ids=[first_event, second_event],
            unused_event_ids=[],
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored_count = conn.execute(
                "SELECT used_count FROM long_term_memory WHERE id = ?",
                (fact_id,),
            ).fetchone()[0]
            derived_count = conn.execute(
                """
                SELECT COUNT(CASE WHEN used = 1 THEN 1 END)
                FROM retrieval_events WHERE fact_id = ?
                """,
                (fact_id,),
            ).fetchone()[0]

        self.assertEqual(stored_count, 2)
        self.assertEqual(stored_count, derived_count)

    def test_use_verdict_is_safe_to_record_twice(self) -> None:
        fact_id = self._insert_fact("Ryan likes Python.", 1)
        event_ids = record_long_term_retrieval(
            self.db_path,
            [fact_id],
            session_id="default",
            query="python",
            forbidden_secret_values=[],
        )

        record_fact_use_verdict(
            self.db_path,
            used_event_ids=[event_ids[0]],
            unused_event_ids=[],
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE retrieval_events SET judged_at = ? WHERE id = ?",
                    ("2000-01-01 00:00:00", event_ids[0]),
                )

        record_fact_use_verdict(
            self.db_path,
            used_event_ids=[],
            unused_event_ids=[event_ids[0]],
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored_count = conn.execute(
                "SELECT used_count FROM long_term_memory WHERE id = ?",
                (fact_id,),
            ).fetchone()[0]
            used, judged_at = conn.execute(
                "SELECT used, judged_at FROM retrieval_events WHERE id = ?",
                (event_ids[0],),
            ).fetchone()
            derived = conn.execute(
                """
                SELECT COUNT(CASE WHEN used = 1 THEN 1 END)
                FROM retrieval_events WHERE fact_id = ?
                """,
                (fact_id,),
            ).fetchone()[0]
        self.assertEqual(stored_count, 1)
        self.assertEqual(stored_count, derived)
        self.assertEqual(used, 1)
        self.assertEqual(judged_at, "2000-01-01 00:00:00")

    def test_counters_are_derivable_from_events(self) -> None:
        # The losslessness claim (upstream ADR-0008): aggregate columns can always be
        # recomputed from retrieval_events after a rebuild.
        fact_id = self._insert_fact("Ryan likes Python.", 1)
        for _ in range(3):
            event_ids = record_long_term_retrieval(
                self.db_path,
                [fact_id],
                session_id="default",
                query="python",
                forbidden_secret_values=[],
            )
        record_fact_use_verdict(
            self.db_path, used_event_ids=[event_ids[0]], unused_event_ids=[]
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute(
                "SELECT retrieved_count, used_count FROM long_term_memory WHERE id = ?",
                (fact_id,),
            ).fetchone()
            derived = conn.execute(
                """
                SELECT COUNT(*), COUNT(CASE WHEN used = 1 THEN 1 END)
                FROM retrieval_events WHERE fact_id = ?
                """,
                (fact_id,),
            ).fetchone()
        self.assertEqual(tuple(stored), tuple(derived))
        self.assertEqual(tuple(stored), (3, 1))


class RetrievalQueryRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                # retrieved_at mirrors the schema CURRENT_TIMESTAMP default
                # (space-separated 'YYYY-MM-DD HH:MM:SS'), while callers pass an
                # ISO-8601 'T'-separated cutoff. The boundary row lands on the
                # cutoff day at a later instant and must survive: a naive string
                # '<' would sort ' ' before 'T' and wrongly expire it.
                conn.execute(
                    """
                    INSERT INTO retrieval_events
                        (fact_id, session_id, query, rewritten_query, retrieved_at, used)
                    VALUES (1, 'session-1', 'aged secret query', 'aged rewrite',
                            '2026-01-01 00:00:00', 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO retrieval_events
                        (fact_id, session_id, query, rewritten_query, retrieved_at)
                    VALUES (1, 'session-1', 'boundary query', 'boundary rewrite',
                            '2026-06-01 12:00:00')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO retrieval_events
                        (fact_id, session_id, query, rewritten_query, retrieved_at)
                    VALUES (1, 'session-1', 'fresh query', 'fresh rewrite',
                            '2026-06-30 00:00:00')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO candidate_retrieval_events
                        (candidate_id, session_id, query, retrieved_at)
                    VALUES (7, 'session-1', 'aged candidate query',
                            '2026-01-01 00:00:00')
                    """
                )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_expiry_blanks_aged_query_text_but_keeps_rows_and_counters(self) -> None:
        from vexic.storage.retention import expire_retrieval_queries

        expired = expire_retrieval_queries(
            self.db_path, older_than="2026-06-01T00:00:00+00:00"
        )

        self.assertEqual(expired, {"retrieval_events": 1, "candidate_retrieval_events": 1})
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT query, rewritten_query, used
                FROM retrieval_events ORDER BY id
                """
            ).fetchall()
            candidate_rows = conn.execute(
                "SELECT query FROM candidate_retrieval_events"
            ).fetchall()

        # Rows survive (retrieved_count/used_count derive from them); only the
        # content-bearing query text is blanked, and the use verdict is kept.
        # The boundary row (cutoff day, later instant) keeps its text.
        self.assertEqual(
            rows,
            [
                ("", None, 1),
                ("boundary query", "boundary rewrite", None),
                ("fresh query", "fresh rewrite", None),
            ],
        )
        self.assertEqual(candidate_rows, [("",)])

    def test_expiry_is_idempotent(self) -> None:
        from vexic.storage.retention import expire_retrieval_queries

        expire_retrieval_queries(self.db_path, older_than="2026-06-01T00:00:00+00:00")
        expired = expire_retrieval_queries(
            self.db_path, older_than="2026-06-01T00:00:00+00:00"
        )

        self.assertEqual(
            expired, {"retrieval_events": 0, "candidate_retrieval_events": 0}
        )


if __name__ == "__main__":
    unittest.main()
