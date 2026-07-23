from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.embeddings import EMBEDDING_DIM
from vexic.models import FactCandidate
from vexic.storage import (
    SourceTranscriptInput,
    commit_deep_cycle,
    commit_dream_cycle,
    ingest_source_messages,
    init_db,
    single_message_adapter,
)
from vexic.storage.promotion import PromotionDecision


def _unit_vector() -> list[float]:
    return [1.0] + [0.0] * (EMBEDDING_DIM - 1)


class OperatorCliTests(unittest.TestCase):
    """Behavior of `vexic operator ...` through the public CLI entry point."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "memory.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed(self, transcript_text: str = "cedar operator transcript") -> None:
        init_db(str(self.db_path))
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content=transcript_text)])
        ).decode()
        result = ingest_source_messages(
            str(self.db_path),
            [
                SourceTranscriptInput(
                    source_host="local-vexic",
                    source_session_id="session-a",
                    source_message_id="message-a",
                    message_json=message_json,
                )
            ],
            session_id="session-a",
            agent_id="agent-a",
        )
        message_id = result[0].message_id
        assert message_id is not None
        commit_dream_cycle(
            str(self.db_path),
            [
                FactCandidate(
                    fact_text="cedar operator durable fact",
                    subject="Ryan",
                    category="fact",
                    importance=7,
                    confidence=0.9,
                    source_message_ids=[message_id],
                )
            ],
            candidate_embeddings=[_unit_vector()],
            agent_id="agent-a",
            status="ok",
            started_at="2026-06-25T00:00:00+00:00",
            finished_at="2026-06-25T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=message_id,
        )
        commit_deep_cycle(
            str(self.db_path),
            [PromotionDecision(1, _unit_vector())],
            agent_id="agent-a",
            started_at="2026-06-25T00:01:00+00:00",
            finished_at="2026-06-25T00:01:01+00:00",
        )

    def test_review_export_writes_markdown_review_and_exits_zero(self) -> None:
        from vexic.cli import main as vexic_main

        self._seed()
        output = self.root / "review.md"

        code = vexic_main(
            [
                "operator",
                "review-export",
                "--db-path",
                str(self.db_path),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(code, 0)
        rendered = output.read_text(encoding="utf-8")
        self.assertIn("# Memory Review Export", rendered)
        self.assertIn("cedar operator durable fact", rendered)

    def test_rebuild_copy_writes_a_rebuilt_copy_and_exits_zero(self) -> None:
        from vexic.cli import main as vexic_main

        self._seed()
        copy_path = self.root / "rebuild-copy.db"

        code = vexic_main(
            [
                "operator",
                "rebuild-copy",
                "--db-path",
                str(self.db_path),
                "--output",
                str(copy_path),
            ]
        )

        self.assertEqual(code, 0)
        self.assertTrue(copy_path.exists())
        with closing(sqlite3.connect(copy_path)) as conn:
            facts = conn.execute("SELECT fact_text FROM long_term_memory").fetchall()
            fts_rows = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        self.assertEqual(facts, [("cedar operator durable fact",)])
        self.assertEqual(fts_rows, 1)

    def test_review_export_without_output_exits_nonzero(self) -> None:
        from vexic.cli import main as vexic_main

        self._seed()

        code = vexic_main(["operator", "review-export", "--db-path", str(self.db_path)])

        self.assertNotEqual(code, 0)

    def test_review_export_on_a_missing_database_exits_nonzero(self) -> None:
        # A typo'd --db-path must not silently create an empty database and
        # hand the operator a review of nothing.
        from vexic.cli import main as vexic_main

        missing_db = self.root / "not-there.db"
        output = self.root / "review.md"

        code = vexic_main(
            [
                "operator",
                "review-export",
                "--db-path",
                str(missing_db),
                "--output",
                str(output),
            ]
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(output.exists())
        self.assertFalse(missing_db.exists())

    def test_rebuild_copy_refuses_to_copy_a_forbidden_secret_value(self) -> None:
        # Redaction fails closed: the copy guard runs before the file copy, so
        # no partial copy is left on disk for the operator to mistake for good.
        from vexic.cli import main as vexic_main

        self._seed("the deploy key is sk-operator-secret")
        copy_path = self.root / "rebuild-copy.db"

        code = vexic_main(
            [
                "operator",
                "rebuild-copy",
                "--db-path",
                str(self.db_path),
                "--output",
                str(copy_path),
                "--forbidden-value",
                "sk-operator-secret",
            ]
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(copy_path.exists())

    def test_review_export_refuses_to_clobber_an_existing_file(self) -> None:
        from vexic.cli import main as vexic_main

        self._seed()
        output = self.root / "review.md"
        output.write_text("earlier review", encoding="utf-8")

        code = vexic_main(
            [
                "operator",
                "review-export",
                "--db-path",
                str(self.db_path),
                "--output",
                str(output),
            ]
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "earlier review")

        overwritten = vexic_main(
            [
                "operator",
                "review-export",
                "--db-path",
                str(self.db_path),
                "--output",
                str(output),
                "--overwrite",
            ]
        )

        self.assertEqual(overwritten, 0)
        self.assertIn(
            "# Memory Review Export", output.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
