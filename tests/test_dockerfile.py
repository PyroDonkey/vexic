from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hosted_docker_runtime_exposes_src_package(tmp_path: Path) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"(?m)^ENV\s+PYTHONPATH=([\"']?)/app/src\1\s*$", dockerfile)
    assert "COPY adapters ./adapters" in dockerfile
    assert "adapters.hosted_control_plane_http:create_app" in dockerfile

    pythonpath_export = (
        r"PYTHONPATH\s*=\s*([\"'])/app/src\$\{PYTHONPATH:\+:\$PYTHONPATH\}\1"
    )
    assert re.search(
        rf"(?m)^RUN\s+{pythonpath_export}\s+uv run --no-sync python -c ",
        dockerfile,
    )
    assert re.search(
        rf"(?m)^CMD\s+{pythonpath_export}\s+uv run --no-sync python -m uvicorn "
        r"adapters\.hosted_control_plane_http:create_app --factory ",
        dockerfile,
    )

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
