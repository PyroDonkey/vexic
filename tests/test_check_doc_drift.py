"""Fixture-based specification for the widened doc-drift gate.

Each test builds a minimal, self-consistent repository tree in `tmp_path` and
then introduces exactly one drift, so the assertions pin the checker's
behavior rather than the current state of the real repository.

The environment-variable check is pinned the same way. It exists because prose
drifted from code in both directions at once: a live flag went undocumented
while three dead names outlived the workflow that read them. A gate that cannot
fail would have caught neither, so those tests drive it to failure deliberately.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_hook() -> ModuleType:
    """Load scripts/check_doc_drift.py, which is a script, not a package."""
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
    _write_config_doc(tmp_path, "")
    (tmp_path / "tests").mkdir()
    return tmp_path


def _write_config_doc(root: Path, rows: str) -> Path:
    """Write docs/configuration.md with `rows` as its variable table body."""
    doc = root / "docs" / "configuration.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "| Variable | Component | Default | Notes |\n| --- | --- | --- | --- |\n"
        + rows,
        encoding="utf-8",
    )
    return doc


def _write_env_code(root: Path, body: str) -> None:
    """Write a src/ module whose environment reads the env gate must see."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "mod.py").write_text(body, encoding="utf-8")


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


def test_flags_a_second_level_subcommand_the_cli_no_longer_knows(
    tmp_path: Path,
) -> None:
    """The depth bound must reach every subcommand level the CLI actually has.

    `vexic recorder uninstall-mcp-client` nests two deep, so a bound that
    stopped at one would leave the second level unvalidated and a renamed
    subcommand there would never be flagged.
    """
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "src" / "vexic" / "cli.py").write_text(
        'SUBCOMMANDS = ("recorder", "ingest")\n', encoding="utf-8"
    )
    (root / "docs" / "usage.md").write_text(
        "```\n"
        "vexic recorder ingest --db-path ./memory.db\n"
        "vexic recorder retired-step\n"
        "```\n",
        encoding="utf-8",
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    joined = "\n".join(warnings)
    assert "retired-step" in joined
    assert "ingest" not in joined


def test_does_not_read_a_positional_argument_value_as_a_subcommand(
    tmp_path: Path,
) -> None:
    """`vexic setup mcp-client myagent` names a client; `myagent` is the value of
    the `name` positional, not a subcommand.

    Nothing in the token shape separates the two -- a bare argument value and a
    subcommand name are both lowercase words, and SUBCOMMAND_RE matches both --
    so validating every token past the CLI's real subcommand depth would demand
    that the module define `myagent`, and the gate would fire on a doc that is
    correct. This is why the check bounds the depth it validates rather than
    walking the whole token list.
    """
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "src" / "vexic" / "cli.py").write_text(
        'SUBCOMMANDS = ("setup", "mcp-client")\n', encoding="utf-8"
    )
    (root / "docs" / "usage.md").write_text(
        "```\nvexic setup mcp-client myagent --base-url https://api.vexic.dev\n```\n",
        encoding="utf-8",
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []


def test_resolves_a_subcommand_through_a_module_the_cli_imports(
    tmp_path: Path,
) -> None:
    """`vexic recorder ingest` resolves through `vexic.cli` into
    `vexic.recorders.cli`, where the `ingest` literal actually lives.

    Every other CLI fixture keeps the literal in `cli.py` itself, so the one-hop
    import walk in `_string_literals` never executes and a regression in it
    would ship green. `ingest` is deliberately absent from `cli.py` here: the
    only way it can resolve is through the import, and the stale `retired-step`
    on the next line proves the check is live rather than vacuously silent.
    """
    hook = _load_hook()
    root = _repo(tmp_path)
    recorders = root / "src" / "vexic" / "recorders"
    recorders.mkdir(parents=True)
    (recorders / "__init__.py").write_text("", encoding="utf-8")
    (recorders / "cli.py").write_text(
        "def build_parser(subparsers):\n"
        '    subparsers.add_parser("ingest")\n'
        "    return subparsers\n",
        encoding="utf-8",
    )
    (root / "src" / "vexic" / "cli.py").write_text(
        "from vexic.recorders.cli import build_parser\n"
        "\n"
        "\n"
        "def main(argv):\n"
        '    if argv[0] == "recorder":\n'
        "        return build_parser(argv)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (root / "docs" / "usage.md").write_text(
        "```\n"
        "vexic recorder ingest --db-path ./memory.db\n"
        "vexic recorder retired-step\n"
        "```\n",
        encoding="utf-8",
    )

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    joined = "\n".join(warnings)
    assert "retired-step" in joined
    assert "ingest" not in joined


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


def test_does_not_read_a_passing_gate_as_a_suite_total(
    tmp_path: Path, monkeypatch
) -> None:
    """ "the gate passes" is prose about a run, not a suite-total cue.

    A substring cue match finds `pass` inside `passes`, then reads the delta
    ("3 tests") as the suite total -- the gate firing on correct docs. A check
    that cries wolf is a check people learn to ignore, so the cue has to match
    on a word boundary.
    """
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "CONTRIBUTING.md").write_text(
        "Adds 3 tests; the gate passes.\n", encoding="utf-8"
    )

    def _never(_root: Path) -> int:
        raise AssertionError("pytest must not be collected when no count is cited")

    monkeypatch.setattr(hook, "_collect_test_count", _never)

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert warnings == []


def test_still_reads_a_bare_pytest_passed_line_as_a_suite_total(
    tmp_path: Path, monkeypatch
) -> None:
    """The word-boundary fix must not cost the check its reach.

    "742 passed" is how pytest reports a suite total, and it is the one form
    that carries no other cue word. Narrowing the cue to a bare `pass` would
    silently stop catching it.
    """
    hook = _load_hook()
    root = _repo(tmp_path)
    (root / "README.md").write_text("The last run: 500 passed.\n", encoding="utf-8")
    monkeypatch.setattr(hook, "_collect_test_count", lambda _root: 742)

    warnings, _ = hook.collect_warnings(root)

    joined = "\n".join(warnings)
    assert "500" in joined
    assert "742" in joined


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


def _env_warnings(tmp_path: Path, *, code: str, rows: str) -> list[str]:
    """Run only the environment-variable check over a fixture repo."""
    hook = _load_hook()
    root = _repo(tmp_path)
    _write_env_code(root, code)
    _write_config_doc(root, rows)
    warnings: list[str] = []
    hook._check_env_vars(root, warnings)
    return warnings


def test_environment_check_is_registered_in_the_gate(tmp_path: Path) -> None:
    """The env gate must fire through `collect_warnings`, not just when called
    directly. Registration is what a careless merge drops silently: the four
    doc-reference checks would still pass and the suite would still look green
    while a shipped check had quietly stopped running."""
    hook = _load_hook()
    root = _repo(tmp_path)
    _write_env_code(root, 'import os\nos.environ.get("VEXIC_TURSO_TOKEN", "")\n')

    warnings, notes = hook.collect_warnings(root)

    assert notes == []
    assert any("VEXIC_TURSO_TOKEN" in warning for warning in warnings)


def test_documented_and_read_variable_does_not_drift(tmp_path: Path) -> None:
    assert (
        _env_warnings(
            tmp_path,
            code='import os\nos.environ.get("VEXIC_CONTROL_PLANE_TARGET", "local")\n',
            rows="| `VEXIC_CONTROL_PLANE_TARGET` | x | `local` | y |\n",
        )
        == []
    )


def test_variable_read_but_undocumented_is_drift(tmp_path: Path) -> None:
    """The live-flag failure: an operator rebuilding from the docs would miss it
    and silently get the default."""
    (warning,) = _env_warnings(
        tmp_path,
        code="import os\n"
        'os.environ.get("PORT", "8000")\n'
        'os.environ.get("VEXIC_CONTROL_PLANE_TARGET", "local")\n',
        rows="| `PORT` | x | `8000` | y |\n",
    )

    assert "VEXIC_CONTROL_PLANE_TARGET" in warning
    assert "does not document" in warning


def test_documented_but_dead_variable_is_drift(tmp_path: Path) -> None:
    """The retired dream-cron names: documented long after the code that read
    them was deleted."""
    (warning,) = _env_warnings(
        tmp_path,
        code='import os\nos.environ.get("PORT", "8000")\n',
        rows="| `PORT` | x | `8000` | y |\n"
        "| `VEXIC_DREAM_TRIGGER_URL` | x | -- | y |\n",
    )

    assert "VEXIC_DREAM_TRIGGER_URL" in warning
    assert "appear nowhere" in warning


def test_env_read_through_a_mapping_parameter_counts(tmp_path: Path) -> None:
    """`resolve_storage_backend(env)` takes a Mapping rather than touching
    os.environ, so a literal read through it must still count as a read."""
    (warning,) = _env_warnings(
        tmp_path,
        code="import os\n"
        'os.environ.get("PORT", "8000")\n'
        "def resolve(env):\n"
        '    return env.get("VEXIC_STORAGE_BACKEND", "local")\n',
        rows="| `PORT` | x | `8000` | y |\n",
    )

    assert "VEXIC_STORAGE_BACKEND" in warning


def test_dynamically_read_name_is_not_reported_dead(tmp_path: Path) -> None:
    """`VEXIC_API_KEY` is read through a variable key -- `--api-key-env` names it
    at runtime. It is documented and alive, so the dead-name direction must not
    flag it merely because no literal env-read context exists."""
    assert (
        _env_warnings(
            tmp_path,
            code='import os\ndef read(name="VEXIC_API_KEY"):\n'
            "    return os.environ.get(name)\n",
            rows="| `VEXIC_API_KEY` | x | -- | y |\n",
        )
        == []
    )


def test_unrelated_getenv_helper_is_not_an_environment_read(tmp_path: Path) -> None:
    """Only `os.getenv` reads the process environment. A helper that happens to
    have a method of the same name must not force a docs row for its argument,
    or an unrelated API could block the gate."""
    assert (
        _env_warnings(
            tmp_path,
            code="import os\n"
            'os.environ.get("PORT", "8000")\n'
            "settings = object()\n"
            'settings.getenv("FEATURE_FLAG")\n',
            rows="| `PORT` | x | `8000` | y |\n",
        )
        == []
    )


def test_name_mentioned_only_inside_a_larger_literal_is_not_dead(
    tmp_path: Path,
) -> None:
    """A documented name whose only literal is embedded in a message -- an error
    string, an f-string fragment -- is still alive. A false dead flag blocks a
    merge, which is worse than the narrow miss this allows."""
    assert (
        _env_warnings(
            tmp_path,
            code="import os\n"
            'os.environ.get("PORT", "8000")\n'
            'raise RuntimeError("OPENROUTER_API_KEY is required")\n',
            rows="| `PORT` | x | `8000` | y |\n| `OPENROUTER_API_KEY` | x | -- | y |\n",
        )
        == []
    )


def test_real_repo_has_no_environment_drift() -> None:
    """The gate must hold against the committed tree, not just fixtures."""
    hook = _load_hook()
    warnings: list[str] = []
    hook._check_env_vars(ROOT, warnings)

    assert warnings == []
