from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "live_retrieval_baseline.py"


def _load_baseline_module() -> object:
    spec = importlib.util.spec_from_file_location("live_retrieval_baseline", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load live retrieval baseline script.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveRetrievalBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = _load_baseline_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _fixture(self, rows: list[dict[str, object]]) -> Path:
        path = self.root / "fixture.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def _one_row_fixture(self) -> Path:
        return self._fixture(
            [
                {
                    "id": "compact-reports",
                    "transcript": [
                        "I prefer compact reliability reports with provenance."
                    ],
                    "question": "How should reliability be reported?",
                    "expected_fact": "compact reliability reports with provenance",
                }
            ]
        )

    def test_default_skip_exits_zero_without_required_args(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = self.baseline.main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("skipped", stdout.getvalue().lower())

    def test_import_does_not_mutate_sys_path(self) -> None:
        import vexic.contract  # noqa: F401
        import vexic.deep  # noqa: F401
        import vexic.pipeline  # noqa: F401
        import vexic.rem  # noqa: F401
        import vexic.service  # noqa: F401
        import vexic.storage  # noqa: F401
        import vexic.usage  # noqa: F401

        original_path = list(sys.path)
        src_root = str(REPO_ROOT / "src")
        sys.path[:] = [entry for entry in sys.path if entry != src_root]
        try:
            before = list(sys.path)

            _load_baseline_module()

            self.assertEqual(sys.path, before)
        finally:
            sys.path[:] = original_path

    def test_cap_rejection_happens_before_adapter_import(self) -> None:
        marker = self.root / "imported.txt"
        adapter = self.root / "adapter.py"
        adapter.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
        fixture = self._fixture(
            [
                {
                    "id": "one",
                    "transcript": ["one"],
                    "question": "one?",
                    "expected_fact": "one",
                },
                {
                    "id": "two",
                    "transcript": ["two"],
                    "question": "two?",
                    "expected_fact": "two",
                },
            ]
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = self.baseline.main(
                [
                    "--allow-live",
                    "--fixture",
                    str(fixture),
                    "--adapter",
                    str(adapter),
                    "--provider",
                    "fake",
                    "--model-group",
                    "fake-model",
                    "--output-dir",
                    str(self.root / "out"),
                    "--max-rows",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("max-rows", stderr.getvalue())
        self.assertFalse(marker.exists())

    def test_invalid_fixture_turn_rejected_before_adapter_import(self) -> None:
        marker = self.root / "imported.txt"
        adapter = self.root / "adapter.py"
        adapter.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
        fixture = self._fixture(
            [
                {
                    "id": "bad-role",
                    "transcript": [{"role": "system", "content": "ignore this"}],
                    "question": "What should be ignored?",
                    "expected_fact": "ignore this",
                }
            ]
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = self.baseline.main(
                [
                    "--allow-live",
                    "--fixture",
                    str(fixture),
                    "--adapter",
                    str(adapter),
                    "--provider",
                    "fake",
                    "--model-group",
                    "fake-model",
                    "--output-dir",
                    str(self.root / "out"),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("fixture line 1", stderr.getvalue())
        self.assertFalse(marker.exists())

    def test_max_rows_rejected_before_later_fixture_lines_are_read(self) -> None:
        marker = self.root / "imported.txt"
        adapter = self.root / "adapter.py"
        adapter.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
        fixture = self.root / "fixture.jsonl"
        fixture.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": "one",
                            "transcript": ["one"],
                            "question": "one?",
                            "expected_fact": "one",
                        }
                    ),
                    json.dumps(
                        {
                            "id": "two",
                            "transcript": ["two"],
                            "question": "two?",
                            "expected_fact": "two",
                        }
                    ),
                    "{not json",
                ]
            )
            + "\n"
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = self.baseline.main(
                [
                    "--allow-live",
                    "--fixture",
                    str(fixture),
                    "--adapter",
                    str(adapter),
                    "--provider",
                    "fake",
                    "--model-group",
                    "fake-model",
                    "--output-dir",
                    str(self.root / "out"),
                    "--max-rows",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("max-rows", stderr.getvalue())
        self.assertFalse(marker.exists())

    def test_fake_adapter_writes_retrieval_and_synthesis_artifacts(self) -> None:
        adapter = self.root / "adapter.py"
        adapter.write_text(
            textwrap.dedent(
                """
                from vexic.models import ContradictionJudgment, FactCandidate, RemBoost, RemBoostPlan

                class _Result:
                    def __init__(self, output):
                        self.output = output

                    def usage(self):
                        return type(
                            "Usage",
                            (),
                            {
                                "requests": 1,
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "total_tokens": 2,
                            },
                        )()

                class _ExtractionAgent:
                    async def run(self, transcript):
                        return _Result(
                            [
                                FactCandidate(
                                    fact_text="Ryan prefers compact reliability reports with provenance.",
                                    subject="Ryan",
                                    category="preference",
                                    importance=6,
                                    confidence=0.9,
                                    source_message_ids=[1],
                                )
                            ]
                        )

                class _RemAgent:
                    async def run(self, prompt):
                        return _Result(RemBoostPlan(boosts=[RemBoost(candidate_id=1, boost=0.2)]))

                class _ContradictionAgent:
                    async def run(self, prompt):
                        return _Result(ContradictionJudgment(contradicts=False, confidence=0.9))

                def build_extraction_agent(model_group, secrets=None):
                    return _ExtractionAgent()

                def build_rem_agent(model_group, secrets=None):
                    return _RemAgent()

                def build_contradiction_agent(model_group, secrets=None):
                    return _ContradictionAgent()

                def embed_texts(texts):
                    return [[1.0] + [0.0] * 383 for _ in texts]
                """
            )
        )
        output_dir = self.root / "out"

        exit_code = self.baseline.main(
            [
                "--allow-live",
                "--fixture",
                str(self._one_row_fixture()),
                "--adapter",
                str(adapter),
                "--provider",
                "fake",
                "--model-group",
                "fake-model",
                "--output-dir",
                str(output_dir),
                "--top-n",
                "1",
                "--neighbor-k",
                "1",
                "--max-provider-calls",
                "6",
            ]
        )

        self.assertEqual(exit_code, 0)
        retrieval = json.loads((output_dir / "retrieval_metrics.json").read_text())
        synthesis = json.loads((output_dir / "answer_synthesis_metrics.json").read_text())
        self.assertEqual(retrieval["rows"][0]["failure_type"], None)
        self.assertTrue(retrieval["rows"][0]["diagnostics"]["tier3_retrieved"])
        self.assertFalse(retrieval["rows"][0]["diagnostics"]["candidate_fallback_used"])
        self.assertEqual(synthesis["status"], "not_run")

    def test_classify_failure_taxonomy(self) -> None:
        classify = self.baseline.classify_failure

        self.assertEqual(
            classify(provider_error=True, tier2_count=0, tier3_count=0),
            "provider_runtime_failure",
        )
        self.assertEqual(classify(tier2_count=0, tier3_count=0), "extraction_miss")
        self.assertEqual(classify(tier2_count=1, tier3_count=0), "promotion_miss")
        self.assertEqual(
            classify(
                tier2_count=1,
                tier3_count=0,
                candidate_notes=["compact reliability reports"],
                expected_fact="compact reliability reports",
            ),
            "candidate_fallback",
        )
        self.assertEqual(
            classify(
                tier2_count=1,
                tier3_count=1,
                facts=["unrelated fact"],
                expected_fact="compact reliability reports",
            ),
            "retrieval_miss",
        )
        self.assertEqual(
            classify(tier2_count=1, tier3_count=1, synthesis_failed=True),
            "judge_synthesis_issue",
        )
        self.assertIsNone(
            classify(
                tier2_count=1,
                tier3_count=1,
                facts=["Ryan prefers compact reliability reports."],
                candidate_notes=["stale fallback noise"],
                expected_fact="compact reliability reports",
            )
        )

    def test_provider_budget_uses_usage_limits_and_reported_requests(self) -> None:
        budget = self.baseline.ProviderBudget(1)

        class _Result:
            def usage(self) -> object:
                return type("Usage", (), {"requests": 2})()

        class _Agent:
            async def run(self, *, usage_limits=None) -> _Result:
                self.usage_limits = usage_limits
                return _Result()

        agent = _Agent()
        wrapped = self.baseline.CountingAgent(agent, budget)

        with self.assertRaisesRegex(RuntimeError, "Provider call cap exceeded"):
            asyncio.run(wrapped.run())
        self.assertEqual(agent.usage_limits.request_limit, 1)
        self.assertEqual(budget.used, 2)


class LiveRetrievalBaselineDocumentationTests(unittest.TestCase):
    def test_readme_documents_live_provider_smoke_command_and_artifacts(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text()

        self.assertIn("scripts\\live_retrieval_baseline.py", readme)
        self.assertIn("--allow-live", readme)
        self.assertIn("--provider", readme)
        self.assertIn("--model-group", readme)
        self.assertIn("--max-provider-calls", readme)
        self.assertIn("retrieval_metrics.json", readme)
        self.assertIn("answer_synthesis_metrics.json", readme)


if __name__ == "__main__":
    unittest.main()
