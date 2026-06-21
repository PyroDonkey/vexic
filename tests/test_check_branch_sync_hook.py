from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_branch_sync", ROOT / ".claude" / "hooks" / "check_branch_sync.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _completed(
    args: tuple[str, ...],
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_branch_sync_reports_failed_drift_comparisons(monkeypatch, capsys) -> None:
    hook = _load_hook()

    def fake_git(*args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
        if args == ("rev-parse", "--is-inside-work-tree"):
            return _completed(args, 0, "true\n")
        if args == ("fetch", "origin"):
            return _completed(args, 0)
        if args[:3] == ("rev-list", "--left-right", "--count"):
            return _completed(
                args,
                128,
                stderr=f"fatal: ambiguous argument '{args[3]}'",
            )
        raise AssertionError(args)

    monkeypatch.setattr(hook, "_git", fake_git)

    assert hook.main() == 0

    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "up to date" not in context
    assert "Unable to compute drift for `origin/main...dev`" in context
    assert "fatal: ambiguous argument 'origin/main...dev'" in context
