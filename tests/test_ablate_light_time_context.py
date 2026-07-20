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
import sys
import unittest
from pathlib import Path
from types import ModuleType

from pydantic_ai.messages import ModelRequest, UserPromptPart

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

    def test_zero_dated_candidates_returns_zero_not_error(self) -> None:
        records = [{"window": "w1", "occurred_at_raw": None}]
        self.assertEqual(self.module.fabricated_year_rate(records, {"w1": {2023}}), 0.0)

    def test_all_plausible_is_zero_rate(self) -> None:
        records = [{"window": "w1", "occurred_at_raw": "2023-01-01"}]
        self.assertEqual(self.module.fabricated_year_rate(records, {"w1": {2023}}), 0.0)


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

    def test_no_intext_dates_returns_zero(self) -> None:
        records = [{"category": "event", "fact_text": "no date here", "occurred_at_raw": None}]
        self.assertEqual(self.module.intext_copy_rate(records), 0.0)


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

    def test_no_event_candidates_returns_zero(self) -> None:
        records = [{"category": "preference", "occurred_at_guarded": "2023-09-24"}]
        self.assertEqual(self.module.dated_event_rate(records), 0.0)


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

    def test_no_intext_dates_returns_zero(self) -> None:
        records = [{"fact_text": "no date here", "occurred_at_raw": "2023-03-14"}]
        self.assertEqual(self.module.full_date_from_partial_rate(records), 0.0)


if __name__ == "__main__":
    unittest.main()
