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

    assert re.search(r"(?m)^ENV\s+PYTHONPATH=([\"']?)/app/src:/app\1\s*$", dockerfile)
    assert "COPY adapters ./adapters" in dockerfile
    assert "vexic.hosted_control_plane_http:create_app" in dockerfile
    assert "import adapters.turso_adapter" in dockerfile
    users = re.findall(r"(?m)^USER\s+(.+)$", dockerfile)
    assert users
    assert users[-1].strip() == "root"
    assert re.search(r"(?m)chown\s+-R\s+\S+:\S+\s+/data/vexic\b", dockerfile)
    assert "root startup repairs the Railway volume" in dockerfile
    assert (
        'CMD ["/app/.venv/bin/python", "-m", "vexic.hosted_entrypoint"]'
        in dockerfile
    )

    pythonpath_export = r"PYTHONPATH\s*=\s*([\"'])/app/src:/app\$\{PYTHONPATH:\+:\$PYTHONPATH\}\1"
    assert re.search(
        rf"(?m)^RUN\s+{pythonpath_export}\s+uv run --no-sync python -c ",
        dockerfile,
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import importlib.util; spec = importlib.util.find_spec('vexic'); "
            "adapter = importlib.util.find_spec('adapters.turso_adapter'); "
            "assert spec is not None and adapter is not None; print(spec.origin)",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert str(ROOT / "src") in completed.stdout


def test_dockerignore_excludes_env_files() -> None:
    # Dockerfile COPY is path-selective today; this guards against a future
    # broad COPY shipping working-tree secrets into the image.
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    entries = {line.strip() for line in dockerignore.splitlines() if line.strip()}
    assert ".env" in entries
    assert ".env.*" in entries


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


def _chown_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path, list[Path]]:
    """A volume tree plus a recorder replacing the real os.lchown."""
    from vexic import hosted_entrypoint

    root = tmp_path / "volume"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "customer.db").write_text("", encoding="utf-8")

    chowned: list[Path] = []
    monkeypatch.setattr(
        hosted_entrypoint.os,
        "lchown",
        lambda path, uid, gid: chowned.append(Path(path)),
    )
    return root, nested, nested / "customer.db", chowned


def test_chown_tree_skips_walk_when_sentinel_and_root_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A completed repair (matching sentinel + correctly-owned root) must not
    trigger the O(volume) re-chown on later boots."""
    if os.name == "nt":
        pytest.skip("hosted_entrypoint uses POSIX uid/gid APIs")

    from vexic import hosted_entrypoint

    root, _nested, _db, chowned = _chown_fixture(monkeypatch, tmp_path)
    stat = root.lstat()
    (root / ".chown-complete").write_text(
        f"{stat.st_uid}:{stat.st_gid}", encoding="utf-8"
    )

    hosted_entrypoint._chown_tree(root, stat.st_uid, stat.st_gid)

    assert chowned == []


def test_chown_tree_walks_when_root_correct_but_sentinel_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A correctly-owned root alone is not proof of a completed repair: the
    pre-sentinel entrypoint chowned the root first, so an interrupted old
    repair (or an external writer) can leave a correct root over mis-owned
    children. Without the completion sentinel the walk must run."""
    if os.name == "nt":
        pytest.skip("hosted_entrypoint uses POSIX uid/gid APIs")

    from vexic import hosted_entrypoint

    root, nested, db, chowned = _chown_fixture(monkeypatch, tmp_path)
    stat = root.lstat()

    hosted_entrypoint._chown_tree(root, stat.st_uid, stat.st_gid)

    assert {nested, db, root} <= set(chowned)


def test_chown_tree_walks_when_sentinel_records_other_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A runtime uid/gid change invalidates the previous repair's sentinel."""
    if os.name == "nt":
        pytest.skip("hosted_entrypoint uses POSIX uid/gid APIs")

    from vexic import hosted_entrypoint

    root, nested, db, chowned = _chown_fixture(monkeypatch, tmp_path)
    (root / ".chown-complete").write_text("999:999", encoding="utf-8")
    stat = root.lstat()

    hosted_entrypoint._chown_tree(root, stat.st_uid, stat.st_gid)

    assert {nested, db, root} <= set(chowned)


def test_chown_tree_repairs_bottom_up_and_stamps_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The root directory flips ownership last and the completion sentinel is
    written first: an interrupted repair leaves either the sentinel stale or
    the root mismatched, so the next boot resumes the walk."""
    if os.name == "nt":
        pytest.skip("hosted_entrypoint uses POSIX uid/gid APIs")

    from vexic import hosted_entrypoint

    root, nested, db, chowned = _chown_fixture(monkeypatch, tmp_path)

    # Target ids differ from the tree's actual owner, so a repair must run.
    hosted_entrypoint._chown_tree(root, 10001, 10001)

    sentinel = root / ".chown-complete"
    assert sentinel.read_text(encoding="utf-8") == "10001:10001"
    assert set(chowned) == {root, nested, db, sentinel}
    assert chowned[-1] == root
