"""Specification for scripts/ablate_extraction_prompts.py's deterministic
surface: instruction assembly, normalization, the CNF rubric matcher, target
well-formedness, window binding, the copied paired schedule, and the pure
metrics builder.

This file exercises only deterministic code -- no DB, no network, no provider
agent. The full live ablation runner is opt-in (``--allow-live``) and is a
do-not-run-during-review live harness per ``docs/ai/REVIEW.md``, mirroring
``tests/test_ablate_light_time_context.py``'s split between gate/config tests
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

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "ablate_extraction_prompts.py"


def _load_module() -> ModuleType:
    """Load scripts/ablate_extraction_prompts.py, which is a script, not a
    package (same pattern as tests/test_ablate_light_time_context.py)."""
    spec = importlib.util.spec_from_file_location(
        "ablate_extraction_prompts", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def user_message(text: str):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    return ModelRequest(parts=[UserPromptPart(content=text)])


class CliGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_default_skip_exits_zero_without_flags(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = self.module.main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("skipped", stdout.getvalue().lower())

    def test_allow_live_without_db_is_a_config_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = self.module.main(["--allow-live"])
        self.assertEqual(exit_code, 2)
        self.assertIn("--db", stderr.getvalue())


class BuildConditionInstructionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()
        from adapters.openrouter_live_adapter import EXTRACTION_INSTRUCTIONS

        self.base = EXTRACTION_INSTRUCTIONS

    def test_conditions_tuple_is_the_four_way_factorial(self) -> None:
        self.assertEqual(self.module.CONDITIONS, ("control", "G", "U", "G+U"))

    def test_control_is_byte_identical_to_base(self) -> None:
        self.assertEqual(
            self.module.build_condition_instructions("control", self.base), self.base
        )

    def test_each_noncontrol_condition_starts_with_base_byte_identical(self) -> None:
        for condition in ("G", "U", "G+U"):
            built = self.module.build_condition_instructions(condition, self.base)
            self.assertTrue(built.startswith(self.base))
            self.assertNotEqual(built, self.base)

    def test_g_appends_only_g_addition(self) -> None:
        built = self.module.build_condition_instructions("G", self.base)
        self.assertEqual(built, self.base + self.module.G_ADDITION)

    def test_u_appends_only_u_addition(self) -> None:
        built = self.module.build_condition_instructions("U", self.base)
        self.assertEqual(built, self.base + self.module.U_ADDITION)

    def test_gu_is_base_then_g_then_u_in_canonical_order(self) -> None:
        built = self.module.build_condition_instructions("G+U", self.base)
        self.assertEqual(
            built, self.base + self.module.G_ADDITION + self.module.U_ADDITION
        )
        self.assertIn(self.module.G_ADDITION, built)
        self.assertIn(self.module.U_ADDITION, built)
        # G strictly before U.
        self.assertLess(
            built.index(self.module.G_ADDITION), built.index(self.module.U_ADDITION)
        )

    def test_excluded_promotion_sentence_is_named_and_nonempty(self) -> None:
        sentence = self.module.EXCLUDED_PROMOTION_SENTENCE
        self.assertTrue(sentence.strip())
        self.assertIn("completed past occurrence", sentence)

    def test_excluded_promotion_sentence_absent_from_every_built_condition(self) -> None:
        # The promotion-policy sentence must not leak in as an invariant bypass: no
        # condition -- not even G+U -- may contain the excluded sentence.
        excluded_norm = self.module.normalize(self.module.EXCLUDED_PROMOTION_SENTENCE)
        for condition in self.module.CONDITIONS:
            built = self.module.build_condition_instructions(condition, self.base)
            self.assertNotIn(excluded_norm, self.module.normalize(built))

    def test_guard_raises_when_base_already_contains_an_addition(self) -> None:
        doctored = self.base + self.module.U_ADDITION
        with self.assertRaises(self.module.AblationConfigError):
            self.module.build_condition_instructions("control", doctored)

    def test_runtime_guard_rejects_excluded_sentence_in_built_condition(self) -> None:
        # Not just test-pinned: build_condition_instructions itself must fail
        # loudly if the excluded promotion sentence would ship in any built
        # condition (e.g. a future base prompt absorbs it).
        doctored = self.base + "\n" + self.module.EXCLUDED_PROMOTION_SENTENCE
        with self.assertRaises(self.module.AblationConfigError):
            self.module.build_condition_instructions("G", doctored)

    def test_live_drift_guard_real_base_contains_neither_addition(self) -> None:
        # Pins the additions against real adapter text: if a future prompt edit
        # ships either paragraph, the guard would fire in production, so this
        # must fail loudly here first.
        base_norm = self.module.normalize(self.base)
        self.assertNotIn(self.module.normalize(self.module.G_ADDITION), base_norm)
        self.assertNotIn(self.module.normalize(self.module.U_ADDITION), base_norm)


class NormalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_lowercases_and_collapses_all_whitespace(self) -> None:
        self.assertEqual(
            self.module.normalize("Hello\tWORLD\n\n  Foo   Bar"),
            "hello world foo bar",
        )

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        self.assertEqual(self.module.normalize("  \n a b \t "), "a b")


class RubricHitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_single_candidate_satisfying_every_group_is_a_hit(self) -> None:
        rubric = (("rachel",), ("suburb",))
        self.assertTrue(
            self.module.rubric_hit(["Rachel moved to the Suburbs"], rubric)
        )

    def test_alternation_within_a_group_via_any_of(self) -> None:
        rubric = (("yoga",), ("three times a week", "3x"))
        self.assertTrue(self.module.rubric_hit(["yoga 3x weekly"], rubric))

    def test_split_across_two_candidates_is_a_miss(self) -> None:
        rubric = (("rachel",), ("suburb",))
        self.assertFalse(
            self.module.rubric_hit(["Rachel is a friend", "moved to a suburb"], rubric)
        )

    def test_unmatched_group_is_a_miss(self) -> None:
        rubric = (("rachel",), ("suburb",))
        self.assertFalse(self.module.rubric_hit(["Rachel moved to Miami"], rubric))

    def test_digit_edged_tokens_do_not_match_inside_larger_numbers(self) -> None:
        # "$5" must not match "$50", "3 times a week" must not match
        # "13 times a week", "400,000" must not match "$1,400,000".
        self.assertFalse(self.module.rubric_hit(["spent $50 at Target"], (("$5",),)))
        self.assertFalse(
            self.module.rubric_hit(
                ["does yoga 13 times a week"], (("3 times a week",),)
            )
        )
        self.assertFalse(
            self.module.rubric_hit(["a $1,400,000 mortgage"], (("400,000",),))
        )

    def test_digit_edged_tokens_still_match_exact_amounts(self) -> None:
        self.assertTrue(self.module.rubric_hit(["used a $5 coupon"], (("$5",),)))
        self.assertTrue(
            self.module.rubric_hit(["yoga 3 times a week now"], (("3 times a week",),))
        )
        self.assertTrue(
            self.module.rubric_hit(["pre-approved for $400,000"], (("400,000",),))
        )

    def test_alpha_stem_tokens_keep_prefix_matching(self) -> None:
        # Stem tokens are intentional (score.py semantics): "suburb" matches
        # "suburbs", "pre-approv" matches "pre-approved", "sunday" matches
        # "sundays". Numeric-boundary guards must not break them.
        self.assertTrue(self.module.rubric_hit(["moved to the suburbs"], (("suburb",),)))
        self.assertTrue(
            self.module.rubric_hit(["got pre-approved yesterday"], (("pre-approv",),))
        )
        self.assertTrue(self.module.rubric_hit(["works sundays"], (("sunday",),)))

    def test_empty_candidate_list_is_a_miss(self) -> None:
        self.assertFalse(self.module.rubric_hit([], (("rachel",),)))


class TargetsWellFormednessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_five_targets_with_unique_ids(self) -> None:
        ids = [t.target_id for t in self.module.TARGETS]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)

    def test_expected_target_ids_present(self) -> None:
        ids = {t.target_id for t in self.module.TARGETS}
        self.assertEqual(
            ids, {"830ce83f", "945e3d21", "852ce960", "51a45a95", "7161e7e2"}
        )

    def test_locators_and_rubric_groups_are_nonempty(self) -> None:
        for target in self.module.TARGETS:
            self.assertTrue(target.window_locators)
            self.assertTrue(target.rubric)
            for locator in target.window_locators:
                self.assertTrue(locator)
            for group in target.rubric:
                self.assertTrue(group)
                for token in group:
                    self.assertTrue(token)

    def test_every_locator_and_token_is_pre_normalized(self) -> None:
        for target in self.module.TARGETS:
            for locator in target.window_locators:
                self.assertEqual(locator, self.module.normalize(locator))
            for group in target.rubric:
                for token in group:
                    self.assertEqual(token, self.module.normalize(token))


class BindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def _window(self, db: str, key: str, text: str, messages: list[str] | None = None):
        message_texts = messages if messages is not None else [text]
        return self.module.Window(
            db=db,
            key=key,
            rows=[],
            transcript=text,
            normalized=self.module.normalize(text),
            normalized_messages=[self.module.normalize(m) for m in message_texts],
        )

    def _target(self, locators, rubric=(("x",),)):
        return self.module.Target(
            target_id="t1", window_locators=tuple(locators), rubric=rubric
        )

    def test_unique_match_binds_one_window(self) -> None:
        windows = [
            self._window("db", "db#w0", "nothing here"),
            self._window("db", "db#w1", "the rachel suburb line"),
        ]
        target = self._target(["rachel suburb"])
        result = self.module._bind_target(target, windows)
        self.assertEqual(result.windows, ["db#w1"])
        self.assertFalse(result.multi_match)
        self.assertEqual(result.dbs, ["db"])

    def test_zero_match_raises_naming_target_and_a_missing_locator(self) -> None:
        windows = [self._window("db", "db#w0", "unrelated content")]
        target = self._target(["rachel suburb"])
        with self.assertRaises(self.module.AblationConfigError) as caught:
            self.module._bind_target(target, windows)
        message = str(caught.exception)
        self.assertIn("t1", message)
        self.assertIn("rachel suburb", message)

    def test_multi_match_binds_all_windows_and_flags_multi(self) -> None:
        windows = [
            self._window("dbA", "dbA#w0", "rachel suburb one"),
            self._window("dbB", "dbB#w3", "rachel suburb two"),
        ]
        target = self._target(["rachel suburb"])
        result = self.module._bind_target(target, windows)
        self.assertEqual(sorted(result.windows), ["dbA#w0", "dbB#w3"])
        self.assertTrue(result.multi_match)
        self.assertEqual(sorted(result.dbs), ["dbA", "dbB"])

    def test_all_locators_must_match_for_a_window_to_bind(self) -> None:
        windows = [
            self._window("db", "db#w0", "rachel is here"),
            self._window("db", "db#w1", "rachel and the suburb"),
        ]
        target = self._target(["rachel", "suburb"])
        result = self.module._bind_target(target, windows)
        self.assertEqual(result.windows, ["db#w1"])

    def test_locators_split_across_messages_do_not_bind(self) -> None:
        # Locators are drawn from ONE answer-bearing user turn; a window where
        # they only co-occur across different messages (e.g. an assistant echo
        # plus an unrelated turn) must not bind.
        windows = [
            self._window(
                "db",
                "db#w0",
                "rachel ... suburb",
                messages=["rachel is a friend", "some suburb talk"],
            ),
            self._window(
                "db",
                "db#w1",
                "rachel moved to the suburb",
                messages=["hello", "rachel moved to the suburb"],
            ),
        ]
        target = self._target(["rachel", "suburb"])
        result = self.module._bind_target(target, windows)
        self.assertEqual(result.windows, ["db#w1"])


class GlobalPairedScheduleTests(unittest.TestCase):
    """Repeat-major plan over the full bound-window panel: a tight budget must
    truncate at panel boundaries (every earlier repeat covers every window and
    condition) instead of spending one window's full plan before the next
    window sees a single call."""

    CONDS = ("control", "G", "U", "G+U")

    def setUp(self) -> None:
        self.module = _load_module()

    def test_full_budget_covers_every_cell_repeat_major(self) -> None:
        plan = self.module._global_paired_schedule(2, ["wA", "wB"], self.CONDS, 100)
        self.assertEqual(
            plan,
            [
                (0, "wA", "control"),
                (0, "wA", "G"),
                (0, "wA", "U"),
                (0, "wA", "G+U"),
                (0, "wB", "control"),
                (0, "wB", "G"),
                (0, "wB", "U"),
                (0, "wB", "G+U"),
                (1, "wA", "control"),
                (1, "wA", "G"),
                (1, "wA", "U"),
                (1, "wA", "G+U"),
                (1, "wB", "control"),
                (1, "wB", "G"),
                (1, "wB", "U"),
                (1, "wB", "G+U"),
            ],
        )

    def test_budget_of_one_panel_gives_every_window_its_repeat_zero(self) -> None:
        # Budget = windows x conditions: repeat 0 runs the complete panel and
        # nothing else -- no window is starved by an earlier window's later
        # repeats.
        plan = self.module._global_paired_schedule(5, ["wA", "wB"], self.CONDS, 8)
        self.assertEqual({r for r, _, _ in plan}, {0})
        self.assertEqual({w for _, w, _ in plan}, {"wA", "wB"})
        self.assertEqual(len(plan), 8)

    def test_tight_budget_truncates_mid_panel_in_order(self) -> None:
        plan = self.module._global_paired_schedule(3, ["wA", "wB"], self.CONDS, 6)
        self.assertEqual(
            plan,
            [
                (0, "wA", "control"),
                (0, "wA", "G"),
                (0, "wA", "U"),
                (0, "wA", "G+U"),
                (0, "wB", "control"),
                (0, "wB", "G"),
            ],
        )

    def test_zero_budget_plans_nothing(self) -> None:
        self.assertEqual(
            self.module._global_paired_schedule(3, ["wA"], self.CONDS, 0), []
        )


class WindowSlicingTests(unittest.TestCase):
    """Windows are delimited by the persisted dream_runs Light watermarks, not
    by uniform LIGHT_PHASE_BATCH_SIZE chunks: production ran Light per
    ingested session batch, so real window boundaries are irregular and a tail
    never consumed by Light is not a window at all."""

    def setUp(self) -> None:
        self.module = _load_module()

    @staticmethod
    def _rows(ids):
        return [(i, None, f"msg{i}") for i in ids]

    def test_slices_rows_at_watermark_boundaries(self) -> None:
        rows = self._rows([1, 2, 3, 4, 5, 6, 7])
        slices = self.module._slice_rows_by_boundaries(rows, [3, 5, 7])
        self.assertEqual(
            [[row[0] for row in s] for s in slices], [[1, 2, 3], [4, 5], [6, 7]]
        )

    def test_unprocessed_tail_beyond_last_boundary_is_not_a_window(self) -> None:
        rows = self._rows([1, 2, 3, 4, 5, 6])
        slices = self.module._slice_rows_by_boundaries(rows, [4])
        self.assertEqual([[row[0] for row in s] for s in slices], [[1, 2, 3, 4]])

    def test_boundary_with_no_rows_is_skipped(self) -> None:
        # e.g. a cycle that only consumed onboarding-excluded rows.
        rows = self._rows([1, 2, 6, 7])
        slices = self.module._slice_rows_by_boundaries(rows, [2, 5, 7])
        self.assertEqual([[row[0] for row in s] for s in slices], [[1, 2], [6, 7]])

    def test_no_boundaries_yields_no_windows(self) -> None:
        self.assertEqual(
            self.module._slice_rows_by_boundaries(self._rows([1, 2]), []), []
        )


class ValidateDbsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_duplicate_db_paths_are_a_config_error(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            with self.assertRaises(self.module.AblationConfigError) as caught:
                self.module._validate_dbs([handle.name, handle.name])
            self.assertIn("duplicate", str(caught.exception).lower())

    def test_aliased_duplicate_paths_are_a_config_error(self) -> None:
        # The same physical DB reached via two spellings would double every
        # window, call, and metric silently.
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", dir=".") as handle:
            name = os.path.basename(handle.name)
            with self.assertRaises(self.module.AblationConfigError):
                self.module._validate_dbs([name, f"./{name}"])


class BuildMetricsDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def _args(self, repeats: int = 3):
        class _Args:
            db = ["db.sqlite"]
            model_group = "extraction"
            max_provider_calls = 100

        args = _Args()
        args.repeats = repeats
        return args

    def _binding(self, target_id, windows, rubric, multi_match=False, dbs=None):
        return self.module.BindingResult(
            target_id=target_id,
            dbs=dbs if dbs is not None else ["db.sqlite"],
            windows=list(windows),
            multi_match=multi_match,
            rubric=rubric,
        )

    def _cand(self, condition, repeat, window, fact_text):
        return {
            "condition": condition,
            "repeat": repeat,
            "window": window,
            "db": "db.sqlite",
            "fact_text": fact_text,
            "category": "fact",
            "occurred_at_raw": None,
            "occurred_at_guarded": None,
            "source_message_ids": [1],
            "target_ids": [],
        }

    def _call(self, condition, repeat, window, kept, raw, dropped, itok, otok):
        return {
            "record_type": "call",
            "condition": condition,
            "repeat": repeat,
            "window": window,
            "db": "db.sqlite",
            "kept": kept,
            "raw": raw,
            "dropped": dropped,
            "input_tokens": itok,
            "output_tokens": otok,
        }

    def test_hit_rate_none_when_zero_attempts(self) -> None:
        bindings = {
            "t1": self._binding("t1", ["w0"], (("rachel",), ("suburb",)))
        }
        # Target's window w0 was never attempted under "G".
        doc = self.module._build_metrics_document(
            args=self._args(),
            bindings=bindings,
            candidate_records=[],
            call_records=[],
            attempts={c: {} for c in self.module.CONDITIONS},
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
        )
        per_target = doc["conditions"]["G"]["per_target"]["t1"]
        self.assertIsNone(per_target["hit_rate"])
        self.assertEqual(per_target["repeats_attempted"], 0)
        self.assertEqual(per_target["per_repeat_hits"], [None, None, None])

    def test_unattempted_repeat_is_null_zero_candidate_repeat_is_false(self) -> None:
        bindings = {"t1": self._binding("t1", ["w0"], (("rachel",), ("suburb",)))}
        # repeat 0 attempted and hit; repeat 1 attempted but produced nothing
        # (a real miss -> False); repeat 2 never attempted (-> None).
        candidate_records = [self._cand("control", 0, "w0", "rachel in the suburb")]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["control"] = {"w0": [0, 1]}
        doc = self.module._build_metrics_document(
            args=self._args(),
            bindings=bindings,
            candidate_records=candidate_records,
            call_records=[],
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
        )
        per_target = doc["conditions"]["control"]["per_target"]["t1"]
        self.assertEqual(per_target["per_repeat_hits"], [True, False, None])
        self.assertEqual(per_target["hits"], 1)
        self.assertEqual(per_target["repeats_attempted"], 2)
        self.assertAlmostEqual(per_target["hit_rate"], 0.5)

    def test_overall_hit_rate_stdev_none_with_single_repeat(self) -> None:
        bindings = {"t1": self._binding("t1", ["w0"], (("rachel",),))}
        candidate_records = [self._cand("control", 0, "w0", "rachel here")]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["control"] = {"w0": [0]}
        doc = self.module._build_metrics_document(
            args=self._args(repeats=1),
            bindings=bindings,
            candidate_records=candidate_records,
            call_records=[],
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
        )
        overall = doc["conditions"]["control"]["overall_hit_rate"]
        self.assertEqual(overall["mean"], 1.0)
        self.assertIsNone(overall["stdev"])
        self.assertEqual(overall["repeats_with_data"], 1)

    def test_candidate_volume_stats_over_cells(self) -> None:
        bindings = {"t1": self._binding("t1", ["w0"], (("rachel",),))}
        call_records = [
            self._call("control", 0, "w0", kept=2, raw=3, dropped=1, itok=10, otok=5),
            self._call("control", 1, "w0", kept=4, raw=4, dropped=0, itok=20, otok=7),
        ]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["control"] = {"w0": [0, 1]}
        doc = self.module._build_metrics_document(
            args=self._args(repeats=2),
            bindings=bindings,
            candidate_records=[],
            call_records=call_records,
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
        )
        volume = doc["conditions"]["control"]["candidate_volume"]
        self.assertEqual(volume["kept"]["total"], 6)
        self.assertEqual(volume["kept"]["mean"], 3.0)
        self.assertEqual(volume["kept"]["min"], 2)
        self.assertEqual(volume["kept"]["max"], 4)
        self.assertEqual(volume["raw"]["total"], 7)
        self.assertEqual(volume["dropped_out_of_window_total"], 1)

    def test_token_accounting_is_per_field_when_usage_partially_missing(self) -> None:
        # A call reporting only one side of usage must not skew the other
        # side's mean: each field carries its own denominator.
        bindings = {"t1": self._binding("t1", ["w0"], (("rachel",),))}
        call_records = [
            self._call("control", 0, "w0", kept=1, raw=1, dropped=0, itok=10, otok=None),
            self._call("control", 1, "w0", kept=1, raw=1, dropped=0, itok=30, otok=6),
            self._call("control", 2, "w0", kept=0, raw=0, dropped=0, itok=None, otok=None),
        ]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["control"] = {"w0": [0, 1, 2]}
        doc = self.module._build_metrics_document(
            args=self._args(repeats=3),
            bindings=bindings,
            candidate_records=[],
            call_records=call_records,
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
        )
        tokens = doc["conditions"]["control"]["tokens"]
        self.assertEqual(tokens["calls_total"], 3)
        self.assertEqual(tokens["calls_with_input"], 2)
        self.assertEqual(tokens["calls_with_output"], 1)
        self.assertEqual(tokens["input_total"], 40)
        self.assertEqual(tokens["output_total"], 6)
        self.assertEqual(tokens["input_mean_per_call"], 20.0)
        self.assertEqual(tokens["output_mean_per_call"], 6.0)

    def test_token_totals_none_not_zero_when_no_call_reports_usage(self) -> None:
        bindings = {"t1": self._binding("t1", ["w0"], (("rachel",),))}
        call_records = [
            self._call("control", 0, "w0", kept=1, raw=1, dropped=0, itok=None, otok=None),
        ]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["control"] = {"w0": [0]}
        doc = self.module._build_metrics_document(
            args=self._args(repeats=1),
            bindings=bindings,
            candidate_records=[],
            call_records=call_records,
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
        )
        tokens = doc["conditions"]["control"]["tokens"]
        self.assertEqual(tokens["calls_total"], 1)
        self.assertEqual(tokens["calls_with_input"], 0)
        self.assertEqual(tokens["calls_with_output"], 0)
        self.assertIsNone(tokens["input_total"])
        self.assertIsNone(tokens["output_total"])
        self.assertIsNone(tokens["input_mean_per_call"])
        self.assertIsNone(tokens["output_mean_per_call"])

    def test_multi_window_bound_target_hits_if_any_bound_window_hits(self) -> None:
        bindings = {
            "t1": self._binding(
                "t1", ["wA", "wB"], (("admon",), ("sunday",)), multi_match=True
            )
        }
        # wA candidate half-satisfies; wB candidate fully satisfies.
        candidate_records = [
            self._cand("control", 0, "wA", "admon on monday"),
            self._cand("control", 0, "wB", "admon works sunday day shift"),
        ]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["control"] = {"wA": [0], "wB": [0]}
        doc = self.module._build_metrics_document(
            args=self._args(repeats=1),
            bindings=bindings,
            candidate_records=candidate_records,
            call_records=[],
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
        )
        per_target = doc["conditions"]["control"]["per_target"]["t1"]
        self.assertEqual(per_target["per_repeat_hits"], [True])
        self.assertTrue(doc["bindings"]["t1"]["multi_match"])

    def test_calls_failed_reported_per_condition(self) -> None:
        # A failure-heavy condition must not silently look cheaper: failed
        # calls appear as calls_failed alongside the successful-call stats.
        bindings = {"t1": self._binding("t1", ["w0"], (("rachel",),))}
        call_records = [
            self._call("G", 0, "w0", kept=1, raw=1, dropped=0, itok=10, otok=4),
        ]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["G"] = {"w0": [0]}
        doc = self.module._build_metrics_document(
            args=self._args(repeats=1),
            bindings=bindings,
            candidate_records=[],
            call_records=call_records,
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=False,
            provider_errors=2,
            error_counts={"G": 2},
        )
        self.assertEqual(doc["conditions"]["G"]["tokens"]["calls_failed"], 2)
        self.assertEqual(doc["conditions"]["control"]["tokens"]["calls_failed"], 0)
        self.assertEqual(doc["provider_errors"], 2)

    def test_multi_window_repeat_null_when_any_bound_window_unattempted(self) -> None:
        # A repeat counts as attempted for a target only when EVERY bound
        # window ran it. Budget truncation that reached wA but not wB leaves
        # the panel incomplete: null, never a false miss.
        bindings = {
            "t1": self._binding(
                "t1", ["wA", "wB"], (("admon",), ("sunday",)), multi_match=True
            )
        }
        candidate_records = [self._cand("control", 0, "wA", "admon on monday")]
        attempts = {c: {} for c in self.module.CONDITIONS}
        attempts["control"] = {"wA": [0]}
        doc = self.module._build_metrics_document(
            args=self._args(repeats=1),
            bindings=bindings,
            candidate_records=candidate_records,
            call_records=[],
            attempts=attempts,
            budget=self.module.ProviderBudget(100),
            budget_exhausted=True,
        )
        per_target = doc["conditions"]["control"]["per_target"]["t1"]
        self.assertEqual(per_target["per_repeat_hits"], [None])
        self.assertEqual(per_target["repeats_attempted"], 0)
        self.assertIsNone(per_target["hit_rate"])


if __name__ == "__main__":
    unittest.main()
