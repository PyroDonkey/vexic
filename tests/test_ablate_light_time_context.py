"""Specification for scripts/ablate_light_time_context.py's deterministic
scoring functions.

This file exercises only the pure metric functions, ``build_baseline_instructions``,
and ``render_transcript_unlabeled`` -- no DB, no network, no provider agent.
The full live ablation runner is opt-in (``--allow-live``) and is a
do-not-run-during-review live harness per ``docs/ai/REVIEW.md``, mirroring
``tests/test_live_retrieval_baseline.py``'s split between gate/config tests
and the never-exercised live path.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.models import FactCandidate

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "ablate_light_time_context.py"


def _load_module() -> ModuleType:
    """Load scripts/ablate_light_time_context.py, which is a script, not a
    package (same pattern as tests/test_check_doc_drift.py)."""
    spec = importlib.util.spec_from_file_location(
        "ablate_light_time_context", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def user_message(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


class AblateLightTimeContextModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_default_skip_exits_zero_without_required_args(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = self.module.main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("skipped", stdout.getvalue().lower())

    def test_allow_live_without_db_is_a_config_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = self.module.main(["--allow-live", "--out", "/tmp/whatever"])
        self.assertEqual(exit_code, 2)
        self.assertIn("--db", stderr.getvalue())


class RenderTranscriptUnlabeledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_unlabeled_render_drops_observed_markers(self) -> None:
        rows = [(7, "2023-11-17T09:30:00+00:00", user_message("hello"))]
        self.assertEqual(
            self.module.render_transcript_unlabeled(rows),
            "[message_id=7] User: hello",
        )

    def test_unlabeled_render_matches_render_transcript_with_none_timestamps(self) -> None:
        from vexic.pipeline import render_transcript

        rows = [
            (1, "2023-11-17T09:30:00+00:00", user_message("a")),
            (2, None, user_message("b")),
        ]
        blanked = [(message_id, None, msg) for message_id, _, msg in rows]
        self.assertEqual(
            self.module.render_transcript_unlabeled(rows),
            render_transcript(blanked),
        )

    def test_labeled_render_differs_from_unlabeled(self) -> None:
        from vexic.pipeline import render_transcript

        rows = [(7, "2023-11-17T09:30:00+00:00", user_message("hello"))]
        self.assertNotEqual(
            render_transcript(rows),
            self.module.render_transcript_unlabeled(rows),
        )
        self.assertNotIn("observed=", self.module.render_transcript_unlabeled(rows))


class BuildBaselineInstructionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_default_swaps_current_paragraph_for_old_paragraph(self) -> None:
        result = self.module.build_baseline_instructions()
        self.assertIn(self.module.OLD_TEMPORAL_PARAGRAPH, result)
        self.assertNotIn(self.module.NEW_TEMPORAL_PARAGRAPH, result)

    def test_current_extraction_instructions_contain_new_paragraph_verbatim(self) -> None:
        # Pins the constant against real adapter text: a future prompt edit
        # that drops this substring must fail loudly here, not silently
        # produce a no-op baseline variant.
        from adapters.openrouter_live_adapter import EXTRACTION_INSTRUCTIONS

        self.assertIn(self.module.NEW_TEMPORAL_PARAGRAPH, EXTRACTION_INSTRUCTIONS)

    def test_swap_preserves_everything_else_byte_identical(self) -> None:
        from adapters.openrouter_live_adapter import EXTRACTION_INSTRUCTIONS

        result = self.module.build_baseline_instructions()
        prefix, _, suffix = EXTRACTION_INSTRUCTIONS.partition(
            self.module.NEW_TEMPORAL_PARAGRAPH
        )
        self.assertTrue(result.startswith(prefix))
        self.assertTrue(result.endswith(suffix))

    def test_fails_loudly_when_new_paragraph_not_found(self) -> None:
        with self.assertRaises(self.module.AblationConfigError):
            self.module.build_baseline_instructions("this text has no temporal paragraph at all")


class FabricatedYearRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_rate_counts_years_outside_window_plausibility(self) -> None:
        records = [
            {"window": "w1", "occurred_at_raw": "2023-09-24"},
            {"window": "w1", "occurred_at_raw": "1999-01-01"},
            {"window": "w1", "occurred_at_raw": None},
            {"window": "w2", "occurred_at_raw": "2030-01-01"},
        ]
        plausible = {"w1": {2022, 2023, 2024}, "w2": {2029, 2030, 2031}}
        rate = self.module.fabricated_year_rate(records, plausible)
        self.assertAlmostEqual(rate, 1 / 3)

    def test_zero_dated_candidates_returns_none_not_zero(self) -> None:
        # No dated candidates means no fabrication to measure -- None, not a
        # spurious 0.0 that reads as evidence of "no fabrication observed".
        records = [{"window": "w1", "occurred_at_raw": None}]
        self.assertIsNone(self.module.fabricated_year_rate(records, {"w1": {2023}}))

    def test_empty_records_returns_none(self) -> None:
        self.assertIsNone(self.module.fabricated_year_rate([], {}))

    def test_all_plausible_is_zero_rate(self) -> None:
        records = [{"window": "w1", "occurred_at_raw": "2023-01-01"}]
        self.assertEqual(self.module.fabricated_year_rate(records, {"w1": {2023}}), 0.0)

    def test_field_param_computes_over_guarded_values(self) -> None:
        # occurred_at_raw is fabricated but occurred_at_guarded was nulled by
        # apply_occurred_at_guards -- field="occurred_at_guarded" must report
        # the post-guard (zero) rate, not the raw one: the
        # acceptance-critical "guarded fabrication rate = 0" number.
        records = [
            {"window": "w1", "occurred_at_raw": "2025-03-01", "occurred_at_guarded": None},
        ]
        plausible = {"w1": {2022, 2023, 2024}}
        raw_rate = self.module.fabricated_year_rate(
            records, plausible, field="occurred_at_raw"
        )
        guarded_rate = self.module.fabricated_year_rate(
            records, plausible, field="occurred_at_guarded"
        )
        self.assertEqual(raw_rate, 1.0)
        # The guard nulled the only date, so there are zero dated guarded
        # candidates: the guarded rate is None (no denominator), not 0.0.
        self.assertIsNone(guarded_rate)

    def test_field_defaults_to_occurred_at_raw(self) -> None:
        records = [{"window": "w1", "occurred_at_raw": "1999-01-01", "occurred_at_guarded": "2023-01-01"}]
        plausible = {"w1": {2023}}
        self.assertEqual(self.module.fabricated_year_rate(records, plausible), 1.0)


class BuildMetricsDocumentTests(unittest.TestCase):
    """The per-variant metrics dict assembled by _build_metrics_document must
    surface both the raw and post-guard fabricated-year rate -- the guarded
    number is the acceptance-critical one and must not be silently absent
    from ablation_metrics.json."""

    def setUp(self) -> None:
        self.module = _load_module()

    def _args(self) -> object:
        class _Args:
            db = ["db.sqlite"]
            model_group = "extraction"
            repeats = 1
            max_windows = 1
            max_provider_calls = 10

        return _Args()

    def test_metrics_include_both_raw_and_guarded_fabrication_rate_keys(self) -> None:
        audit_records = [
            {
                "record_type": "window_transcript_hash",
                "window": "w1",
                "variant": "treated",
                "plausible_years": [2022, 2023, 2024],
            },
            {
                "record_type": "window_transcript_hash",
                "window": "w1",
                "variant": "baseline",
                "plausible_years": [2022, 2023, 2024],
            },
        ]
        candidate_records = [
            {
                "window": "w1",
                "variant": "treated",
                "repeat": 0,
                "category": "event",
                "fact_text": "User ran the race on March 1, 2025",
                "occurred_at_raw": "2025-03-01",
                "occurred_at_guarded": None,
            },
            {
                "window": "w1",
                "variant": "baseline",
                "repeat": 0,
                "category": "event",
                "fact_text": "User ran the race on March 1, 2025",
                "occurred_at_raw": "2025-03-01",
                "occurred_at_guarded": None,
            },
        ]
        doc = self.module._build_metrics_document(
            args=self._args(),
            jobs=[object()],
            candidate_records=candidate_records,
            audit_records=audit_records,
            budget=self.module.ProviderBudget(10),
            budget_exhausted=False,
        )
        for variant in ("treated", "baseline"):
            metrics = doc["variants"][variant]["metrics"]
            self.assertIn("fabricated_year_rate_raw", metrics)
            self.assertIn("fabricated_year_rate_guarded", metrics)
            self.assertEqual(metrics["fabricated_year_rate_raw"]["mean"], 1.0)
            # The single candidate's guarded date was nulled, so no dated
            # guarded candidates exist: the guarded mean is null, not 0.0.
            self.assertIsNone(metrics["fabricated_year_rate_guarded"]["mean"])
            self.assertEqual(metrics["fabricated_year_rate_guarded"]["repeats_with_data"], 0)

    def test_aggregation_includes_attempted_repeat_with_zero_records(self) -> None:
        # A repeat that produced zero candidates must still appear in
        # per_repeat (as None), not vanish; repeats_with_data counts only the
        # repeats that actually contributed a denominator.
        class _Args:
            db = ["db.sqlite"]
            model_group = "extraction"
            repeats = 3
            max_windows = 1
            max_provider_calls = 10

        audit_records = [
            {
                "record_type": "window_transcript_hash",
                "window": "w1",
                "variant": variant,
                "plausible_years": [2022, 2023, 2024],
            }
            for variant in ("treated", "baseline")
        ]
        # Only repeat 0 produced a candidate; repeats 1 and 2 were attempted
        # but yielded nothing.
        candidate_records = [
            {
                "window": "w1",
                "variant": "treated",
                "repeat": 0,
                "category": "event",
                "fact_text": "User ran the race on 2023-09-24",
                "occurred_at_raw": "2023-09-24",
                "occurred_at_guarded": "2023-09-24",
            }
        ]
        doc = self.module._build_metrics_document(
            args=_Args(),
            jobs=[object()],
            candidate_records=candidate_records,
            audit_records=audit_records,
            budget=self.module.ProviderBudget(10),
            budget_exhausted=False,
        )
        metric = doc["variants"]["treated"]["metrics"]["fabricated_year_rate_raw"]
        self.assertEqual(len(metric["per_repeat"]), 3)
        self.assertEqual(metric["per_repeat"][0], 0.0)
        self.assertIsNone(metric["per_repeat"][1])
        self.assertIsNone(metric["per_repeat"][2])
        self.assertEqual(metric["repeats_with_data"], 1)
        self.assertEqual(metric["mean"], 0.0)
        # dated_event_rate over the single event candidate is 1.0 for repeat 0.
        dated = doc["variants"]["treated"]["metrics"]["dated_event_rate"]
        self.assertEqual(dated["per_repeat"], [1.0, None, None])
        self.assertEqual(dated["repeats_with_data"], 1)


class IntextCopyRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_rate_counts_exact_precision_matches_among_event_intext_dates(self) -> None:
        records = [
            {
                "category": "event",
                "fact_text": "User ran the Berlin half on 2023-09-24",
                "occurred_at_raw": "2023-09-24",
            },
            {
                "category": "event",
                "fact_text": "User ran the Berlin half on 2023-09-24",
                "occurred_at_raw": "2023-09-01",
            },
            {
                "category": "event",
                "fact_text": "User ran a marathon sometime",
                "occurred_at_raw": None,
            },
            {
                "category": "preference",
                "fact_text": "User prefers dark mode, set on 2023-09-24",
                "occurred_at_raw": "2023-09-24",
            },
        ]
        rate = self.module.intext_copy_rate(records)
        self.assertAlmostEqual(rate, 1 / 2)

    def test_no_intext_dates_returns_none(self) -> None:
        records = [{"category": "event", "fact_text": "no date here", "occurred_at_raw": None}]
        self.assertIsNone(self.module.intext_copy_rate(records))


class DatedEventRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_rate_over_event_candidates_only(self) -> None:
        records = [
            {"category": "event", "occurred_at_guarded": "2023-09-24"},
            {"category": "event", "occurred_at_guarded": None},
            {"category": "preference", "occurred_at_guarded": None},
        ]
        rate = self.module.dated_event_rate(records)
        self.assertAlmostEqual(rate, 1 / 2)

    def test_no_event_candidates_returns_none(self) -> None:
        records = [{"category": "preference", "occurred_at_guarded": "2023-09-24"}]
        self.assertIsNone(self.module.dated_event_rate(records))


class FullDateFromPartialRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_rate_flags_full_date_from_month_only_intext(self) -> None:
        records = [
            {
                "fact_text": "User started the job in March 2023",
                "occurred_at_raw": "2023-03-14",
            },
            {
                "fact_text": "User started the job on 2023-03-14",
                "occurred_at_raw": "2023-03-14",
            },
            {
                "fact_text": "User started the job in March 2023",
                "occurred_at_raw": "2023-03",
            },
            {
                "fact_text": "random text no date",
                "occurred_at_raw": "2023-03-14",
            },
        ]
        rate = self.module.full_date_from_partial_rate(records)
        self.assertAlmostEqual(rate, 1 / 3)

    def test_no_intext_dates_returns_none(self) -> None:
        records = [{"fact_text": "no date here", "occurred_at_raw": "2023-03-14"}]
        self.assertIsNone(self.module.full_date_from_partial_rate(records))


class MirrorProductionCandidateDropTests(unittest.TestCase):
    """3a: the ablation must mirror production's Light candidate drop
    (keep_candidates_with_valid_source_ids over the window's rendered evidence
    ids) so out-of-window-cited candidates do not skew reported metrics."""

    def setUp(self) -> None:
        self.module = _load_module()

    def _candidate(self, fact_text: str, source_message_ids: list[int]):
        from vexic.models import FactCandidate

        return FactCandidate(
            fact_text=fact_text,
            subject="Ryan",
            category="fact",
            importance=5,
            confidence=0.8,
            source_message_ids=source_message_ids,
        )

    def test_drops_candidate_citing_out_of_window_id(self) -> None:
        rows = [(1, None, user_message("hi")), (2, None, user_message("bye"))]
        candidates = [
            self._candidate("kept", [1]),
            self._candidate("dropped", [99]),
        ]
        kept, dropped = self.module._drop_out_of_window_candidates(candidates, rows)
        self.assertEqual([c.fact_text for c in kept], ["kept"])
        self.assertEqual(dropped, 1)

    def test_uses_rendered_evidence_ids_not_raw_ids(self) -> None:
        # A candidate citing only a message that renders to no transcript line
        # (no evidence id) is dropped, matching the pipeline's evidence-id gate.
        rows = [(5, None, user_message("visible"))]
        candidates = [self._candidate("miscited", [1])]
        kept, dropped = self.module._drop_out_of_window_candidates(candidates, rows)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)


class GlobalPairedScheduleTests(unittest.TestCase):
    """3b: pairing spans the whole window panel and a repeat is atomic, so a
    tight budget truncates on a repeat boundary rather than starving one
    variant or leaving one window scored by only one variant."""

    def setUp(self) -> None:
        self.module = _load_module()

    def test_full_budget_covers_every_window_variant_repeat_major(self) -> None:
        plan = self.module._global_paired_schedule(
            2, ("w0", "w1"), ("baseline", "treated"), 100
        )
        self.assertEqual(
            plan,
            [
                (0, "w0", "baseline"),
                (0, "w0", "treated"),
                (0, "w1", "baseline"),
                (0, "w1", "treated"),
                (1, "w0", "baseline"),
                (1, "w0", "treated"),
                (1, "w1", "baseline"),
                (1, "w1", "treated"),
            ],
        )

    def test_budget_truncates_on_a_repeat_boundary(self) -> None:
        # Panel is 2 windows x 2 variants = 4. Budget 7 affords one full repeat
        # and three quarters of a second; the partial repeat is not scheduled,
        # so every scheduled repeat covers the identical panel.
        plan = self.module._global_paired_schedule(
            3, ("w0", "w1"), ("baseline", "treated"), 7
        )
        self.assertEqual({repeat for repeat, _, _ in plan}, {0})
        self.assertEqual(len(plan), 4)

    def test_budget_below_one_panel_plans_nothing(self) -> None:
        # The prior per-window shape ran a truncated plan here, which is what
        # produced a baseline-only window.
        plan = self.module._global_paired_schedule(
            3, ("w0", "w1"), ("baseline", "treated"), 3
        )
        self.assertEqual(plan, [])

    def test_every_scheduled_repeat_pairs_every_window(self) -> None:
        plan = self.module._global_paired_schedule(
            5, ("w0", "w1", "w2"), ("baseline", "treated"), 13
        )
        by_cell: dict[tuple[int, str], set[str]] = {}
        for repeat, window, variant in plan:
            by_cell.setdefault((repeat, window), set()).add(variant)
        self.assertTrue(by_cell)
        for cell, variants in by_cell.items():
            self.assertEqual(variants, {"baseline", "treated"}, f"cell {cell} unpaired")

    def test_zero_budget_plans_nothing(self) -> None:
        self.assertEqual(
            self.module._global_paired_schedule(
                3, ("w0",), ("baseline", "treated"), 0
            ),
            [],
        )

    def test_metrics_attempted_reflects_the_surviving_repeat_indices(self) -> None:
        # treated survived only repeat 0; baseline survived repeats 0 and 2 --
        # a gapped set, which is what a voided middle repeat leaves behind.
        # repeats_attempted and the per_repeat slot count must follow those
        # indices, not args.repeats and not a bare count.
        class _Args:
            db = ["db.sqlite"]
            model_group = "extraction"
            repeats = 3
            max_windows = 1
            max_provider_calls = 3

        audit_records = [
            {
                "record_type": "window_transcript_hash",
                "window": "w1",
                "variant": variant,
                "plausible_years": [2022, 2023, 2024],
            }
            for variant in ("treated", "baseline")
        ]
        doc = self.module._build_metrics_document(
            args=_Args(),
            jobs=[object()],
            candidate_records=[],
            audit_records=audit_records,
            budget=self.module.ProviderBudget(3),
            budget_exhausted=True,
            attempted_repeats={"baseline": [0, 2], "treated": [0]},
        )
        self.assertEqual(doc["variants"]["baseline"]["repeats_attempted"], 2)
        self.assertEqual(doc["variants"]["treated"]["repeats_attempted"], 1)
        self.assertEqual(
            len(doc["variants"]["treated"]["metrics"]["dated_event_rate"]["per_repeat"]),
            1,
        )
        self.assertEqual(
            len(doc["variants"]["baseline"]["metrics"]["dated_event_rate"]["per_repeat"]),
            2,
        )


class _Result:
    """Minimal pydantic-ai result shape: ``_run_agent`` does
    ``list(result.output)``. Shaped after tests/test_live_retrieval_baseline.py's
    fake adapter."""

    def __init__(self, output: list[FactCandidate]) -> None:
        self.output = output


class _Recorder:
    """Shared call log across both variants' fake agents, so a failure can be
    scheduled at an exact global call index."""

    def __init__(self, fail_on: frozenset[int]) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on


class _FakeAgent:
    def __init__(
        self, variant: str, recorder: _Recorder, fact_text: str | None = None
    ) -> None:
        self.variant = variant
        self.recorder = recorder
        self.fact_text = fact_text or "Ryan ran the Berlin marathon on 2024-09-29."

    async def run(self, transcript: str) -> _Result:
        index = len(self.recorder.calls)
        self.recorder.calls.append((self.variant, transcript))
        if index in self.recorder.fail_on:
            raise RuntimeError(f"synthetic provider failure on call {index}")
        # Cite the window's first rendered message id so
        # _drop_out_of_window_candidates keeps the candidate; a candidate citing
        # an out-of-window id would be dropped and every assertion would then
        # run over empty lists.
        # The treated marker is "[message_id=1 observed=2024-09-30 Mon]", the
        # baseline marker "[message_id=1]"; take the leading digits of either.
        marker = transcript.split("[message_id=", 1)[1]
        first_id = int(marker[: len(marker) - len(marker.lstrip("0123456789"))])
        return _Result(
            [
                FactCandidate(
                    fact_text=self.fact_text,
                    subject="Ryan",
                    category="event",
                    importance=6,
                    confidence=0.9,
                    source_message_ids=[first_id],
                )
            ]
        )


class ValidateDbsTests(unittest.TestCase):
    """The same physical database supplied twice must be rejected, not
    measured twice: ``_collect_windows`` walks every supplied path, so an
    alias silently doubles that corpus's weight in the aggregate metrics and
    re-spends provider budget on it."""

    def setUp(self) -> None:
        self.module = _load_module()

    def test_duplicate_db_paths_are_a_config_error(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            with self.assertRaises(self.module.AblationConfigError) as caught:
                self.module._validate_dbs([handle.name, handle.name])
            self.assertIn("duplicate", str(caught.exception).lower())

    def test_symlinked_duplicate_paths_are_a_config_error(self) -> None:
        # A symlink reaches the same physical DB under a different spelling.
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "a.db"
            original.touch()
            link = Path(tmp) / "b.db"
            link.symlink_to(original)
            with self.assertRaises(self.module.AblationConfigError):
                self.module._validate_dbs([str(original), str(link)])

    def test_hardlinked_duplicate_paths_are_a_config_error(self) -> None:
        # A hard link has no "real" path to resolve to: identity must key on
        # (device, inode), which is why Path.resolve() alone is not enough.
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "a.db"
            original.touch()
            link = Path(tmp) / "b.db"
            os.link(original, link)
            with self.assertRaises(self.module.AblationConfigError):
                self.module._validate_dbs([str(original), str(link)])

    def test_distinct_databases_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.db"
            second = Path(tmp) / "b.db"
            first.touch()
            second.touch()
            self.module._validate_dbs([str(first), str(second)])

    def test_missing_db_is_still_a_config_error(self) -> None:
        with self.assertRaises(self.module.AblationConfigError) as caught:
            self.module._validate_dbs(["/nonexistent/memory.db"])
        self.assertIn("not found", str(caught.exception))

    def test_a_directory_is_a_config_error(self) -> None:
        # stat() sees a directory happily; without a file check the failure
        # surfaces later as a generic SQLite error (exit 1) instead of the
        # config channel this function feeds (exit 2).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.module.AblationConfigError) as caught:
                self.module._validate_dbs([tmp])
            self.assertIn("not a file", str(caught.exception))

    def test_no_db_is_a_config_error(self) -> None:
        with self.assertRaises(self.module.AblationConfigError) as caught:
            self.module._validate_dbs([])
        self.assertIn("--db is required", str(caught.exception))


class AblationExecutionHarness(unittest.TestCase):
    """Drives the full runner (``main``) with a faked transcript source and
    faked provider agents. No DB reads, no network, no provider."""

    def setUp(self) -> None:
        self.module = _load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out_dir = self.root / "out"

    def _db_path(self, name: str = "memory.db") -> str:
        path = self.root / name
        path.touch()
        return str(path)

    def _install_windows(self, windows: dict[str, list[list[int]]]) -> None:
        """Fake ``load_messages_since`` (the module global the runner imports at
        :90), not ``_collect_windows`` -- keeping the real window-collection
        path live so the read-only DB behavior stays observable."""

        self.load_calls: list[dict[str, object]] = []

        def fake_load_messages_since(
            db: str, after_id: int, limit: int | None = None, **kwargs: object
        ) -> list[tuple[int, str, ModelRequest]]:
            self.load_calls.append(kwargs)
            for batch in windows[db]:
                if batch and batch[0] > after_id:
                    return [
                        (
                            message_id,
                            "2024-09-30T09:30:00+00:00",
                            user_message(f"message {message_id} about last Sunday"),
                        )
                        for message_id in batch
                    ]
            return []

        self.module.load_messages_since = fake_load_messages_since

    def _install_agents(
        self, fail_on: frozenset[int] = frozenset(), fact_text: str | None = None
    ) -> _Recorder:
        recorder = _Recorder(fail_on)
        self.module.build_extraction_agent = lambda *a, **k: _FakeAgent(
            "treated", recorder, fact_text
        )
        self.module._build_agent = lambda *a, **k: _FakeAgent(
            "baseline", recorder, fact_text
        )
        return recorder

    def _run(self, db_paths: list[str], **flags: object) -> int:
        argv = ["--allow-live", "--out", str(self.out_dir)]
        for db in db_paths:
            argv += ["--db", db]
        for name, value in flags.items():
            argv += [f"--{name.replace('_', '-')}", str(value)]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = self.module.main(argv)
        self.stderr = stderr.getvalue()
        return exit_code

    def _audit_records(self) -> list[dict[str, object]]:
        lines = (self.out_dir / "ablation_audit.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def _metrics(self) -> dict[str, object]:
        return json.loads((self.out_dir / "ablation_metrics.json").read_text())


class ProviderErrorToleranceTests(AblationExecutionHarness):
    """Gap 4: a transient provider failure must not discard every completed
    window and every already-spent paid provider call."""

    def test_transient_provider_error_preserves_completed_work(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        recorder = self._install_agents(fail_on=frozenset({1}))

        exit_code = self._run([db], repeats=2, max_windows=1, max_provider_calls=10)

        self.assertEqual(exit_code, 0, self.stderr)
        self.assertTrue((self.out_dir / "ablation_metrics.json").exists())
        self.assertEqual(len(recorder.calls), 4)
        errors = [
            record
            for record in self._audit_records()
            if record.get("record_type") == "call_error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("synthetic provider failure", str(errors[0]["error"]))
        candidates = [
            record
            for record in self._audit_records()
            if record.get("record_type") == "candidate"
        ]
        self.assertEqual(len(candidates), 3)

    def test_a_voided_middle_repeat_does_not_hide_a_later_surviving_repeat(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        # One window, so the panel is (baseline, treated) and call index 3 is
        # repeat 1's treated call. Treated then survives repeats {0, 2} -- a
        # gapped set. Scoring must follow the surviving indices, not a count:
        # collapsing {0, 2} to "2 attempted" would score range(2) and silently
        # drop repeat 2's data while reporting repeat 1 as an empty slot.
        self._install_agents(fail_on=frozenset({3}))

        exit_code = self._run([db], repeats=3, max_windows=1, max_provider_calls=6)

        self.assertEqual(exit_code, 0, self.stderr)
        treated = self._metrics()["variants"]["treated"]
        self.assertEqual(treated["candidate_count"], 2)
        self.assertEqual(treated["repeats_attempted"], 2)
        self.assertEqual(treated["repeats_with_candidates"], 2)

    def test_a_failed_call_voids_that_repeat_for_every_variant(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2], [3, 4]]})
        # Panel order is window-major, variants inner: index 1 is window 0's
        # treated call. Voiding only treated's repeat would leave baseline
        # scoring a repeat treated never scored, so the aggregate means compare
        # different repeat samples -- the pairing the repeat-atomic schedule
        # exists to guarantee. A failed cell voids the whole repeat.
        self._install_agents(fail_on=frozenset({1}))

        exit_code = self._run([db], repeats=1, max_windows=2, max_provider_calls=4)

        self.assertEqual(exit_code, 0, self.stderr)
        variants = self._metrics()["variants"]
        for variant in ("baseline", "treated"):
            self.assertEqual(variants[variant]["candidate_count"], 0, variant)
            self.assertEqual(variants[variant]["repeats_attempted"], 0, variant)

    def test_a_run_whose_every_call_fails_is_not_reported_as_a_clean_run(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        self._install_agents(fail_on=frozenset({0, 1, 2, 3}))

        exit_code = self._run([db], repeats=2, max_windows=1, max_provider_calls=10)

        # Zero successful calls is not evidence: the run must fail loudly rather
        # than write an artifact that reads as "measured, found nothing".
        self.assertEqual(exit_code, 1)
        self.assertIn("provider", self.stderr.lower())

    def test_voided_candidates_are_marked_in_the_audit(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        self._install_agents(fail_on=frozenset({1}))

        exit_code = self._run([db], repeats=2, max_windows=1, max_provider_calls=10)

        self.assertEqual(exit_code, 0, self.stderr)
        candidates = [
            record
            for record in self._audit_records()
            if record.get("record_type") == "candidate"
        ]
        # The audit keeps every candidate, including those dropped from scoring;
        # without an explicit marker a consumer would count candidates that
        # candidate_count excludes and read the two artifacts as contradictory.
        voided = [record for record in candidates if record.get("voided")]
        scored = [record for record in candidates if not record.get("voided")]
        self.assertEqual(len(voided), 1)
        self.assertEqual(voided[0]["repeat"], 0)
        self.assertEqual(
            len(scored), self._metrics()["variants"]["baseline"]["candidate_count"]
            + self._metrics()["variants"]["treated"]["candidate_count"],
        )

    def test_metrics_report_provider_error_counts(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        self._install_agents(fail_on=frozenset({1}))

        exit_code = self._run([db], repeats=2, max_windows=1, max_provider_calls=10)

        self.assertEqual(exit_code, 0, self.stderr)
        metrics = self._metrics()
        self.assertEqual(metrics["provider_errors"], 1)
        self.assertEqual(metrics["variants"]["treated"]["calls_failed"], 1)
        self.assertEqual(metrics["variants"]["baseline"]["calls_failed"], 0)


class ForbiddenValueGuardTests(AblationExecutionHarness):
    """Gap 1: the runner must fail closed on a configured forbidden value, both
    before the transcript reaches a third-party provider and before any artifact
    lands on disk."""

    def test_forbidden_value_in_transcript_never_reaches_the_provider(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        recorder = self._install_agents()
        self.module.REDACTION = self.module.RedactionContext(
            forbidden_values=("last Sunday",)
        )

        exit_code = self._run([db], repeats=1, max_windows=1, max_provider_calls=4)

        self.assertEqual(exit_code, 1)
        # Fail closed *before* egress: the provider was never called at all.
        self.assertEqual(recorder.calls, [])
        self.assertFalse((self.out_dir / "ablation_metrics.json").exists())

    def test_forbidden_value_in_model_output_blocks_every_artifact(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        # The secret is absent from the transcript and arrives only in the
        # provider's response, so the transcript guard cannot catch it.
        self._install_agents(fact_text="the key is sk-live-abc123")
        self.module.REDACTION = self.module.RedactionContext(
            forbidden_values=("sk-live-abc123",)
        )

        exit_code = self._run([db], repeats=1, max_windows=1, max_provider_calls=4)

        self.assertEqual(exit_code, 1)
        # _write_artifacts writes ablation_metrics.json before it opens the
        # jsonl, so a guard placed inside it would leave the metrics file on
        # disk for a run that must fail closed.
        self.assertFalse((self.out_dir / "ablation_metrics.json").exists())
        self.assertFalse((self.out_dir / "ablation_audit.jsonl").exists())

    def test_a_forbidden_value_in_a_path_argument_never_reaches_stderr(self) -> None:
        # --db not found interpolates the path straight into an error printed to
        # stderr, and --out is used to build directories but never enters the
        # guarded payload. The fail-closed rule is categorical, so path strings
        # are egress too.
        self.module.REDACTION = self.module.RedactionContext(
            forbidden_values=("sk-live-abc123",)
        )

        exit_code = self._run([str(self.root / "sk-live-abc123" / "missing.db")])

        # 1, not the 2 a usage error returns: a forbidden value is a fail-closed
        # run failure, and the guard fires before --db existence is validated.
        self.assertEqual(exit_code, 1)
        self.assertNotIn("sk-live-abc123", self.stderr)

    def test_a_forbidden_value_in_the_out_path_never_reaches_stderr(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        self._install_agents()
        self.out_dir = self.root / "sk-live-abc123-out"
        self.module.REDACTION = self.module.RedactionContext(
            forbidden_values=("sk-live-abc123",)
        )

        exit_code = self._run([db], repeats=1, max_windows=1, max_provider_calls=4)

        self.assertNotEqual(exit_code, 0)
        self.assertNotIn("sk-live-abc123", self.stderr)
        self.assertFalse(self.out_dir.exists())

    def test_clean_run_writes_artifacts_with_a_configured_forbidden_value(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        self._install_agents()
        self.module.REDACTION = self.module.RedactionContext(
            forbidden_values=("a-value-that-appears-nowhere",)
        )

        exit_code = self._run([db], repeats=1, max_windows=1, max_provider_calls=4)

        self.assertEqual(exit_code, 0, self.stderr)
        self.assertTrue((self.out_dir / "ablation_metrics.json").exists())


class ReadOnlyEvalDatabaseTests(AblationExecutionHarness):
    """Gap 2: the runner measures an eval corpus it must not mutate, so every
    window read opens the input database read-only."""

    def test_windows_are_collected_through_a_read_only_connection(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2]]})
        self._install_agents()

        exit_code = self._run([db], repeats=1, max_windows=1, max_provider_calls=4)

        self.assertEqual(exit_code, 0, self.stderr)
        self.assertTrue(self.load_calls)
        for kwargs in self.load_calls:
            self.assertIs(kwargs.get("read_only"), True)


class MultiWindowPairingTests(AblationExecutionHarness):
    """Gap 3: a budget that truncates mid-panel must never leave a window with
    one variant's candidates and not the other's. An unpaired window feeds
    cross-window aggregation with content and plausible-year sets the other
    variant never saw."""

    def _variants_by_window(self) -> dict[str, set[str]]:
        by_window: dict[str, set[str]] = {}
        for record in self._audit_records():
            if record.get("record_type") != "candidate":
                continue
            by_window.setdefault(str(record["window"]), set()).add(str(record["variant"]))
        return by_window

    def test_tight_budget_never_leaves_a_window_with_one_variant(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2], [3, 4]]})
        # Budget 3 with repeats=1 over 2 windows: window 0 costs both variants
        # (2 calls) and window 1 can only afford baseline.
        self._install_agents()

        exit_code = self._run([db], repeats=1, max_windows=2, max_provider_calls=3)

        self.assertEqual(exit_code, 0, self.stderr)
        for window, variants in self._variants_by_window().items():
            self.assertEqual(
                variants, {"baseline", "treated"}, f"window {window} is unpaired"
            )
        # The 2x2 panel does not fit in 3 calls, so nothing is scored and no
        # call is spent -- rather than spending 3 calls to produce one paired
        # window plus one baseline-only window.
        metrics = self._metrics()
        self.assertEqual(metrics["provider_calls_used"], 0)
        self.assertTrue(metrics["budget_exhausted"])
        # Every window is still rendered and audited: plausible_years must stay
        # complete for windows the budget never scores.
        hashed = {
            record["window"]
            for record in self._audit_records()
            if record.get("record_type") == "window_transcript_hash"
        }
        self.assertEqual(len(hashed), 2)

    def test_every_window_scores_both_variants_at_equal_repeat_counts(self) -> None:
        db = self._db_path()
        self._install_windows({db: [[1, 2], [3, 4]]})
        self._install_agents()

        exit_code = self._run([db], repeats=2, max_windows=2, max_provider_calls=5)

        self.assertEqual(exit_code, 0, self.stderr)
        counts: dict[tuple[str, str], int] = {}
        for record in self._audit_records():
            if record.get("record_type") != "candidate":
                continue
            key = (str(record["window"]), str(record["variant"]))
            counts[key] = counts.get(key, 0) + 1
        windows = {window for window, _ in counts}
        # Without this the loop below is vacuous: zero candidates would pass.
        self.assertEqual(len(windows), 2, counts)
        for window in windows:
            self.assertEqual(
                counts.get((window, "baseline")),
                counts.get((window, "treated")),
                f"window {window} scored the variants unequally",
            )


class DuplicateDbWiringTests(AblationExecutionHarness):
    """_validate_dbs must actually be reached from main(): the unit tests above
    call it directly, so deleting its call site would leave them all green."""

    def test_an_aliased_duplicate_db_exits_two_before_any_provider_call(self) -> None:
        db = self._db_path()
        alias = self.root / "alias.db"
        alias.symlink_to(db)
        self._install_windows({db: [[1, 2]]})
        recorder = self._install_agents()

        exit_code = self._run([db, str(alias)], repeats=1, max_windows=1)

        self.assertEqual(exit_code, 2)
        self.assertIn("duplicate --db", self.stderr)
        self.assertEqual(recorder.calls, [])
        self.assertFalse(self.out_dir.exists())


class AuditProvenanceIdentityTests(AblationExecutionHarness):
    """The window audit must record the identity actually read, not only the
    spelling the operator typed: ``load_messages_since`` resolves the path
    (``Path.resolve()``) before opening it, so a symlinked eval database is
    read under one identity and, without this, recorded under another."""

    def test_window_audit_records_the_resolved_database_identity(self) -> None:
        db = self._db_path()
        link = self.root / "alias.db"
        link.symlink_to(db)
        self._install_windows({str(link): [[1, 2]]})
        self._install_agents()

        exit_code = self._run([str(link)], repeats=1, max_windows=1, max_provider_calls=4)

        self.assertEqual(exit_code, 0, self.stderr)
        window_records = [
            record
            for record in self._audit_records()
            if record.get("record_type") == "window_transcript_hash"
        ]
        self.assertTrue(window_records)
        for record in window_records:
            self.assertEqual(record["db"], str(link))
            self.assertEqual(record["db_resolved"], str(Path(link).resolve()))


if __name__ == "__main__":
    unittest.main()
