"""Tests for the hosted ingest concurrency-sweep harness.

Fully offline: `post_source_messages` is stubbed, so no path here touches the
network or the deployed service.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from vexic.storage.transcript import single_message_adapter

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "hosted_ingest_latency_sweep.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hosted_ingest_latency_sweep", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load_module()


def _write_config(root: Path) -> Path:
    path = root / "recorder.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "https://example.invalid",
                "api_key": "test-key-never-printed",
                "project_id": "project-a",
            }
        ),
        encoding="utf-8",
    )
    return path


class BatchBuildingTests(TestCase):
    def test_source_triples_are_unique_within_a_batch(self) -> None:
        # Load-bearing: a reused triple takes the dedup read path instead of
        # the insert path, so the sweep would measure reads while claiming to
        # measure ingest.
        batch = sweep._build_batch(
            session_id="probe-x", label="c1-t0", count=25, body_bytes=64
        )
        triples = {
            (item.source_host, item.source_session_id, item.source_message_id)
            for item in batch
        }
        self.assertEqual(len(triples), 25)

    def test_labels_keep_triples_unique_across_levels_and_trials(self) -> None:
        first = sweep._build_batch(
            session_id="probe-x", label="run-c1-t0", count=3, body_bytes=64
        )
        second = sweep._build_batch(
            session_id="probe-x", label="run-c2-t0", count=3, body_bytes=64
        )
        overlap = {item.source_message_id for item in first} & {
            item.source_message_id for item in second
        }
        self.assertEqual(overlap, set())

    def test_message_json_is_a_valid_transcript_message(self) -> None:
        batch = sweep._build_batch(
            session_id="probe-x", label="c1-t0", count=1, body_bytes=64
        )
        single_message_adapter.validate_json(batch[0].message_json)

    def test_bodies_are_sized_from_the_requested_byte_count(self) -> None:
        small = sweep._build_batch(
            session_id="probe-x", label="s", count=1, body_bytes=64
        )
        large = sweep._build_batch(
            session_id="probe-x", label="l", count=1, body_bytes=4000
        )
        self.assertGreater(len(large[0].message_json), len(small[0].message_json) * 10)


class PercentileTests(TestCase):
    def test_nearest_rank_percentiles_return_observed_samples(self) -> None:
        values = list(range(1, 9))
        self.assertEqual(sweep._percentile(values, 0.50), 4)
        self.assertEqual(sweep._percentile(values, 0.95), 8)

    def test_empty_sample_does_not_raise(self) -> None:
        self.assertEqual(sweep._percentile([], 0.95), 0)


class LiveGateTests(TestCase):
    def test_refuses_to_run_without_allow_live(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = sweep.main([])
        self.assertEqual(exit_code, 2)
        self.assertIn("--allow-live", stderr.getvalue())

    def test_allow_live_alone_does_not_post_when_config_is_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "absent.json"
            with self.assertRaises(Exception):
                sweep.main(["--allow-live", "--config", str(missing)])


class SweepRunTests(TestCase):
    def test_sweep_reports_one_result_per_concurrency_level(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = _write_config(root)
            output = root / "sweep.json"
            with patch.object(sweep, "post_source_messages", return_value={}):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = sweep.main(
                        [
                            "--allow-live",
                            "--config",
                            str(config_path),
                            "--concurrency",
                            "1,2",
                            "--trials",
                            "2",
                            "--messages",
                            "2",
                            "--body-bytes",
                            "32",
                            "--output",
                            str(output),
                        ]
                    )
            self.assertEqual(exit_code, 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([level["concurrency"] for level in report["levels"]], [1, 2])
            self.assertEqual([level["ok_count"] for level in report["levels"]], [2, 2])
            self.assertEqual([level["failure_count"] for level in report["levels"]], [0, 0])

    def test_failed_requests_are_recorded_as_samples_not_crashes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = _write_config(root)
            output = root / "sweep.json"
            with patch.object(
                sweep, "post_source_messages", side_effect=TimeoutError("timed out")
            ):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    exit_code = sweep.main(
                        [
                            "--allow-live",
                            "--config",
                            str(config_path),
                            "--concurrency",
                            "1",
                            "--trials",
                            "2",
                            "--messages",
                            "1",
                            "--body-bytes",
                            "32",
                            "--output",
                            str(output),
                        ]
                    )
            self.assertEqual(exit_code, 0)
            level = json.loads(output.read_text(encoding="utf-8"))["levels"][0]
            self.assertEqual(level["ok_count"], 0)
            self.assertEqual(level["failure_count"], 2)
            self.assertEqual(level["failure_types"], ["TimeoutError"])

    def test_api_key_never_reaches_the_report_or_the_console(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = _write_config(root)
            output = root / "sweep.json"
            with patch.object(sweep, "post_source_messages", return_value={}):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    sweep.main(
                        [
                            "--allow-live",
                            "--config",
                            str(config_path),
                            "--concurrency",
                            "1",
                            "--trials",
                            "1",
                            "--messages",
                            "1",
                            "--body-bytes",
                            "32",
                            "--output",
                            str(output),
                        ]
                    )
            emitted = stdout.getvalue() + stderr.getvalue() + output.read_text(encoding="utf-8")
            self.assertNotIn("test-key-never-printed", emitted)
