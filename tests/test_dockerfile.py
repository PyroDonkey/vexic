from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hosted_docker_runtime_exposes_src_package(tmp_path: Path) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'ENV PYTHONPATH="/app/src:${PYTHONPATH}"' in dockerfile

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import importlib.util; spec = importlib.util.find_spec('vexic'); "
            "assert spec is not None; print(spec.origin)",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert str(ROOT / "src") in completed.stdout
