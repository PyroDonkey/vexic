"""Fixture-based specification for the widened doc-drift gate.

Each test builds a minimal, self-consistent repository tree in `tmp_path` and
then introduces exactly one drift, so the assertions pin the checker's
behavior rather than the current state of the real repository.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_doc_drift", ROOT / "scripts" / "check_doc_drift.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path) -> Path:
    """Write a minimal repository tree with no drift in it."""
    (tmp_path / ".git").mkdir()
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-first-decision.md").write_text(
        "# First decision\n\nStatus: accepted\n", encoding="utf-8"
    )
    (adr / "README.md").write_text(
        "| ADR  | Title          | Status   |\n"
        "| ---- | -------------- | -------- |\n"
        "| 0001 | First decision | accepted |\n",
        encoding="utf-8",
    )
    src = tmp_path / "src" / "vexic"
    (src / "contract").mkdir(parents=True)
    (src / "contract" / "__init__.py").write_text(
        "class MemoryService:\n    async def append_transcript(self) -> None: ...\n",
        encoding="utf-8",
    )
    (src / "service.py").write_text(
        "class LocalMemoryService:\n"
        "    async def append_transcript(self) -> None: ...\n",
        encoding="utf-8",
    )
    (src / "cli.py").write_text(
        'def main(argv):\n    if argv[0] == "mcp-stdio":\n        return 0\n',
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text(
        "### v0.1 Local Service Surface\n\n- `append_transcript`\n\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_flags_doc_reference_to_a_path_that_does_not_exist(tmp_path: Path) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "docs" / "usage.md").write_text(
        "Run the reference service in `src/vexic/service.py`.\n"
        "Its retired helper lived in `src/vexic/legacy_shim.py`.\n",
        encoding="utf-8",
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert any("src/vexic/legacy_shim.py" in warning for warning in warnings)
    assert not any("src/vexic/service.py" in warning for warning in warnings)


def test_flags_source_comment_claiming_a_path_that_does_not_exist(
    tmp_path: Path,
) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "src" / "vexic" / "pipeline.py").write_text(
        '"""Pipeline.\n\nPinned by tests/test_pipeline_removed.py.\n"""\n'
        "\n"
        "# Mirrors the schema in src/vexic/storage/schema.py.\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )

    warnings, _ = hook.collect_warnings(root)

    joined = "\n".join(warnings)
    assert "tests/test_pipeline_removed.py" in joined
    assert "src/vexic/storage/schema.py" in joined


def test_ignores_path_claims_in_historical_records(tmp_path: Path) -> None:
    """ADRs, provenance, and the changelog describe the repo as it was."""
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "docs" / "adr" / "0001-first-decision.md").write_text(
        "# First decision\n\nStatus: accepted\n\n"
        "We retired `.github/workflows/dream-cron.yml` and `src/vexic/old.py`.\n",
        encoding="utf-8",
    )
    (root / "docs" / "provenance.md").write_text(
        "Extracted from `scripts/eval_longmemeval_memory.py` in the source host.\n",
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "## 0.1.0\n\nRemoved `src/vexic/gone.py`.\n", encoding="utf-8"
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []


def test_flags_documented_command_for_a_module_that_no_longer_exists(
    tmp_path: Path,
) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "docs" / "usage.md").write_text(
        "```\nuv run python -m vexic.retired_module --flag\n```\n",
        encoding="utf-8",
    )

    warnings, _ = hook.collect_warnings(root)

    assert any("vexic.retired_module" in warning for warning in warnings)


def test_flags_documented_subcommand_the_cli_no_longer_knows(tmp_path: Path) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "docs" / "usage.md").write_text(
        "```\n"
        "vexic mcp-stdio --db-path ./memory.db\n"
        "vexic mcp-serve --db-path ./memory.db\n"
        "```\n",
        encoding="utf-8",
    )

    warnings, _ = hook.collect_warnings(root)

    joined = "\n".join(warnings)
    assert "mcp-serve" in joined
    assert "mcp-stdio" not in joined


def test_does_not_read_a_python_import_example_as_a_cli_call(tmp_path: Path) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "README.md").write_text(
        "```python\nfrom vexic import LocalMemoryService\nimport vexic\n```\n",
        encoding="utf-8",
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []


def test_does_not_read_command_arguments_as_subcommands(tmp_path: Path) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "docs" / "usage.md").write_text(
        "```\nuv run python -m vexic.cli mcp-stdio --tenant-id local --db ./x.db\n```\n",
        encoding="utf-8",
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []


def test_flags_stale_suite_test_count_in_a_doc(tmp_path: Path, monkeypatch) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "README.md").write_text(
        "The suite is 500 tests and stays green.\n", encoding="utf-8"
    )
    monkeypatch.setattr(hook, "_collect_test_count", lambda _root: 742)

    warnings, _ = hook.collect_warnings(root)

    joined = "\n".join(warnings)
    assert "500" in joined
    assert "742" in joined


def test_accepts_a_suite_test_count_that_matches(tmp_path: Path, monkeypatch) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "README.md").write_text(
        "`uv run pytest` reports 742 passed.\n", encoding="utf-8"
    )
    monkeypatch.setattr(hook, "_collect_test_count", lambda _root: 742)

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []


def test_does_not_read_a_test_delta_as_a_suite_total(
    tmp_path: Path, monkeypatch
) -> None:
    """ "Adds 3 tests" is a claim about a change, not about the suite size."""
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "CONTRIBUTING.md").write_text(
        "A bug fix adds 3 tests under `tests/`.\n", encoding="utf-8"
    )

    def _never(_root: Path) -> int:
        raise AssertionError("pytest must not be collected when no count is cited")

    monkeypatch.setattr(hook, "_collect_test_count", _never)

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []


def test_flags_reference_to_an_adr_that_does_not_exist(tmp_path: Path) -> None:
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "docs" / "architecture.md").write_text(
        "Storage follows ADR 0001. Encryption follows ADR 0099.\n",
        encoding="utf-8",
    )
    (root / "src" / "vexic" / "ports.py").write_text(
        '"""Ports.\n\nContent codec ports are ADR 0098.\n"""\n', encoding="utf-8"
    )

    warnings, _ = hook.collect_warnings(root)

    joined = "\n".join(warnings)
    assert "0099" in joined
    assert "0098" in joined
    assert "0001" not in joined


def test_does_not_read_code_identifiers_as_paths(tmp_path: Path) -> None:
    """`adapters/turso_adapter.reconcile_tenant_databases` is a symbol, not a file."""
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "docs" / "usage.md").write_text(
        "Call `adapters/turso_adapter.reconcile_tenant_databases` to reconcile.\n"
        "Every ADR under `docs/adr/*` is indexed.\n",
        encoding="utf-8",
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []
