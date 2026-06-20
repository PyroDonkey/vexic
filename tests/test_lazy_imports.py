import subprocess
import sys
import unittest
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VexicLazyImportTests(unittest.TestCase):
    def test_importing_vexic_does_not_load_sentence_transformers(self) -> None:
        script = (
            "import importlib, sys; "
            "importlib.import_module('vexic'); "
            "raise SystemExit(1 if 'sentence_transformers' in sys.modules else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
