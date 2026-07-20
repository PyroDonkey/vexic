"""Tests for the oracle-evidence experiment harness.

Deterministic coverage of the read-only machinery: oracle-fixture parsing and
its drift guard, offline fused[:k] reconstruction from persisted retrieval
events, constituent-capture and pre-fusion-pool ceiling maths, the
membership-set headroom builder, supported-only pass accounting, judge-repeat
aggregation with an injected stub judge, and the --bind-only / skip CLI paths.

No provider calls: the judge is always a stub, so the whole module is exercised
without --allow-live. Mirrors tests/test_longmemeval_analysis.py (real
read-only SQLite run DBs) and tests/test_ablate_extraction_prompts.py (stubbed
provider boundary + CLI exit codes).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import TestCase

from vexic.longmemeval import (
    LongMemEvalRecallJudgeInput,
    LongMemEvalRecallJudgeVerdict,
)
from vexic.longmemeval_analysis import _question_path_component
from vexic.storage import init_db
from vexic.subagents.retrieval import reciprocal_rank_fusion

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "oracle_evidence_experiment.py"


def _load_module() -> ModuleType:
    """Load scripts/oracle_evidence_experiment.py, which is a script, not a
    package (same pattern as tests/test_ablate_extraction_prompts.py)."""
    spec = importlib.util.spec_from_file_location(
        "oracle_evidence_experiment", _MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


oee = _load_module()


def _verdict(value: str, confidence: float = 0.9) -> LongMemEvalRecallJudgeVerdict:
    return LongMemEvalRecallJudgeVerdict(verdict=value, reason="stub", confidence=confidence)


class _StubJudge:
    """Deterministic judge: returns a verdict keyed by the set of fact texts it
    is shown, so a test can script exactly which condition passes."""

    def __init__(self, by_signature: dict[frozenset[str], str], default: str = "not_supported") -> None:
        self._by_signature = by_signature
        self._default = default
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, judge_input: LongMemEvalRecallJudgeInput) -> LongMemEvalRecallJudgeVerdict:
        self.calls.append(judge_input.retrieved_fact_texts)
        signature = frozenset(judge_input.retrieved_fact_texts)
        return _verdict(self._by_signature.get(signature, self._default))


class OracleFixtureTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _seed_db(
        self,
        question_id: str,
        facts: list[tuple[str, str, str | None]],
        event: dict | None = None,
    ) -> Path:
        """facts: (fact_text, category, occurred_at). retirement always 0."""
        db_path = self.run_dir / _question_path_component(question_id) / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(str(db_path))
        with closing(sqlite3.connect(db_path)) as conn:
            for index, (fact_text, category, occurred_at) in enumerate(facts, start=1):
                conn.execute(
                    """
                    INSERT INTO long_term_memory (
                        fact_text, subject, category, importance, confidence,
                        occurred_at, source_message_ids, promoted_from_candidate_id
                    ) VALUES (?, ?, ?, 5, 0.9, ?, '[1]', ?)
                    """,
                    (fact_text, "user", category, occurred_at, index),
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

    def _write_diagnostics(self, rows: list[dict]) -> None:
        path = self.run_dir / "diagnostics.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def _fixture(self, entries: list[dict]) -> Path:
        path = self.root / "oracle.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def _entry(self, question_id: str, **overrides) -> dict:
        entry = {
            "question_id": question_id,
            "run_dir": str(self.run_dir),
            "question": "How many?",
            "gold_answer": 2,
            "constituent_fact_ids": [1, 2],
            "expected_fact_texts": ["Fact one.", "Fact two."],
            "note": "hand-curated",
        }
        entry.update(overrides)
        return entry

    def test_load_fixture_returns_typed_entries(self) -> None:
        path = self._fixture([self._entry("q1"), self._entry("q2", gold_answer=3)])

        entries = oee.load_oracle_fixture(path)

        self.assertEqual([e.question_id for e in entries], ["q1", "q2"])
        self.assertEqual(entries[0].constituent_fact_ids, [1, 2])
        self.assertEqual(entries[1].gold_answer, 3)

    def test_load_fixture_rejects_length_mismatch(self) -> None:
        path = self._fixture(
            [self._entry("q1", constituent_fact_ids=[1, 2, 3])]
        )
        with self.assertRaises(oee.OracleFixtureError):
            oee.load_oracle_fixture(path)

    def test_load_fixture_rejects_duplicate_question_id(self) -> None:
        path = self._fixture([self._entry("q1"), self._entry("q1")])
        with self.assertRaises(oee.OracleFixtureError):
            oee.load_oracle_fixture(path)

    def test_resolve_constituents_returns_live_texts_in_order(self) -> None:
        self._seed_db(
            "q1",
            [("Fact one.", "fact", None), ("Fact two.", "fact", None)],
        )
        entry = oee.load_oracle_fixture(self._fixture([self._entry("q1")]))[0]

        texts = oee.resolve_constituents(entry)

        self.assertEqual(texts, ["Fact one.", "Fact two."])

    def test_resolve_constituents_fails_on_reassigned_text(self) -> None:
        # id 2 exists but now holds a different fact than the fixture recorded:
        # a re-run renumbered the rows. Recording text is not enough; the guard
        # must catch the mismatch, not just a deleted id.
        self._seed_db(
            "q1",
            [("Fact one.", "fact", None), ("A totally different fact.", "fact", None)],
        )
        entry = oee.load_oracle_fixture(self._fixture([self._entry("q1")]))[0]
        with self.assertRaises(oee.OracleFixtureError):
            oee.resolve_constituents(entry)

    def test_drift_guard_is_exact_not_normalized(self) -> None:
        # A whitespace/case-only difference must trip the guard: an exact match
        # is the correct check since the fixture copies text verbatim.
        self._seed_db("q1", [("Fact one.", "fact", None), ("Fact two.", "fact", None)])
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", expected_fact_texts=["fact one.", "Fact two."])]
            )
        )[0]
        with self.assertRaises(oee.OracleFixtureError):
            oee.resolve_constituents(entry)

    def test_load_fixture_rejects_duplicate_constituent_ids(self) -> None:
        path = self._fixture(
            [
                self._entry(
                    "q1",
                    constituent_fact_ids=[1, 1],
                    expected_fact_texts=["Fact one.", "Fact one."],
                )
            ]
        )
        with self.assertRaises(oee.OracleFixtureError):
            oee.load_oracle_fixture(path)

    def test_reconstruct_fused_reproduces_persisted_fused_column(self) -> None:
        # The central faithfulness premise: offline reconstruction of fused[:5]
        # must equal what the run actually stored, over an asymmetric >5 pool.
        keyword_ids = [1, 2, 3, 4, 5, 6, 7, 8]
        vector_ids = [8, 3, 1, 9, 2, 10, 4, 5]
        stored = reciprocal_rank_fusion([keyword_ids, vector_ids])[:5]
        facts = [(f"Fact {i}.", "fact", None) for i in range(1, 11)]
        self._seed_db(
            "q1",
            facts,
            event={
                "keyword_fact_ids": keyword_ids,
                "vector_fact_ids": vector_ids,
                "fused_fact_ids": stored,
            },
        )
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Fact 1."])]
            )
        )[0]
        kw, vec, fused_stored = oee._read_answer_arrays(entry)
        self.assertEqual(oee.reconstruct_fused(kw, vec, len(fused_stored)), fused_stored)

    def test_preflight_fails_when_stored_fused_diverges(self) -> None:
        # A stored fused that disagrees with RRF over the pools means the run's
        # fusion wiring drifted; preflight must fail loud, not judge silently.
        facts = [(f"Fact {i}.", "fact", None) for i in range(1, 8)]
        self._seed_db(
            "q1",
            facts,
            event={
                "keyword_fact_ids": [1, 2, 3, 4, 5],
                "vector_fact_ids": [5, 4, 3, 2, 1],
                "fused_fact_ids": [7, 6, 5, 4, 3],  # not what RRF would produce
            },
        )
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Fact 1."])]
            )
        )[0]
        with self.assertRaises(oee.OracleFixtureError):
            oee.preflight([entry])

    def test_preflight_fails_on_missing_event_with_constituents(self) -> None:
        self._seed_db("q1", [("Fact one.", "fact", None)])  # no retrieval event
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Fact one."])]
            )
        )[0]
        with self.assertRaises(oee.OracleFixtureError):
            oee.preflight([entry])

    def test_condition_fact_texts_include_retired_to_match_production(self) -> None:
        # Production fetch_long_term_facts selects purely by id; a fused id at a
        # retired fact is still presented, so condition texts must include it.
        db_path = self.run_dir / _question_path_component("q1") / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(str(db_path))
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO long_term_memory (fact_text, subject, category, "
                "importance, confidence, source_message_ids, "
                "promoted_from_candidate_id, retired) "
                "VALUES ('Retired fact.', 'user', 'fact', 5, 0.9, '[1]', 1, 1)"
            )
            conn.commit()
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Retired fact."])]
            )
        )[0]
        self.assertEqual(oee.condition_fact_texts(entry, [1]), ["Retired fact."])

    def test_condition_fact_texts_tolerate_pre_mentioned_at_schema(self) -> None:
        # Frozen run DBs predate the mentioned_at migration (ADR 0037). The
        # condition fetch must not assume the current schema.
        db_path = self.run_dir / _question_path_component("q1") / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(str(db_path))
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO long_term_memory (fact_text, subject, category, "
                "importance, confidence, source_message_ids, "
                "promoted_from_candidate_id) "
                "VALUES ('Old-schema fact.', 'user', 'event', 5, 0.9, '[1]', 1)"
            )
            conn.execute("ALTER TABLE long_term_memory DROP COLUMN mentioned_at")
            conn.commit()
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Old-schema fact."])]
            )
        )[0]
        self.assertEqual(oee.condition_fact_texts(entry, [1]), ["Old-schema fact."])

    def test_event_sorted_matches_production_with_events_sorted(self) -> None:
        # The copied _event_sorted must stay byte-faithful to production's
        # _with_events_sorted (ADR 0037); compare both on the same rows.
        from vexic.subagents.retrieval import _with_events_sorted

        rows = [
            oee._FactRow(1, "older event", "event", "2020-01-01", None, "2024-01-01"),
            oee._FactRow(2, "a preference", "preference", None, None, "2024-01-01"),
            oee._FactRow(3, "newer event", "event", "2023-06-01", None, "2024-01-01"),
        ]
        mine = [r.fact_id for r in oee._event_sorted(rows)]
        prod = [f.fact_id for f in _with_events_sorted(rows)]
        self.assertEqual(mine, prod)
        self.assertEqual(mine, [3, 2, 1])

    def test_run_experiment_stops_and_records_on_budget_exhaustion(self) -> None:
        # Two questions, budget too small to finish both: exhaustion stops the
        # run, records budget_exhausted, keeps completed results.
        self._write_diagnostics(
            [
                {"question_id": q, "judge_verdict": "not_supported", "status": "ok"}
                for q in ("q1", "q2")
            ]
        )
        for qid in ("q1", "q2"):
            self._seed_db(
                qid,
                [("Fact.", "fact", None)],
                event={
                    "keyword_fact_ids": [1],
                    "vector_fact_ids": [1],
                    "fused_fact_ids": [1],
                },
            )
        entries = oee.load_oracle_fixture(
            self._fixture(
                [
                    self._entry(q, constituent_fact_ids=[1], expected_fact_texts=["Fact."])
                    for q in ("q1", "q2")
                ]
            )
        )
        # 5 conditions x 1 repeat = 5 calls per question; cap at 6 finishes only
        # the first question.
        budget = oee.ProviderBudget(6)
        doc = asyncio.run(
            oee.run_experiment(entries, _StubJudge({}), repeats=1, budget=budget)
        )
        self.assertTrue(doc["budget_exhausted"])
        self.assertEqual(len(doc["results"]), 1)

    def test_resolve_constituents_fails_on_missing_id(self) -> None:
        self._seed_db("q1", [("Fact one.", "fact", None)])  # only id 1 exists
        entry = oee.load_oracle_fixture(self._fixture([self._entry("q1")]))[0]
        with self.assertRaises(oee.OracleFixtureError):
            oee.resolve_constituents(entry)

    def test_reconstruct_fused_matches_rrf_truncated_to_k(self) -> None:
        keyword_ids = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        vector_ids = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
        expected = reciprocal_rank_fusion([keyword_ids, vector_ids])
        for k in (5, 8, 10):
            self.assertEqual(
                oee.reconstruct_fused(keyword_ids, vector_ids, k), expected[:k]
            )

    def test_load_retrieval_arrays_reads_persisted_event(self) -> None:
        self._seed_db(
            "q1",
            [("Fact one.", "fact", None), ("Fact two.", "fact", None)],
            event={
                "keyword_fact_ids": [2, 1],
                "vector_fact_ids": [1, 2],
                "fused_fact_ids": [1, 2],
            },
        )
        entry = oee.load_oracle_fixture(self._fixture([self._entry("q1")]))[0]

        keyword_ids, vector_ids = oee.load_retrieval_arrays(entry)

        self.assertEqual(keyword_ids, [2, 1])
        self.assertEqual(vector_ids, [1, 2])

    def test_constituent_capture_is_fraction_in_fused_slice(self) -> None:
        capture = oee.constituent_capture([3, 7, 99], fused_k_ids=[1, 3, 5, 7])

        self.assertEqual(capture["captured"], 2)
        self.assertEqual(capture["total"], 3)
        self.assertAlmostEqual(capture["fraction"], 2 / 3)
        self.assertEqual(capture["retrieved_count"], 4)

    def test_condition_fact_texts_apply_event_time_reorder(self) -> None:
        # Two event facts (fused order: older first) get permuted newest-first;
        # the non-event fact between them keeps its relevance slot (ADR 0037).
        self._seed_db(
            "q1",
            [
                ("Older event.", "event", "2021-01-01"),
                ("A preference.", "preference", None),
                ("Newer event.", "event", "2023-05-05"),
            ],
        )
        entry = oee.load_oracle_fixture(self._fixture([self._entry("q1")]))[0]

        texts = oee.condition_fact_texts(entry, [1, 2, 3])

        self.assertEqual(texts, ["Newer event.", "A preference.", "Older event."])

    def test_run_question_aggregates_pass_fraction_over_repeats(self) -> None:
        facts = [
            ("Led project alpha.", "fact", None),
            ("Led project beta.", "fact", None),
        ]
        # A wide-enough pool so the k=5..15 sweep slices are all this 2-fact set.
        self._seed_db(
            "q1",
            facts,
            event={
                "keyword_fact_ids": [1, 2],
                "vector_fact_ids": [1, 2],
                "fused_fact_ids": [1, 2],
            },
        )
        entry = oee.load_oracle_fixture(
            self._fixture(
                [
                    self._entry(
                        "q1",
                        gold_answer=2,
                        constituent_fact_ids=[1, 2],
                        expected_fact_texts=["Led project alpha.", "Led project beta."],
                    )
                ]
            )
        )[0]
        # Judge passes only when it sees BOTH constituents (oracle set); the
        # fused slices here also hold both, so they pass too. Baseline verdict is
        # informational -- the recorded miss defines N elsewhere.
        both = frozenset(["Led project alpha.", "Led project beta."])
        judge = _StubJudge({both: "supported"})
        budget = oee.ProviderBudget(100)

        result = asyncio.run(
            oee.run_question(entry, judge, repeats=3, budget=budget)
        )

        self.assertEqual(result["oracle"]["pass_fraction"], 1.0)
        self.assertEqual(result["oracle"]["n"], 3)
        # 5 conditions (oracle + k in {5,8,10,15}) x 3 repeats = 15 judge calls.
        self.assertEqual(len(judge.calls), 15)
        self.assertEqual(budget.used, 15)
        self.assertEqual(result["capture"]["15"]["fraction"], 1.0)

    def test_judge_error_is_recorded_and_does_not_abort_run(self) -> None:
        # A judge that raises on every call must not crash the run: each repeat
        # records "error", pass/partial are None (no graded signal), budget still
        # spent, and run_experiment completes and yields a result row.
        self._seed_db(
            "q1",
            [("Only fact.", "fact", None)],
            event={
                "keyword_fact_ids": [1],
                "vector_fact_ids": [1],
                "fused_fact_ids": [1],
            },
        )
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Only fact."])]
            )
        )[0]

        self._write_diagnostics(
            [{"question_id": "q1", "judge_verdict": "not_supported", "status": "ok"}]
        )

        class _Boom:
            calls = 0

            async def __call__(self, judge_input):
                _Boom.calls += 1
                raise RuntimeError("unparseable structured output")

        budget = oee.ProviderBudget(100)
        doc = asyncio.run(
            oee.run_experiment([entry], _Boom(), repeats=2, budget=budget)
        )

        self.assertEqual(len(doc["results"]), 1)
        oracle = doc["results"][0]["oracle"]
        self.assertIsNone(oracle["pass_fraction"])
        self.assertEqual(oracle["errors"], 2)
        self.assertEqual(oracle["verdicts"], ["error", "error"])
        self.assertGreater(budget.used, 0)  # calls attempted -> budget spent

    def test_run_question_supported_only_partial_is_not_a_pass(self) -> None:
        self._seed_db(
            "q1",
            [("Only fact.", "fact", None)],
            event={
                "keyword_fact_ids": [1],
                "vector_fact_ids": [1],
                "fused_fact_ids": [1],
            },
        )
        entry = oee.load_oracle_fixture(
            self._fixture(
                [
                    self._entry(
                        "q1",
                        constituent_fact_ids=[1],
                        expected_fact_texts=["Only fact."],
                    )
                ]
            )
        )[0]
        judge = _StubJudge({}, default="partial")  # every condition -> partial

        result = asyncio.run(
            oee.run_question(entry, judge, repeats=2, budget=oee.ProviderBudget(100))
        )

        self.assertEqual(result["oracle"]["pass_fraction"], 0.0)
        self.assertEqual(result["oracle"]["partial_fraction"], 1.0)

    def test_pool_ceiling_flags_constituents_outside_prefusion_union(self) -> None:
        # id 99 is in neither the keyword nor vector retrieve_k pool: no return_k
        # widening can ever surface it -- a RETRIEVE_K ceiling.
        ceiling = oee.pool_ceiling(
            [3, 7, 99], keyword_ids=[1, 2, 3], vector_ids=[7, 8]
        )
        self.assertEqual(ceiling, [99])

    def test_build_headroom_reports_membership_sets_not_a_subtraction(self) -> None:
        def result(qid, oracle_pass, sweep_pass, oracle_complete=True, cap15=1.0):
            return {
                "question_id": qid,
                "oracle_complete": oracle_complete,
                "oracle": {"pass_fraction": oracle_pass},
                "sweep": {
                    str(k): {"pass_fraction": p}
                    for k, p in zip((5, 8, 10, 15), sweep_pass)
                },
                "capture": {"15": {"fraction": cap15}},
                "pool_ceiling": [],
            }

        results = [
            result("qA", 1.0, [0.0, 0.0, 0.0, 0.0]),  # oracle+full capture -> derivation
            result("qB", 1.0, [0.0, 1.0, 1.0, 1.0]),  # widening (k>5) fixes it
            result("qC", 0.0, [0.0, 0.0, 0.0, 0.0]),  # complete evidence, still fails
            result("qD", 0.0, [0.0, 1.0, 0.0, 0.0]),  # k=8 passes, k=10/15 regress
            result("qE", 1.0, [1.0, 0.0, 0.0, 0.0]),  # only baseline k5 re-judges pass
            result("qF", 0.0, [0.0, 0.0, 0.0, 0.0], oracle_complete=False),  # gap
            result("qG", 1.0, [0.0, 0.0, 0.0, 0.0], cap15=0.0),  # oracle ok, never retrieved
        ]

        headroom = oee.build_headroom(results, threshold=0.5)

        self.assertEqual(headroom["n"], 7)
        self.assertEqual(set(headroom["set_completeness_reachable"]), {"qB", "qD"})
        self.assertEqual(set(headroom["baseline_rejudge_pass"]), {"qE"})
        self.assertEqual(set(headroom["combined_ceiling"]), {"qA", "qB", "qE", "qG"})
        # qA/qE: oracle passes, no k>5 passes, full capture -> derivation needed.
        self.assertEqual(set(headroom["derivation_needed"]), {"qA", "qE"})
        # qG: oracle passes but capture@15 == 0 -> retrieval-depth-limited, NOT
        # derivation.
        self.assertEqual(set(headroom["retrieval_depth_limited"]), {"qG"})
        self.assertIn("qD", headroom["nonmonotonic_regressions"])
        self.assertEqual(set(headroom["upstream_extraction_gap"]), {"qF"})
        self.assertEqual(
            set(headroom["derivation_ceiling_complete_evidence"]), {"qC"}
        )

    def test_recorded_verdict_reads_diagnostics(self) -> None:
        self._write_diagnostics(
            [
                {"question_id": "q1", "judge_verdict": "partial", "status": "ok"},
                {"question_id": "q2", "judge_verdict": "supported", "status": "ok"},
            ]
        )
        entry = oee.load_oracle_fixture(self._fixture([self._entry("q1")]))[0]
        self.assertEqual(oee.recorded_verdict(entry), "partial")

    def test_recorded_verdict_none_when_run_failed(self) -> None:
        # A failed run's stale verdict must not be trusted (Greptile P1): status
        # != ok, or a judge_error, means the recorded outcome is unknown.
        self._write_diagnostics(
            [
                {"question_id": "q1", "judge_verdict": "supported", "status": "error"},
            ]
        )
        entry = oee.load_oracle_fixture(self._fixture([self._entry("q1")]))[0]
        self.assertIsNone(oee.recorded_verdict(entry))

    def test_run_experiment_excludes_unknown_recorded_from_n(self) -> None:
        # No diagnostics row -> unknown outcome -> not a confirmed miss, excluded
        # from the denominator N rather than silently scored (Greptile P1).
        self._seed_db(
            "q1",
            [("Only fact.", "fact", None)],
            event={
                "keyword_fact_ids": [1],
                "vector_fact_ids": [1],
                "fused_fact_ids": [1],
            },
        )
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Only fact."])]
            )
        )[0]
        judge = _StubJudge({})
        budget = oee.ProviderBudget(100)
        doc = asyncio.run(
            oee.run_experiment([entry], judge, repeats=1, budget=budget)
        )
        self.assertIn("q1", doc["unknown_recorded"])
        self.assertEqual(doc["headroom"]["n"], 0)
        self.assertEqual(len(judge.calls), 0)

    def test_build_headroom_no_oracle_signal_not_a_derivation_ceiling(self) -> None:
        # All oracle repeats errored -> pass_fraction None -> no signal; must NOT
        # be reported as a derivation ceiling (Greptile P1).
        result = {
            "question_id": "qX",
            "oracle_complete": True,
            "oracle": {"pass_fraction": None},
            "sweep": {
                str(k): {"pass_fraction": 0.0} for k in (5, 8, 10, 15)
            },
            "capture": {"15": {"fraction": 1.0}},
            "pool_ceiling": [],
        }
        headroom = oee.build_headroom([result])
        self.assertEqual(headroom["no_oracle_signal"], ["qX"])
        self.assertEqual(headroom["derivation_ceiling_complete_evidence"], [])
        self.assertEqual(headroom["combined_ceiling"], [])

    def test_build_headroom_no_widened_signal_not_derivation_or_depth(self) -> None:
        # Oracle passes, but every widened (k>5) sweep errored -> no graded
        # widened data. Must NOT be labeled derivation_needed or
        # retrieval_depth_limited from capture alone (Greptile P1 follow-up).
        result = {
            "question_id": "qX",
            "oracle_complete": True,
            "oracle": {"pass_fraction": 1.0},
            "sweep": {
                "5": {"pass_fraction": 0.0},
                "8": {"pass_fraction": None},
                "10": {"pass_fraction": None},
                "15": {"pass_fraction": None},
            },
            "capture": {"15": {"fraction": 1.0}},
            "pool_ceiling": [],
        }
        headroom = oee.build_headroom([result])
        self.assertEqual(headroom["no_widened_signal"], ["qX"])
        self.assertEqual(headroom["derivation_needed"], [])
        self.assertEqual(headroom["retrieval_depth_limited"], [])
        self.assertIn("qX", headroom["combined_ceiling"])  # oracle still graded

    def test_render_table_all_error_condition_not_selected_as_best_k(self) -> None:
        result = {
            "question_id": "qX",
            "recorded_verdict": "partial",
            "oracle": {"pass_fraction": None},
            "baseline": {"pass_fraction": None},
            "sweep": {str(k): {"pass_fraction": None} for k in (5, 8, 10, 15)},
            "capture": {"15": {"fraction": 1.0}},
        }
        table = oee.render_table([result])
        # best-k column and best-k@ must be "--", not a fake 0.00 / a k label.
        row = table.splitlines()[-1]
        self.assertIn("| -- | -- |", row)  # best-k and best-k@ both blank

    def test_run_experiment_excludes_curated_recorded_pass_from_n(self) -> None:
        # A curated question the run RECORDED as supported is a curation error --
        # it is not a miss, so it must NOT be scored (no budget) and NOT counted
        # in N; surface it under curation_warnings only.
        self._write_diagnostics([{"question_id": "q1", "judge_verdict": "supported"}])
        self._seed_db(
            "q1",
            [("Only fact.", "fact", None)],
            event={
                "keyword_fact_ids": [1],
                "vector_fact_ids": [1],
                "fused_fact_ids": [1],
            },
        )
        entry = oee.load_oracle_fixture(
            self._fixture(
                [self._entry("q1", constituent_fact_ids=[1], expected_fact_texts=["Only fact."])]
            )
        )[0]
        judge = _StubJudge({})
        budget = oee.ProviderBudget(100)
        doc = asyncio.run(
            oee.run_experiment([entry], judge, repeats=1, budget=budget)
        )
        self.assertIn("q1", doc["curation_warnings"])
        self.assertEqual(doc["headroom"]["n"], 0)
        self.assertEqual(doc["results"], [])
        self.assertEqual(len(judge.calls), 0)  # not scored -> no budget spent
        self.assertEqual(budget.used, 0)


class CliTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _seed(self, question_id: str) -> None:
        db_path = self.run_dir / _question_path_component(question_id) / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(str(db_path))
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO long_term_memory (
                    fact_text, subject, category, importance, confidence,
                    source_message_ids, promoted_from_candidate_id
                ) VALUES ('Led alpha.', 'user', 'fact', 5, 0.9, '[1]', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO retrieval_events (
                    fact_id, session_id, query,
                    keyword_fact_ids, vector_fact_ids, fused_fact_ids
                ) VALUES (1, ?, 'q', '[1]', '[1]', '[1]')
                """,
                (f"longmemeval:{question_id}:answer",),
            )
            conn.commit()

    def _fixture(self) -> Path:
        self._seed("q1")
        path = self.root / "oracle.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q1",
                        "run_dir": str(self.run_dir),
                        "question": "How many?",
                        "gold_answer": 1,
                        "constituent_fact_ids": [1],
                        "expected_fact_texts": ["Led alpha."],
                    }
                ]
            ),
            encoding="utf-8",
        )
        return path

    def test_default_skip_exits_zero_without_flags(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = oee.main([])
        self.assertEqual(code, 0)
        self.assertIn("skipped", stdout.getvalue().lower())

    def test_allow_live_without_fixture_is_config_error(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = oee.main(["--allow-live"])
        self.assertEqual(code, 2)
        self.assertIn("oracle-fixture", stderr.getvalue())

    def test_bind_only_prints_capture_table_without_provider(self) -> None:
        fixture = self._fixture()
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = oee.main(["--bind-only", "--oracle-fixture", str(fixture)])
        self.assertEqual(code, 0)
        out = stdout.getvalue()
        self.assertIn("q1", out)

    def test_bind_only_fails_on_drift(self) -> None:
        fixture = self._fixture()
        # Corrupt the fixture's expected text so the drift guard trips.
        data = json.loads(fixture.read_text())
        data[0]["expected_fact_texts"] = ["Totally different."]
        fixture.write_text(json.dumps(data))
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = oee.main(["--bind-only", "--oracle-fixture", str(fixture)])
        self.assertEqual(code, 2)

    def test_import_does_not_mutate_sys_path_len(self) -> None:
        before = list(sys.path)
        _load_module()
        # Re-import must be idempotent about sys.path (guarded insert).
        self.assertEqual(len([p for p in sys.path if p not in before]), 0)
