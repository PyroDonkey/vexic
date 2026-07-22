"""Conformance tests for the opt-in preference rubric-delta rescore.

``vexic.longmemeval_rescore`` reopens a completed LongMemEval run directory,
re-judges every preference judged-recall MISS row through the WP1
rubric-aware render, and writes ``preference_rescore.jsonl`` beside the run.
It only reads run artifacts (every ``memory.db`` opened read-only) plus the
source dataset, and writes exactly the one rescore file.
"""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from vexic.longmemeval import (
    LONGMEMEVAL_RECALL_JUDGE_PREFERENCE_PROMPT_VERSION,
    LongMemEvalRecallJudgeInput,
    LongMemEvalRecallJudgeVerdict,
    question_db_path,
)
from vexic.longmemeval_analysis import PreferenceRescoreRow
from vexic.longmemeval_rescore import (
    build_parser,
    main as rescore_main,
    rescore_preference_rows,
)
from vexic.storage import init_db


class _FakeRecallJudge:
    """Duck-typed recall judge for CI: records inputs, returns a fixed verdict."""

    def __init__(self, verdict: LongMemEvalRecallJudgeVerdict) -> None:
        self.verdict = verdict
        self.calls: list[LongMemEvalRecallJudgeInput] = []

    async def __call__(
        self,
        judge_input: LongMemEvalRecallJudgeInput,
    ) -> LongMemEvalRecallJudgeVerdict:
        self.calls.append(judge_input)
        return self.verdict


def _diagnostics_row(question_id: str, **overrides) -> dict:
    row = {
        "question_id": question_id,
        "question_type": "single-session-preference",
        "status": "ok",
        "answer_mode": "judged-recall",
        "judged_recall_pass": False,
        "candidate_fallback_used": False,
        "retrieved_long_term_fact_count": 0,
        "judge_verdict": "not_supported",
    }
    row.update(overrides)
    return row


class LongMemEvalRescoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_run(self, diagnostics_rows: list[dict]) -> None:
        (self.run_dir / "diagnostics.jsonl").write_text(
            "\n".join(json.dumps(row) for row in diagnostics_rows) + "\n",
            encoding="utf-8",
        )

    def _write_dataset(self, rows: list[dict]) -> Path:
        dataset_path = self.root / "dataset.json"
        dataset_path.write_text(json.dumps(rows), encoding="utf-8")
        return dataset_path

    def _dataset_row(
        self,
        question_id: str,
        *,
        question: str = "What coffee does the user prefer?",
        answer: str = "The user prefers strong, dark-roast coffee.",
    ) -> dict:
        return {
            "question_id": question_id,
            "question_type": "single-session-preference",
            "question": question,
            "answer": answer,
        }

    def _seed_question_db(
        self,
        question_id: str,
        facts: list[tuple[str, str, str, str | None]],
        *,
        fused_fact_ids: list[int],
        keyword_fact_ids: list[int] | None = None,
        vector_fact_ids: list[int] | None = None,
    ) -> Path:
        """Seed a question memory.db with Tier-3 facts and one answer event.

        ``facts`` is a list of ``(fact_text, subject, category, occurred_at)``.
        Fact ids are assigned 1..N in list order.
        """
        db_path = question_db_path(self.run_dir, question_id)
        init_db(str(db_path))
        with closing(sqlite3.connect(db_path)) as conn:
            for index, (fact_text, subject, category, occurred_at) in enumerate(
                facts, start=1
            ):
                conn.execute(
                    """
                    INSERT INTO long_term_memory (
                        fact_text, subject, category, importance, confidence,
                        source_message_ids, occurred_at,
                        promoted_from_candidate_id
                    ) VALUES (?, ?, ?, 5, 0.9, '[1]', ?, ?)
                    """,
                    (fact_text, subject, category, occurred_at, index),
                )
            conn.execute(
                """
                INSERT INTO retrieval_events (
                    fact_id, session_id, query,
                    keyword_fact_ids, vector_fact_ids, fused_fact_ids
                ) VALUES (?, ?, 'q', ?, ?, ?)
                """,
                (
                    fused_fact_ids[0] if fused_fact_ids else 1,
                    f"longmemeval:{question_id}:answer",
                    json.dumps(
                        keyword_fact_ids if keyword_fact_ids is not None else fused_fact_ids
                    ),
                    json.dumps(
                        vector_fact_ids if vector_fact_ids is not None else fused_fact_ids
                    ),
                    json.dumps(fused_fact_ids),
                ),
            )
            conn.commit()
        return db_path

    def _run(self, dataset_path: Path, judge: _FakeRecallJudge, **kwargs) -> Path:
        import asyncio

        return asyncio.run(
            rescore_preference_rows(
                self.run_dir,
                dataset_path,
                judge_model_group="claude",
                judge_scorer=judge,
                **kwargs,
            )
        )

    def _read_rescore(self, artifact: Path) -> list[PreferenceRescoreRow]:
        return [
            PreferenceRescoreRow.model_validate_json(line)
            for line in artifact.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_rescore_writes_preference_rescore_artifact_shape(self) -> None:
        self._write_run(
            [
                _diagnostics_row(
                    "q-pref",
                    retrieved_long_term_fact_count=1,
                    judge_verdict="not_supported",
                )
            ]
        )
        dataset_path = self._write_dataset([self._dataset_row("q-pref")])
        self._seed_question_db(
            "q-pref",
            [("Likes espresso.", "user", "preference", None)],
            fused_fact_ids=[1],
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported", reason="Rubric satisfied.", confidence=0.8
            )
        )

        artifact = self._run(dataset_path, judge)

        self.assertEqual(artifact, self.run_dir / "preference_rescore.jsonl")
        rows = self._read_rescore(artifact)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.question_id, "q-pref")
        self.assertEqual(row.question_type, "single-session-preference")
        self.assertEqual(row.original_verdict, "not_supported")
        self.assertEqual(row.rubric_verdict, "supported")
        self.assertEqual(row.rubric_reason, "Rubric satisfied.")
        self.assertAlmostEqual(row.rubric_confidence, 0.8)
        self.assertIsNone(row.judge_model_id)
        self.assertTrue(
            row.judge_prompt_version.endswith("+preference-rubric-v1")
        )
        self.assertEqual(
            row.judge_prompt_version,
            LONGMEMEVAL_RECALL_JUDGE_PREFERENCE_PROMPT_VERSION,
        )
        self.assertTrue(row.reconstruction_complete)

    def test_rescore_reconstructs_retrieved_facts_in_event_sorted_order(self) -> None:
        self._write_run(
            [_diagnostics_row("q-events", retrieved_long_term_fact_count=2)]
        )
        dataset_path = self._write_dataset([self._dataset_row("q-events")])
        # RRF/fused order is [1, 2] (older event first); event-sort must reorder
        # to newest-occurred first -> fact 2 then fact 1.
        self._seed_question_db(
            "q-events",
            [
                ("Ran a 5k in early 2020.", "user", "event", "2020-03-01"),
                ("Ran a marathon in late 2023.", "user", "event", "2023-11-01"),
            ],
            fused_fact_ids=[1, 2],
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="partial", reason="Some evidence.", confidence=0.5
            )
        )

        artifact = self._run(dataset_path, judge)

        self.assertEqual(len(judge.calls), 1)
        judge_input = judge.calls[0]
        self.assertEqual(
            judge_input.retrieved_fact_texts,
            ("Ran a marathon in late 2023.", "Ran a 5k in early 2020."),
        )
        self.assertEqual(judge_input.question_type, "single-session-preference")
        rows = self._read_rescore(artifact)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].reconstruction_complete)

    def test_rescore_skips_non_preference_and_supported_rows(self) -> None:
        self._write_run(
            [
                # Non-preference miss: wrong question type for rubric rescore.
                _diagnostics_row(
                    "q-multi",
                    question_type="multi-session",
                    retrieved_long_term_fact_count=1,
                ),
                # Preference row the eval-time judge already scored supported.
                _diagnostics_row(
                    "q-supported",
                    judged_recall_pass=True,
                    judge_verdict="supported",
                    retrieved_long_term_fact_count=1,
                ),
            ]
        )
        dataset_path = self._write_dataset(
            [self._dataset_row("q-multi"), self._dataset_row("q-supported")]
        )
        self._seed_question_db(
            "q-multi",
            [("Some fact.", "user", "fact", None)],
            fused_fact_ids=[1],
        )
        self._seed_question_db(
            "q-supported",
            [("Likes tea.", "user", "preference", None)],
            fused_fact_ids=[1],
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported", reason="ok", confidence=0.9
            )
        )

        artifact = self._run(dataset_path, judge)

        self.assertEqual(self._read_rescore(artifact), [])
        self.assertEqual(len(judge.calls), 0)

    def test_rescore_marks_reconstruction_incomplete_on_candidate_fallback(self) -> None:
        self._write_run(
            [
                _diagnostics_row(
                    "q-fallback",
                    candidate_fallback_used=True,
                    retrieved_long_term_fact_count=0,
                )
            ]
        )
        dataset_path = self._write_dataset([self._dataset_row("q-fallback")])
        # The answer came from a Tier-2 candidate note: no Tier-3 fused ids.
        self._seed_question_db(
            "q-fallback",
            [("Likes cats.", "user", "preference", None)],
            fused_fact_ids=[],
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="not_supported", reason="No facts.", confidence=0.4
            )
        )

        artifact = self._run(dataset_path, judge)

        rows = self._read_rescore(artifact)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].reconstruction_complete)
        # Still judged (over an empty reconstructed set), still written.
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(judge.calls[0].retrieved_fact_texts, ())

    def test_rescore_marks_reconstruction_incomplete_on_count_mismatch(self) -> None:
        # Diagnostics recorded returning 2 facts, but only 1 id is reconstructable.
        self._write_run(
            [_diagnostics_row("q-mismatch", retrieved_long_term_fact_count=2)]
        )
        dataset_path = self._write_dataset([self._dataset_row("q-mismatch")])
        self._seed_question_db(
            "q-mismatch",
            [("Likes hiking.", "user", "preference", None)],
            fused_fact_ids=[1],
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="partial", reason="Partial.", confidence=0.5
            )
        )

        artifact = self._run(dataset_path, judge)

        rows = self._read_rescore(artifact)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].reconstruction_complete)

    def test_rescore_cli_skips_without_allow_live(self) -> None:
        self._write_run([_diagnostics_row("q-pref")])
        dataset_path = self._write_dataset([self._dataset_row("q-pref")])
        # A bogus adapter path that would fail if the CLI ever tried to load it;
        # without --allow-live the adapter must never be touched.
        bogus_adapter = self.root / "does_not_exist_adapter.py"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = rescore_main(
                [
                    "--run-dir",
                    str(self.run_dir),
                    "--dataset",
                    str(dataset_path),
                    "--adapter",
                    str(bogus_adapter),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("Skipping", buffer.getvalue())
        self.assertFalse(
            (self.run_dir / "preference_rescore.jsonl").exists()
        )

    def test_rescore_cli_requires_adapter_with_allow_live(self) -> None:
        self._write_run([_diagnostics_row("q-pref")])
        dataset_path = self._write_dataset([self._dataset_row("q-pref")])
        code = rescore_main(
            [
                "--run-dir",
                str(self.run_dir),
                "--dataset",
                str(dataset_path),
                "--allow-live",
            ]
        )
        self.assertEqual(code, 2)

    def test_rescore_forbidden_secret_guard(self) -> None:
        secret = "sk-super-secret-token"
        self._write_run(
            [_diagnostics_row("q-secret", retrieved_long_term_fact_count=1)]
        )
        dataset_path = self._write_dataset([self._dataset_row("q-secret")])
        # A forbidden secret value leaked into a reconstructed fact must fail
        # closed before the rescore row is written.
        self._seed_question_db(
            "q-secret",
            [(f"The API key is {secret}.", "user", "preference", None)],
            fused_fact_ids=[1],
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported", reason="ok", confidence=0.9
            )
        )

        with self.assertRaises(Exception) as ctx:
            self._run(dataset_path, judge, forbidden_secret_values=(secret,))
        self.assertIn("forbidden secret", str(ctx.exception).lower())
        self.assertEqual(len(judge.calls), 0)

    def test_rescore_overwrites_prior_artifact(self) -> None:
        self._write_run(
            [_diagnostics_row("q-pref", retrieved_long_term_fact_count=1)]
        )
        dataset_path = self._write_dataset([self._dataset_row("q-pref")])
        self._seed_question_db(
            "q-pref",
            [("Likes espresso.", "user", "preference", None)],
            fused_fact_ids=[1],
        )
        judge = _FakeRecallJudge(
            LongMemEvalRecallJudgeVerdict(
                verdict="supported", reason="ok", confidence=0.9
            )
        )

        artifact = self._run(dataset_path, judge)
        self.assertEqual(len(self._read_rescore(artifact)), 1)

        # Re-running regenerates rather than appends: still exactly one row.
        artifact = self._run(dataset_path, judge)
        self.assertEqual(len(self._read_rescore(artifact)), 1)


if __name__ == "__main__":
    unittest.main()
