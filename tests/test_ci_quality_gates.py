from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
ASCII_CHECKER = ROOT / "scripts" / "check_docs_ascii.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _load_ascii_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_ascii", ASCII_CHECKER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docs_ascii_checker_accepts_ascii_docs(tmp_path: Path) -> None:
    checker = _load_ascii_checker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ok.md").write_text("Plain ASCII docs.\n", encoding="utf-8")

    assert checker.main([str(docs)]) == 0


def test_docs_ascii_checker_rejects_non_ascii_docs(tmp_path: Path, capsys) -> None:
    checker = _load_ascii_checker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bad.md").write_text("Bad — punctuation.\n", encoding="utf-8")

    assert checker.main([str(docs)]) == 1
    captured = capsys.readouterr()
    assert "bad.md:1:5: non-ASCII U+2014" in captured.err


def test_ci_runs_annotation_and_docs_ascii_gates() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "uv run ruff check --select ANN --ignore ANN401 ." in workflow
    assert "uv run python scripts/check_docs_ascii.py docs" in workflow
