"""Conformance tests for the LongMemEval miss-classification analysis.

The analysis module reads a completed LongMemEval run directory
(diagnostics.jsonl plus the per-question memory.db files) and buckets every
judged-recall miss into exactly one failing-stage class:

    class 1 -- fact absent from Tier 3 (extraction or promotion miss)
    class 2 -- fact present but ranked out of the returned top-k
    class 3 -- facts present but the answer requires joining/deriving them

It never mutates run artifacts: every memory.db is opened read-only.
"""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from vexic.longmemeval import question_db_path
from vexic.longmemeval_analysis import (
    RunAnalysisReport,
    _open_readonly,
    analyze_run,
    main as analysis_main,
)
from vexic.storage import init_db
from vexic.subagents.retrieval import reciprocal_rank_fusion


def _diagnostics_row(question_id: str, **overrides) -> dict:
    row = {
        "question_id": question_id,
        "question_type": "multi-session",
        "status": "ok",
        "answer_mode": "judged-recall",
        "judged_recall_pass": False,
        "answer_matchable": True,
        "answer_match_skipped_reason": None,
        "answer_found_in_tier1": True,
        "answer_extracted_to_tier2": False,
        "answer_candidate_rank": None,
        "answer_promoted_to_tier3": False,
        "answer_retrieved_from_tier3": False,
    }
    row.update(overrides)
    return row


class LongMemEvalAnalysisTests(unittest.TestCase):
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

    def _dataset_row(self, question_id: str, answer: str) -> dict:
        return {
            "question_id": question_id,
            "question_type": "multi-session",
            "question": "Where did the user run a marathon?",
            "answer": answer,
        }

    def _seed_question_db(
        self,
        question_id: str,
        facts: list[tuple[str, str]],
        event: dict | None = None,
    ) -> Path:
        db_path = question_db_path(self.run_dir, question_id)
        init_db(str(db_path))
        with closing(sqlite3.connect(db_path)) as conn:
            for index, (fact_text, subject) in enumerate(facts, start=1):
                conn.execute(
                    """
                    INSERT INTO long_term_memory (
                        fact_text, subject, category, importance, confidence,
                        source_message_ids, promoted_from_candidate_id
                    ) VALUES (?, ?, 'fact', 5, 0.9, '[1]', ?)
                    """,
                    (fact_text, subject, index),
                )
            if event is not None:
                conn.execute(
                    """
                    INSERT INTO retrieval_events (
                        fact_id, session_id, query,
                        keyword_fact_ids, vector_fact_ids, fused_fact_ids
                    ) VALUES (?, ?, 'q', ?, ?, ?)
                    """,
                    (
                        event.get("fact_id", 1),
                        f"longmemeval:{question_id}:answer",
                        json.dumps(event["keyword_fact_ids"]),
                        json.dumps(event["vector_fact_ids"]),
                        json.dumps(event["fused_fact_ids"]),
                    ),
                )
            conn.commit()
        return db_path

    def test_class1_when_answer_absent_from_long_term_memory(self) -> None:
        self._write_run(
            [
                _diagnostics_row("q-extraction", answer_extracted_to_tier2=False),
                _diagnostics_row(
                    "q-promotion",
                    answer_extracted_to_tier2=True,
                    answer_promoted_to_tier3=False,
                ),
            ]
        )
        dataset = self._write_dataset(
            [
                self._dataset_row("q-extraction", "Boston Marathon"),
                self._dataset_row("q-promotion", "Boston Marathon"),
            ]
        )
        for question_id in ("q-extraction", "q-promotion"):
            self._seed_question_db(
                question_id,
                [("The user likes trail running.", "user")],
            )

        report = analyze_run(self.run_dir, dataset)

        by_id = {miss.question_id: miss for miss in report.misses}
        self.assertEqual(by_id["q-extraction"].miss_class, 1)
        self.assertEqual(by_id["q-extraction"].sub_reason, "extraction_miss")
        self.assertEqual(by_id["q-promotion"].miss_class, 1)
        self.assertEqual(by_id["q-promotion"].sub_reason, "promotion_miss")
        self.assertFalse(by_id["q-extraction"].needs_manual_review)

    def test_gold_fact_detection_uses_answer_token_containment(self) -> None:
        self._write_run([_diagnostics_row("q-gold")])
        dataset = self._write_dataset([self._dataset_row("q-gold", "Boston Marathon")])
        self._seed_question_db(
            "q-gold",
            [
                ("The user ran the Boston Marathon in 2023.", "user"),
                ("The user's marathon in Boston was rainy.", "user"),
            ],
        )

        report = analyze_run(self.run_dir, dataset)

        miss = report.misses[0]
        # Ordered n-gram containment: fact 1 contains "boston marathon",
        # fact 2 ("marathon in boston") does not.
        self.assertEqual(miss.gold_fact_ids, [1])
        self.assertNotEqual(miss.miss_class, 1)

    def test_class2_below_return_k_recomputes_full_rrf_from_event_arrays(self) -> None:
        self._write_run([_diagnostics_row("q-ranked-out")])
        dataset = self._write_dataset(
            [self._dataset_row("q-ranked-out", "Boston Marathon")]
        )
        facts = [
            (f"Filler fact number {index}.", f"filler-{index}") for index in range(1, 7)
        ]
        facts.append(("The user ran the Boston Marathon in 2023.", "user"))
        keyword_ids = [1, 2, 3, 4, 5, 6, 7]
        vector_ids = [1, 2, 3, 4, 5, 6, 7]
        fused = reciprocal_rank_fusion([keyword_ids, vector_ids])
        self._seed_question_db(
            "q-ranked-out",
            facts,
            event={
                "keyword_fact_ids": keyword_ids,
                "vector_fact_ids": vector_ids,
                "fused_fact_ids": fused[:5],
            },
        )

        report = analyze_run(self.run_dir, dataset)

        miss = report.misses[0]
        self.assertEqual(miss.miss_class, 2)
        self.assertEqual(miss.sub_reason, "below_return_k")
        self.assertEqual(miss.gold_fact_ids, [7])
        self.assertEqual(miss.gold_fused_rank, fused.index(7) + 1)
        self.assertGreater(miss.gold_fused_rank, 5)

    def test_class2_outside_retrieve_k_when_gold_absent_from_both_arrays(self) -> None:
        self._write_run([_diagnostics_row("q-outside")])
        dataset = self._write_dataset(
            [self._dataset_row("q-outside", "Boston Marathon")]
        )
        facts = [
            (f"Filler fact number {index}.", f"filler-{index}") for index in range(1, 6)
        ]
        facts.append(("The user ran the Boston Marathon in 2023.", "user"))
        self._seed_question_db(
            "q-outside",
            facts,
            event={
                "keyword_fact_ids": [1, 2, 3, 4, 5],
                "vector_fact_ids": [1, 2, 3, 4, 5],
                "fused_fact_ids": [1, 2, 3, 4, 5],
            },
        )

        report = analyze_run(self.run_dir, dataset)

        miss = report.misses[0]
        self.assertEqual(miss.miss_class, 2)
        self.assertEqual(miss.sub_reason, "outside_retrieve_k")
        self.assertIsNone(miss.gold_fused_rank)

    def test_missing_retrieval_events_row_handled(self) -> None:
        self._write_run([_diagnostics_row("q-no-event")])
        dataset = self._write_dataset(
            [self._dataset_row("q-no-event", "Boston Marathon")]
        )
        self._seed_question_db(
            "q-no-event",
            [("The user ran the Boston Marathon in 2023.", "user")],
        )

        report = analyze_run(self.run_dir, dataset)

        miss = report.misses[0]
        self.assertEqual(miss.miss_class, 2)
        self.assertEqual(miss.sub_reason, "outside_retrieve_k")

    def test_retrieved_but_judged_miss_routes_to_manual_not_class2(self) -> None:
        self._write_run(
            [_diagnostics_row("q-judged", answer_retrieved_from_tier3=True)]
        )
        dataset = self._write_dataset([self._dataset_row("q-judged", "Boston Marathon")])
        self._seed_question_db(
            "q-judged",
            [
                ("The user ran the Boston Marathon in 2023.", "user"),
                ("Filler fact.", "filler"),
            ],
            event={
                "keyword_fact_ids": [1, 2],
                "vector_fact_ids": [1, 2],
                "fused_fact_ids": [1, 2],
            },
        )

        report = analyze_run(self.run_dir, dataset)

        miss = report.misses[0]
        self.assertEqual(miss.miss_class, 3)
        self.assertEqual(miss.sub_reason, "retrieved_but_judged_miss")
        self.assertTrue(miss.needs_manual_review)

    def test_class3_candidate_when_answer_never_verbatim_flags_manual_review(
        self,
    ) -> None:
        # Aggregation-style answers ("4 marathons") appear verbatim nowhere in
        # the transcript (answer_found_in_tier1=False), so no extracted fact
        # could ever contain them: that is a join/derivation candidate, not an
        # extraction miss.
        self._write_run(
            [_diagnostics_row("q-join", answer_found_in_tier1=False)]
        )
        dataset = self._write_dataset([self._dataset_row("q-join", "four marathons")])
        self._seed_question_db(
            "q-join",
            [
                ("The user ran the Boston Marathon in 2023.", "user"),
                ("The user ran the Chicago Marathon in 2024.", "user"),
            ],
        )

        report = analyze_run(self.run_dir, dataset)

        miss = report.misses[0]
        self.assertEqual(miss.miss_class, 3)
        self.assertEqual(miss.sub_reason, "answer_not_verbatim_requires_join")
        self.assertTrue(miss.needs_manual_review)

    def test_unmatchable_answer_row_reported_unclassified_with_evidence(self) -> None:
        self._write_run(
            [
                _diagnostics_row(
                    "q-yes",
                    answer_matchable=False,
                    answer_match_skipped_reason="unmatchable-answer",
                )
            ]
        )
        dataset = self._write_dataset([self._dataset_row("q-yes", "yes")])
        self._seed_question_db("q-yes", [("Some fact.", "user")])

        report = analyze_run(self.run_dir, dataset)

        miss = report.misses[0]
        self.assertIsNone(miss.miss_class)
        self.assertEqual(miss.sub_reason, "unmatchable_answer")
        self.assertTrue(miss.needs_manual_review)
        self.assertEqual(miss.evidence["answer"], "yes")

    def test_passing_and_non_judged_rows_are_not_classified(self) -> None:
        self._write_run(
            [
                _diagnostics_row("q-pass", judged_recall_pass=True),
                _diagnostics_row("q-error", status="error", judged_recall_pass=None),
                _diagnostics_row("q-miss"),
            ]
        )
        dataset = self._write_dataset(
            [
                self._dataset_row("q-pass", "Boston Marathon"),
                self._dataset_row("q-error", "Boston Marathon"),
                self._dataset_row("q-miss", "Boston Marathon"),
            ]
        )
        self._seed_question_db("q-miss", [("Filler.", "user")])

        report = analyze_run(self.run_dir, dataset)

        self.assertEqual([miss.question_id for miss in report.misses], ["q-miss"])
        self.assertEqual(
            report.judged_recall_by_question_type,
            {"multi-session": {"supported": 1, "total": 2}},
        )

    def test_subject_histogram_median_max_distinct_per_db_and_aggregate(self) -> None:
        self._write_run(
            [
                _diagnostics_row("q-a", judged_recall_pass=True),
                _diagnostics_row("q-b", judged_recall_pass=True),
            ]
        )
        dataset = self._write_dataset(
            [
                self._dataset_row("q-a", "Boston Marathon"),
                self._dataset_row("q-b", "Boston Marathon"),
            ]
        )
        self._seed_question_db(
            "q-a",
            [
                ("Fact one.", "ryan"),
                ("Fact two.", "ryan"),
                ("Fact three.", "boston"),
            ],
        )
        self._seed_question_db("q-b", [("Fact four.", "chicago")])

        report = analyze_run(self.run_dir, dataset)

        by_id = {hist.question_id: hist for hist in report.subject_histograms}
        self.assertEqual(by_id["q-a"].total_facts, 3)
        self.assertEqual(by_id["q-a"].distinct_subjects, 2)
        self.assertEqual(by_id["q-a"].median_facts_per_subject, 1.5)
        self.assertEqual(by_id["q-a"].max_facts_per_subject, 2)
        self.assertEqual(by_id["q-b"].median_facts_per_subject, 1)
        # Aggregate pools every (db, subject) count: [2, 1, 1] -> median 1.
        self.assertEqual(report.aggregate_histogram.total_facts, 4)
        self.assertEqual(report.aggregate_histogram.distinct_subjects, 3)
        self.assertEqual(report.aggregate_histogram.median_facts_per_subject, 1)
        self.assertEqual(report.aggregate_histogram.max_facts_per_subject, 2)

    def test_aggregate_histogram_pools_all_subjects_not_just_top_n(self) -> None:
        # 10 subjects with 3 facts each + 11 subjects with 1 fact each. The
        # top-10 display list holds only the 3s; the aggregate median must be
        # computed over all 21 subject counts (median 1), not the display list.
        self._write_run([_diagnostics_row("q-many", judged_recall_pass=True)])
        dataset = self._write_dataset([self._dataset_row("q-many", "unused")])
        facts = []
        for index in range(10):
            facts.extend(
                (f"Repeated fact {index}-{copy}.", f"heavy-{index}")
                for copy in range(3)
            )
        facts.extend((f"Single fact {index}.", f"light-{index}") for index in range(11))
        self._seed_question_db("q-many", facts)

        report = analyze_run(self.run_dir, dataset)

        self.assertEqual(report.aggregate_histogram.distinct_subjects, 21)
        self.assertEqual(report.aggregate_histogram.median_facts_per_subject, 1)

    def test_analysis_opens_memory_db_read_only(self) -> None:
        db_path = self._seed_question_db("q-ro", [("Fact.", "user")])

        with closing(_open_readonly(db_path)) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO long_term_memory (fact_text) VALUES ('x')")

    def test_cli_writes_report_json_and_prints_summary(self) -> None:
        self._write_run([_diagnostics_row("q-cli")])
        dataset = self._write_dataset([self._dataset_row("q-cli", "Boston Marathon")])
        self._seed_question_db("q-cli", [("Filler.", "user")])

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = analysis_main(
                ["--run-dir", str(self.run_dir), "--dataset", str(dataset)]
            )

        self.assertEqual(exit_code, 0)
        report_path = self.run_dir / "analysis_report.json"
        self.assertTrue(report_path.exists())
        report = RunAnalysisReport.model_validate_json(
            report_path.read_text(encoding="utf-8")
        )
        self.assertEqual(len(report.misses), 1)
        self.assertIn("class 1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
