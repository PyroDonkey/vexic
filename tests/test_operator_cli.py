from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.cli import main as vexic_main
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

    def _run(self, *argv: str) -> tuple[int, str]:
        """Drive the public CLI entry point, capturing its stdout."""
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = vexic_main(list(argv))
        return code, stdout.getvalue()

    def _seed(
        self,
        transcript_text: str = "cedar operator transcript",
        *,
        fact_text: str = "cedar operator durable fact",
        subject: str = "Ryan",
    ) -> None:
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
                    fact_text=fact_text,
                    subject=subject,
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
        self._seed()
        output = self.root / "review.md"

        code, stdout = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(self.db_path),
            "--output",
            str(output),
        )

        self.assertEqual(code, 0)
        rendered = output.read_text(encoding="utf-8")
        self.assertIn("# Memory Review Export", rendered)
        self.assertIn("cedar operator durable fact", rendered)
        # The JSON summary is the documented machine-readable result. Pin the
        # whole object: a renamed key silently breaks that contract, and the
        # doc-drift gate does not read JSON keys. rows_exported is 2 here --
        # the one Tier 2 candidate plus the one Tier 3 fact it was promoted to.
        self.assertEqual(
            json.loads(stdout),
            {
                "ok": True,
                "output_path": str(output),
                "rows_exported": 2,
                "bytes_written": len(output.read_bytes()),
            },
        )

    def test_rebuild_copy_writes_a_rebuilt_copy_and_exits_zero(self) -> None:
        self._seed()
        copy_path = self.root / "rebuild-copy.db"

        code, stdout = self._run(
            "operator",
            "rebuild-copy",
            "--db-path",
            str(self.db_path),
            "--output",
            str(copy_path),
        )

        self.assertEqual(code, 0)
        self.assertTrue(copy_path.exists())
        with closing(sqlite3.connect(copy_path)) as conn:
            facts = conn.execute("SELECT fact_text FROM long_term_memory").fetchall()
            fts_rows = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        self.assertEqual(facts, [("cedar operator durable fact",)])
        self.assertEqual(fts_rows, 1)
        # Same contract pin as review-export: the counters an operator reads to
        # confirm the rebuild landed must keep their documented names.
        self.assertEqual(
            json.loads(stdout),
            {
                "ok": True,
                "output_path": str(copy_path),
                "messages_fts_rows": 1,
                "candidate_fts_rows": 1,
                "long_term_fts_rows": 1,
                "candidate_counters_recomputed": 1,
                "long_term_counters_recomputed": 1,
            },
        )

    def test_review_export_without_output_exits_2_and_writes_nothing(self) -> None:
        self._seed()
        before = sorted(path.name for path in self.root.iterdir())

        code, stdout = self._run(
            "operator", "review-export", "--db-path", str(self.db_path)
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)

    def test_review_export_on_a_missing_database_exits_nonzero(self) -> None:
        # A typo'd --db-path must not silently create an empty database and
        # hand the operator a review of nothing.
        missing_db = self.root / "not-there.db"
        output = self.root / "review.md"

        code, _ = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(missing_db),
            "--output",
            str(output),
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(output.exists())
        self.assertFalse(missing_db.exists())

    def test_review_export_on_an_empty_file_exits_nonzero_and_leaves_it_alone(
        self,
    ) -> None:
        # An empty file passes an `is_file()` check, after which `init_db`
        # would happily stamp a fresh empty schema onto it and the operator
        # would read a review of nothing. The source must be recognizably a
        # memory database before either command touches it.
        empty_db = self.root / "empty.db"
        empty_db.touch()
        output = self.root / "review.md"

        code, _ = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(empty_db),
            "--output",
            str(output),
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(output.exists())
        self.assertEqual(empty_db.read_bytes(), b"")

    def test_review_export_refuses_to_write_over_its_own_source(self) -> None:
        # `--overwrite` is meant to replace a stale review, not to let the
        # markdown report land on top of the database it was rendered from.
        # Mid-incident that is unrecoverable data loss reported as success.
        self._seed()
        before = self.db_path.read_bytes()

        code, stdout = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(self.db_path),
            "--output",
            str(self.db_path),
            "--overwrite",
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_review_export_refuses_an_output_symlinked_to_its_source(self) -> None:
        # Same data loss, spelled differently: the operator points --output at
        # a link that resolves to the live database.
        self._seed()
        before = self.db_path.read_bytes()
        link = self.root / "review-link.md"
        link.symlink_to(self.db_path)

        code, stdout = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(self.db_path),
            "--output",
            str(link),
            "--overwrite",
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_rebuild_copy_refuses_an_output_that_is_its_own_source(self) -> None:
        # rebuild-copy must not depend on the underlying operator function's
        # own refuse-to-clobber check to protect the source; the CLI rejects
        # the aliased target itself, and the source survives untouched.
        self._seed()
        before = self.db_path.read_bytes()

        code, stdout = self._run(
            "operator",
            "rebuild-copy",
            "--db-path",
            str(self.db_path),
            "--output",
            str(self.db_path),
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_rebuild_copy_rejects_a_forbidden_secret_before_copying(self) -> None:
        # Redaction fails closed, and specifically *before* the file copy: the
        # secret sits in the candidate/fact `subject` column, which the
        # pre-copy database scan reads but the copy's own projection-repair
        # guard (message_json and fact_text only) does not. So an absent copy
        # here can only mean the pre-copy guard stopped the run.
        self._seed(subject="sk-operator-secret")
        copy_path = self.root / "rebuild-copy.db"

        code, _ = self._run(
            "operator",
            "rebuild-copy",
            "--db-path",
            str(self.db_path),
            "--output",
            str(copy_path),
            "--forbidden-value",
            "sk-operator-secret",
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(copy_path.exists())

    def test_review_export_refuses_to_write_a_forbidden_secret_value(self) -> None:
        self._seed(fact_text="the deploy key is sk-operator-secret")
        output = self.root / "review.md"

        code, _ = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(self.db_path),
            "--output",
            str(output),
            "--forbidden-value",
            "sk-operator-secret",
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(output.exists())

    def test_review_export_refuses_to_clobber_an_existing_file(self) -> None:
        self._seed()
        output = self.root / "review.md"
        output.write_text("earlier review", encoding="utf-8")

        code, _ = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(self.db_path),
            "--output",
            str(output),
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "earlier review")

        overwritten, _ = self._run(
            "operator",
            "review-export",
            "--db-path",
            str(self.db_path),
            "--output",
            str(output),
            "--overwrite",
        )

        self.assertEqual(overwritten, 0)
        self.assertIn("# Memory Review Export", output.read_text(encoding="utf-8"))

    def test_rebuild_copy_refuses_to_clobber_an_existing_output(self) -> None:
        # rebuild-copy has no --overwrite escape hatch: a recovery copy must
        # never overwrite whatever the operator already has at that path.
        self._seed()
        copy_path = self.root / "rebuild-copy.db"
        copy_path.write_bytes(b"earlier copy")

        code, _ = self._run(
            "operator",
            "rebuild-copy",
            "--db-path",
            str(self.db_path),
            "--output",
            str(copy_path),
        )

        self.assertNotEqual(code, 0)
        self.assertEqual(copy_path.read_bytes(), b"earlier copy")


if __name__ == "__main__":
    unittest.main()
