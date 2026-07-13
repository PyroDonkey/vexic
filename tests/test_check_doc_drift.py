"""Pin the doc-drift gate's environment-variable check.

The check exists because prose drifted from code in both directions at once: a
live flag went undocumented while three dead names outlived the workflow that
read them. A gate that cannot fail would have caught neither, so these tests
drive it to failure deliberately.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def drift() -> ModuleType:
    """Load scripts/check_doc_drift.py, which is a script, not a package."""
    path = REPO_ROOT / "scripts" / "check_doc_drift.py"
    spec = importlib.util.spec_from_file_location("check_doc_drift", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_code(tmp_path: Path, body: str) -> Path:
    code_dir = tmp_path / "src"
    code_dir.mkdir()
    (code_dir / "mod.py").write_text(body, encoding="utf-8")
    return code_dir


def _write_doc(tmp_path: Path, rows: str) -> Path:
    doc = tmp_path / "configuration.md"
    doc.write_text(
        "| Variable | Component | Default | Notes |\n| --- | --- | --- | --- |\n"
        + rows,
        encoding="utf-8",
    )
    return doc


def _run(
    drift: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    code_dir: Path,
    doc: Path,
) -> list[str]:
    monkeypatch.setattr(drift, "CODE_DIRS", (code_dir,))
    monkeypatch.setattr(drift, "CONFIG_DOC", doc)
    warnings: list[str] = []
    drift._check_env_vars(warnings)
    return warnings


def test_documented_and_read_variable_does_not_drift(
    drift: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code_dir = _write_code(
        tmp_path,
        'import os\nos.environ.get("VEXIC_CONTROL_PLANE_TARGET", "local")\n',
    )
    doc = _write_doc(tmp_path, "| `VEXIC_CONTROL_PLANE_TARGET` | x | `local` | y |\n")

    assert _run(drift, monkeypatch, code_dir=code_dir, doc=doc) == []


def test_variable_read_but_undocumented_is_drift(
    drift: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live-flag failure: an operator rebuilding from the docs would miss it
    and silently get the default."""
    code_dir = _write_code(
        tmp_path,
        "import os\n"
        'os.environ.get("PORT", "8000")\n'
        'os.environ.get("VEXIC_CONTROL_PLANE_TARGET", "local")\n',
    )
    doc = _write_doc(tmp_path, "| `PORT` | x | `8000` | y |\n")

    (warning,) = _run(drift, monkeypatch, code_dir=code_dir, doc=doc)
    assert "VEXIC_CONTROL_PLANE_TARGET" in warning
    assert "does not document" in warning


def test_documented_but_dead_variable_is_drift(
    drift: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The retired dream-cron names: documented long after the code that read
    them was deleted."""
    code_dir = _write_code(tmp_path, 'import os\nos.environ.get("PORT", "8000")\n')
    doc = _write_doc(
        tmp_path,
        "| `PORT` | x | `8000` | y |\n"
        "| `VEXIC_DREAM_TRIGGER_URL` | x | -- | y |\n",
    )

    (warning,) = _run(drift, monkeypatch, code_dir=code_dir, doc=doc)
    assert "VEXIC_DREAM_TRIGGER_URL" in warning
    assert "appear nowhere" in warning


def test_env_read_through_a_mapping_parameter_counts(
    drift: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`resolve_storage_backend(env)` takes a Mapping rather than touching
    os.environ, so a literal read through it must still count as a read."""
    code_dir = _write_code(
        tmp_path,
        "import os\n"
        'os.environ.get("PORT", "8000")\n'
        "def resolve(env):\n"
        '    return env.get("VEXIC_STORAGE_BACKEND", "local")\n',
    )
    doc = _write_doc(tmp_path, "| `PORT` | x | `8000` | y |\n")

    (warning,) = _run(drift, monkeypatch, code_dir=code_dir, doc=doc)
    assert "VEXIC_STORAGE_BACKEND" in warning


def test_dynamically_read_name_is_not_reported_dead(
    drift: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`VEXIC_API_KEY` is read through a variable key -- `--api-key-env` names it
    at runtime. It is documented and alive, so the dead-name direction must not
    flag it merely because no literal env-read context exists."""
    code_dir = _write_code(
        tmp_path,
        'import os\ndef read(name="VEXIC_API_KEY"):\n    return os.environ.get(name)\n',
    )
    doc = _write_doc(tmp_path, "| `VEXIC_API_KEY` | x | -- | y |\n")

    assert _run(drift, monkeypatch, code_dir=code_dir, doc=doc) == []


def test_real_repo_has_no_environment_drift(drift: ModuleType) -> None:
    """The gate must hold against the committed tree, not just fixtures."""
    warnings: list[str] = []
    drift._check_env_vars(warnings)
    assert warnings == []
