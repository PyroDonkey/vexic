from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_hosted_docker_runtime_exposes_src_package(tmp_path: Path) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"(?m)^ENV\s+PYTHONPATH=([\"']?)/app/src\1\s*$", dockerfile)
    assert "COPY adapters ./adapters" not in dockerfile
    assert "vexic.hosted_control_plane_http:create_app" in dockerfile
    users = re.findall(r"(?m)^USER\s+(.+)$", dockerfile)
    assert users
    assert users[-1].strip() == "root"
    assert re.search(r"(?m)chown\s+-R\s+\S+:\S+\s+/data/vexic\b", dockerfile)
    assert "root startup repairs the Railway volume" in dockerfile
    assert (
        'CMD ["/app/.venv/bin/python", "-m", "vexic.hosted_entrypoint"]'
        in dockerfile
    )

    pythonpath_export = (
        r"PYTHONPATH\s*=\s*([\"'])/app/src\$\{PYTHONPATH:\+:\$PYTHONPATH\}\1"
    )
    assert re.search(
        rf"(?m)^RUN\s+{pythonpath_export}\s+uv run --no-sync python -c ",
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


def test_hosted_entrypoint_repairs_volume_then_drops_privileges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("hosted_entrypoint uses POSIX uid/gid APIs")

    from vexic import hosted_entrypoint

    nested = tmp_path / "nested"
    nested.mkdir()
    db_path = nested / "control-plane.db"
    db_path.write_text("", encoding="utf-8")
    events: list[tuple[str, object]] = []
    chowned: list[Path] = []

    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setattr(hosted_entrypoint.os, "getuid", lambda: 0)
    monkeypatch.setattr(hosted_entrypoint, "_runtime_ids", lambda user: (10001, 10001))
    monkeypatch.setattr(
        hosted_entrypoint.os,
        "lchown",
        lambda path, uid, gid: chowned.append(Path(path).relative_to(tmp_path)),
    )
    monkeypatch.setattr(
        hosted_entrypoint.os,
        "setgroups",
        lambda groups: events.append(("setgroups", groups)),
    )
    monkeypatch.setattr(
        hosted_entrypoint.os,
        "setgid",
        lambda gid: events.append(("setgid", gid)),
    )
    monkeypatch.setattr(
        hosted_entrypoint.os,
        "setuid",
        lambda uid: events.append(("setuid", uid)),
    )

    def fake_execv(path: str, args: list[str]) -> None:
        events.append(("execv", (path, args)))
        raise SystemExit(0)

    monkeypatch.setattr(hosted_entrypoint.os, "execv", fake_execv)

    with pytest.raises(SystemExit):
        hosted_entrypoint.main()

    assert {Path("."), Path("nested"), Path("nested/control-plane.db")} <= set(chowned)
    assert events[:3] == [("setgroups", []), ("setgid", 10001), ("setuid", 10001)]
    assert events[-1] == (
        "execv",
        (
            "/app/.venv/bin/python",
            [
                "/app/.venv/bin/python",
                "-m",
                "uvicorn",
                "vexic.hosted_control_plane_http:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                "8123",
            ],
        ),
    )
