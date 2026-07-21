"""Tests for the Tier-3 gap probe.

Deterministic and provider-free: the probe only reads run artifacts. Each test
builds its own synthetic run DB, reusing the seeding helper from
tests/test_simulate_mentioned_at_promotion.py so both harnesses are exercised
against the same synthetic run shape.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType

from tests.test_simulate_mentioned_at_promotion import _RunFixture

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "probe_class3_gaps.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_class3_gaps", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_module()


class CoverageTests(_RunFixture):
    def _probe_one(self, gaps: list[dict], **question_overrides) -> dict:
        path = self._fixture([self._question("q1", gaps, **question_overrides)])
        entry = probe.load_gap_fixture(path)[0]
        return probe.probe_question(entry)

    def test_fact_in_tier3_is_covered(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
            facts=[
                {
                    "id": 1,
                    "fact_text": "User went on a 5-day camping trip to Yellowstone.",
                    "occurred_at": "2023-04-29",
                }
            ],
        )

        result = self._probe_one(
            [
                {
                    "gap_id": "g1",
                    "kind": "tier2-undated-event",
                    "match_tokens": ["Yellowstone", "camping"],
                }
            ]
        )

        self.assertEqual(result["gaps"][0]["coverage"], "covered")
        self.assertTrue(result["oracle_complete"])

    def test_candidate_only_is_tier2_only(self) -> None:
        self._seed_db(
            "q1",
            [
                {
                    "id": 1,
                    "category": "event",
                    "fact_text": "User went camping at Yellowstone for five days.",
                    "source_message_ids": [1],
                }
            ],
            messages={1: "2023-04-29 10:00:00"},
        )

        result = self._probe_one(
            [
                {
                    "gap_id": "g1",
                    "kind": "tier2-undated-event",
                    "match_tokens": ["Yellowstone", "camping"],
                }
            ]
        )

        self.assertEqual(result["gaps"][0]["coverage"], "tier2-only")
        self.assertFalse(result["oracle_complete"])

    def test_missing_from_both_tiers_is_absent(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )

        result = self._probe_one(
            [
                {
                    "gap_id": "g1",
                    "kind": "transcript-only",
                    "match_tokens": ["Camaro"],
                }
            ]
        )

        self.assertEqual(result["gaps"][0]["coverage"], "absent")

    def test_matching_requires_every_token(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
            facts=[{"id": 1, "fact_text": "User visited Yellowstone by car."}],
        )

        result = self._probe_one(
            [
                {
                    "gap_id": "g1",
                    "kind": "tier2-undated-event",
                    "match_tokens": ["Yellowstone", "camping"],
                }
            ]
        )

        self.assertEqual(result["gaps"][0]["coverage"], "absent")

    def test_undated_tier3_fact_is_not_counted_as_covered(self) -> None:
        # The whole gap is the missing date, so a dateless fact whose text
        # matches must not read as closed.
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-01-15 10:00:00"},
            facts=[
                {
                    "id": 70,
                    "fact_text": "User visited the Ancient Civilizations exhibit at the Met.",
                    "occurred_at": None,
                }
            ],
        )

        result = self._probe_one(
            [
                {
                    "gap_id": "g1",
                    "kind": "tier3-undated",
                    "frozen_fact_id": 70,
                    "match_tokens": ["Ancient Civilizations"],
                }
            ]
        )

        self.assertEqual(result["gaps"][0]["coverage"], "tier3-undated")
        self.assertFalse(result["oracle_complete"])

    def test_dated_tier3_fact_closes_a_tier3_undated_gap(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-01-15 10:00:00"},
            facts=[
                {
                    "id": 70,
                    "fact_text": "User visited the Ancient Civilizations exhibit at the Met.",
                    "occurred_at": "2023-01-15",
                }
            ],
        )

        result = self._probe_one(
            [
                {
                    "gap_id": "g1",
                    "kind": "tier3-undated",
                    "frozen_fact_id": 70,
                    "match_tokens": ["Ancient Civilizations"],
                }
            ]
        )

        self.assertEqual(result["gaps"][0]["coverage"], "covered")

    def test_question_without_gaps_is_complete(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )

        result = self._probe_one([], bucket="crowding")

        self.assertTrue(result["oracle_complete"])
        self.assertEqual(result["gaps"], [])

    def test_diagnostics_verdict_is_reported(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        (self.run_dir / "diagnostics.jsonl").write_text(
            json.dumps(
                {
                    "question_id": "q1",
                    "status": "ok",
                    "judge_verdict": "partial",
                    "answer_promoted_to_tier3": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self._probe_one([])

        self.assertEqual(result["judge_verdict"], "partial")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["answer_promoted_to_tier3"])


class CliTests(_RunFixture):
    def _seed_one(self) -> Path:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
            facts=[{"id": 1, "fact_text": "User camped at Yellowstone."}],
        )
        return self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "match_tokens": ["Yellowstone"],
                        }
                    ],
                )
            ]
        )

    def test_main_writes_artifacts(self) -> None:
        path = self._seed_one()
        out_dir = self.root / "out"

        with redirect_stdout(StringIO()):
            exit_code = probe.main(["--gaps", str(path), "--out", str(out_dir)])

        self.assertEqual(exit_code, 0)
        doc = json.loads((out_dir / "class3_gap_probe.json").read_text())
        self.assertEqual(doc["summary"]["gaps_covered"], 1)
        self.assertIn("covered", (out_dir / "class3_gap_probe.md").read_text())

    def test_missing_db_without_override_fails_loud(self) -> None:
        path = self._fixture([self._question("q1", [])])

        self.assertEqual(probe.main(["--gaps", str(path)]), 2)

    def test_run_dir_override_skips_and_reports_absent_questions(self) -> None:
        self._seed_one()
        path = self._fixture(
            [
                self._question("q1", []),
                self._question("q2", []),
            ]
        )
        out_dir = self.root / "out"

        with redirect_stdout(StringIO()):
            exit_code = probe.main(
                [
                    "--gaps",
                    str(path),
                    "--run-dir",
                    str(self.run_dir),
                    "--out",
                    str(out_dir),
                ]
            )

        self.assertEqual(exit_code, 0)
        doc = json.loads((out_dir / "class3_gap_probe.json").read_text())
        self.assertEqual(doc["skipped_question_ids"], ["q2"])
        self.assertEqual(doc["summary"]["questions"], 1)
