#!/usr/bin/env python
"""Warn when in-repo docs drift from in-repo code.

Also runs in CI with `--ci`: same checks, but findings go to stderr and drift
exits 1 so a drifting PR cannot merge.

Read-only. It checks in-repo invariants and reports drift; it never edits
files. A hook cannot read an external tracking system, so this only enforces the
in-repo half of the "Docs Are Downstream Of Code" loop in AGENTS.md:

1. docs/adr/README.md lists every ADR file under docs/adr/ (and no phantom
   entries), so "the index lists 0001-0004 but nine ADRs exist" cannot recur.
2. The MemoryService contract Protocol and LocalMemoryService expose the same
   operation surface, and AGENTS.md mentions each operation, so an
   operation added in code without a doc update is surfaced.
3. docs/configuration.md catalogues every environment variable the code reads,
   and names no variable the code does not read. Both directions have drifted at
   once before: `VEXIC_CONTROL_PLANE_TARGET` was read but undocumented (an
   operator rebuilding from the docs would silently get the local control
   plane), while three `VEXIC_DREAM_TRIGGER_*` names outlived the cron workflow
   that read them.
4. Every repo-relative file path cited in a living doc or in a src/vexic
   comment or docstring exists on disk.
5. Every `vexic ...` / `python -m vexic.<module> ...` command cited in a living
   doc names a module that exists and a subcommand the module still knows.
6. Every `ADR NNNN` cited in a doc or in a src/vexic comment or docstring has a
   matching file under docs/adr/.
7. Every suite test count cited in a living doc matches what pytest collects.

Only mechanically checkable claims are checked. A free-text assertion in a
comment ("this is the only caller") is not statically decidable, so it is left
to review rather than guessed at: a doc gate that cries wolf gets ignored.

Closing the loop against the downstream tracking roadmap/todo stays a manual
step under the reconciliation triggers in AGENTS.md.
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories whose Python files are code this repo ships, relative to a root.
# Every check takes `root` so the suite can point it at a fixture repo; module
# constants pinned to REPO_ROOT would make that impossible.
CODE_DIRS = ("src", "adapters")

ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")
ADR_INDEX_RE = re.compile(r"^\|\s*(\d{4})\s*\|")
ADR_REF_RE = re.compile(r"\bADR[ -](\d{4})\b")

# Top-level directories whose contents are code or docs this repo owns, so a
# reference to a path under one of them is a claim about this repo.
REPO_DIRS = ("src", "tests", "scripts", "docs", "adapters", "examples", "pypi")
PATH_RE = re.compile(
    r"(?:" + "|".join(REPO_DIRS) + r")[/\\][A-Za-z0-9_./\\-]+",
)
# Only paths that name a file, not a bare prose fragment that happens to start
# with a repo directory name.
PATH_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".yml",
        ".yaml",
        ".toml",
        ".json",
        ".jsonl",
        ".txt",
        ".cfg",
        ".ini",
        ".sh",
        ".ps1",
    }
)

# Historical records describe the repo as it was, not as it is. An ADR may cite
# a workflow a later ADR retired (0025 cites `.github/workflows/dream-cron.yml`,
# which 0030 removed), the provenance doc cites paths in the private source
# host, and the changelog cites paths from past releases. Checking "does this
# path exist now" against them would report correct history as drift, so path
# and CLI checks scope to living docs only. ADR-reference checks still apply
# everywhere: ADR numbers are stable, so a dangling one is always drift.
HISTORICAL_DOCS = ("docs/adr/", "docs/provenance.md", "CHANGELOG.md")

# A documented shell command only counts as a Vexic CLI invocation if the line
# starts like a command. This keeps `from vexic import ...` in a Python example
# from being read as a `vexic` CLI call.
COMMAND_STARTS = ("vexic", "uv", "python", "$", ">")
SUBCOMMAND_RE = re.compile(r"[a-z][a-z0-9-]*$")

# How many leading tokens are subcommand *names* rather than argument *values*.
#
# The Vexic CLI nests exactly two subcommand levels (`vexic setup mcp-client`,
# `vexic recorder uninstall-mcp-client`); the token after them is a positional
# argument value. Nothing in a token's shape distinguishes the two -- in
# `vexic setup mcp-client myagent`, the client name `myagent` satisfies
# SUBCOMMAND_RE exactly as `mcp-client` does, and the collecting loop below
# stops at flags and paths but has no way to stop at a bare value.
#
# So validating every collected token would demand that the module define
# `myagent` as a subcommand and fire the gate on a doc that is correct. This
# bound is what keeps the check honest, not a shortcut around it: a stale
# third-level subcommand and a third-position argument value are the same string
# to a flat literal scan, and the only options are to bound the depth or to cry
# wolf. Raise this if and only if the CLI grows a third subcommand level.
CLI_SUBCOMMAND_DEPTH = 2

# Suite-total test-count claims. A bare "3 tests" in prose is usually a delta
# ("adds 3 tests"), not a suite total, so a cue word must appear on the line.
#
# Cues match on a word boundary, and `passed` is spelled out rather than left as
# a `pass` stem. A substring cue finds `pass` inside `passes`, so "Adds 3 tests;
# the gate passes." -- correct prose about a delta -- was read as a claim that
# the suite totals 3, and the gate fired on a doc that was right. A check that
# cries wolf is a check people learn to ignore.
#
# `passed` earns its place separately: "742 passed" is how pytest reports a
# total, and it is the one cited form that carries no other cue word on the
# line. `passes`/`passing` are deliberately not cues -- they describe a run's
# outcome, not its size.
TEST_COUNT_RE = re.compile(r"\b(\d[\d,_]*)\s+(?:tests?|passed)\b", re.IGNORECASE)
TEST_COUNT_CUES = ("suite", "collected", "passed", "total", "green", "pytest")
TEST_COUNT_CUE_RE = re.compile(
    r"\b(?:" + "|".join(TEST_COUNT_CUES) + r")\b", re.IGNORECASE
)

# An environment variable name as it appears as a string literal in code and as
# a backticked token in the docs. Deliberately excludes the `VEXIC_LIVE_<GROUP>
# _MODEL` doc row: that angle-bracketed name is a *pattern* (the model group is
# interpolated at runtime), not a literal, so it never matches here and needs no
# allowlist entry.
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
DOC_ENV_RE = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")
DOC_ENV_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9_]{2,}")

# Mapping parameters that carry the process environment. `resolve_storage_backend
# (env)` and friends take `env: Mapping[str, str]` rather than reading
# `os.environ` directly, so a literal read through one of these names counts.
ENV_MAPPING_NAMES = frozenset({"env", "environ"})


def _emit(additional_context: str | None, system_message: str | None) -> None:
    payload: dict[str, object] = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context or "",
        }
    }
    if system_message:
        payload["systemMessage"] = system_message
    json.dump(payload, sys.stdout)


def _async_methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(child, ast.AsyncFunctionDef)
            }
    raise ValueError(f"class {class_name} not found in {path}")


def _markdown_files(root: Path) -> list[Path]:
    """Every tracked markdown file, skipping dot-dirs and vendored trees."""
    skip = {"node_modules", "build", "dist", "site-packages"}
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(
            part.startswith(".") or part in skip
            for part in path.relative_to(root).parts[:-1]
        )
    )


def _source_files(root: Path) -> list[Path]:
    return sorted((root / "src" / "vexic").rglob("*.py"))


def _is_living(root: Path, path: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return not rel.startswith(HISTORICAL_DOCS)


def _path_refs(text: str) -> set[str]:
    """Repo-relative file paths asserted by `text`."""
    refs = set()
    for match in PATH_RE.finditer(text):
        ref = match.group(0).replace("\\", "/").rstrip(".,;:)`'\"")
        if "*" in ref or Path(ref).suffix not in PATH_SUFFIXES:
            continue
        refs.add(ref)
    return refs


def _comments_and_docstrings(path: Path) -> str:
    """Every comment and docstring in `path`, as one blob of text."""
    source = path.read_text(encoding="utf-8")
    chunks = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                chunks.append(docstring)
    return "\n".join(chunks)


def _check_adr_index(root: Path, warnings: list[str]) -> None:
    adr_dir = root / "docs" / "adr"
    files = {
        match.group(1)
        for path in adr_dir.glob("*.md")
        if (match := ADR_FILE_RE.match(path.name))
    }
    listed = {
        match.group(1)
        for line in (adr_dir / "README.md").read_text(encoding="utf-8").splitlines()
        if (match := ADR_INDEX_RE.match(line.strip()))
    }
    missing = sorted(files - listed)
    phantom = sorted(listed - files)
    if missing:
        warnings.append(
            "docs/adr/README.md is missing ADR(s) present on disk: "
            + ", ".join(missing)
            + ". Add them to the index (see 'Docs Are Downstream Of Code')."
        )
    if phantom:
        warnings.append(
            "docs/adr/README.md lists ADR(s) with no matching file: "
            + ", ".join(phantom)
            + ". Remove or fix the index entry."
        )


def _service_surface_section(agents_text: str) -> str:
    """Return the body of the service-surface section of AGENTS.md.

    Slices from the section header to the next markdown heading or horizontal
    rule. Raises if the header is absent so the caller records a "could not
    run" note rather than silently passing.
    """
    header = "### v0.1 Local Service Surface"
    start = agents_text.find(header)
    if start == -1:
        raise ValueError(
            "'### v0.1 Local Service Surface' section not found in AGENTS.md"
        )
    rest = agents_text[start + len(header) :]
    end = len(rest)
    for marker in ("\n## ", "\n### ", "\n---"):
        idx = rest.find(marker)
        if idx != -1:
            end = min(end, idx)
    return rest[:end]


def _check_service_surface(root: Path, warnings: list[str]) -> None:
    contract_ops = _async_methods(
        root / "src" / "vexic" / "contract" / "__init__.py", "MemoryService"
    )
    service_ops = _async_methods(
        root / "src" / "vexic" / "service.py", "LocalMemoryService"
    )
    only_contract = sorted(contract_ops - service_ops)
    only_service = sorted(service_ops - contract_ops)
    if only_contract:
        warnings.append(
            "MemoryService contract declares operation(s) LocalMemoryService "
            "does not implement: " + ", ".join(only_contract) + "."
        )
    if only_service:
        warnings.append(
            "LocalMemoryService implements operation(s) absent from the "
            "MemoryService contract: " + ", ".join(only_service) + "."
        )

    # Scope the documentation check to the authoritative
    # "v0.1 Local Service Surface" section, and match each operation as a
    # backticked token (`op`) rather than a bare substring. Section scoping
    # means an op dropped from that section is flagged even if it survives in
    # unrelated prose; backtick matching means `rebuild` is not counted as
    # documented just because the word "Rebuildable" contains it. The section
    # documents run_dream_phase as a host-port op in prose rather than in the
    # bullet list, which still satisfies the backtick match, so this stays a
    # true check with no false positive on it.
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    surface = _service_surface_section(agents)
    undocumented = sorted(op for op in service_ops if f"`{op}`" not in surface)
    if undocumented:
        warnings.append(
            "The 'v0.1 Local Service Surface' section of AGENTS.md does not "
            "list service operation(s) present in code: "
            + ", ".join(undocumented)
            + ". Update that section and reconcile the downstream tracking "
            "roadmap/todo (see AGENTS.md)."
        )


def _check_path_refs(root: Path, warnings: list[str]) -> None:
    """Every path a living doc or a src/vexic comment cites must exist."""
    sources: list[tuple[str, str]] = [
        (path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        for path in _markdown_files(root)
        if _is_living(root, path)
    ]
    sources += [
        (path.relative_to(root).as_posix(), _comments_and_docstrings(path))
        for path in _source_files(root)
    ]
    for name, text in sources:
        dangling = sorted(ref for ref in _path_refs(text) if not (root / ref).exists())
        if dangling:
            warnings.append(
                f"{name} references path(s) that do not exist: "
                + ", ".join(dangling)
                + ". Fix the reference or restore the path."
            )


def _is_env_source(node: ast.expr) -> bool:
    """True if `node` evaluates to the process environment.

    Recognizes `os.environ` and a bare `env`/`environ` mapping parameter.
    """
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id in ENV_MAPPING_NAMES


def _is_getenv(node: ast.expr) -> bool:
    """True only for `os.getenv`.

    Matching any attribute named `getenv` would treat an unrelated helper such
    as `settings.getenv("FEATURE_FLAG")` as a process-environment read and
    demand a docs row for it, so an unrelated API could block the gate.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "getenv"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_names_read(tree: ast.AST) -> set[str]:
    """Env var names read through a literal key.

    Strict on purpose: only `os.environ["X"]`, `os.environ.get("X")`,
    `os.getenv("X")`, and `env.get("X")` count. A dynamic read
    (`os.environ.get(name)`) yields no name here, which is why the reverse
    direction below searches for bare literals instead of demanding an env-read
    context.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Subscript) and _is_env_source(node.value):
            key = node.slice
        elif isinstance(node, ast.Call):
            func = node.func
            if _is_getenv(func) and node.args:
                key = node.args[0]
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_env_source(func.value)
                and node.args
            ):
                key = node.args[0]
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and ENV_NAME_RE.match(key.value)
        ):
            names.add(key.value)
    return names


def _names_mentioned(tree: ast.AST) -> set[str]:
    """Env-shaped tokens appearing anywhere inside a string literal.

    Substring scan, not exact match: a name is "mentioned" even when it is
    embedded in a larger string, such as the `"OPENROUTER_API_KEY is required"`
    of an error message or an f-string fragment. Only the dead-name direction
    uses this. Requiring a standalone literal there would flag a live variable
    as dead the moment its only literal moved into a message, and a false dead
    flag blocks a merge -- a worse failure than the narrow miss this allows (a
    variable named in an error string but no longer read).
    """
    mentioned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            mentioned.update(DOC_ENV_TOKEN_RE.findall(node.value))
    return mentioned


def _check_env_vars(root: Path, warnings: list[str]) -> None:
    read: set[str] = set()
    mentioned: set[str] = set()
    for code_dir in CODE_DIRS:
        for path in (root / code_dir).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            read |= _env_names_read(tree)
            mentioned |= _names_mentioned(tree)

    config_doc = root / "docs" / "configuration.md"
    documented = {
        name
        for line in config_doc.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("|")
        for name in DOC_ENV_RE.findall(line.split("|")[1] if "|" in line else "")
    }

    undocumented = sorted(read - documented)
    if undocumented:
        warnings.append(
            "docs/configuration.md does not document environment variable(s) "
            "read by src/ or adapters/: "
            + ", ".join(undocumented)
            + ". That file claims to catalogue every variable the code reads, "
            "and an operator rebuilding the deployment from it would silently "
            "get the default. Add a row (name only, never a value)."
        )

    # Reverse direction, deliberately looser: a documented name only has to be
    # mentioned in a string literal somewhere in the code, not read in an
    # env-read context. `VEXIC_API_KEY` is read through a variable key
    # (`--api-key-env` names it at runtime), so demanding an env-read context
    # here would flag a live variable as dead. A genuinely dead name -- the
    # retired `VEXIC_DREAM_TRIGGER_*` trio -- is mentioned nowhere at all.
    dead = sorted(documented - mentioned)
    if dead:
        warnings.append(
            "docs/configuration.md documents environment variable(s) that "
            "appear nowhere in src/ or adapters/: "
            + ", ".join(dead)
            + ". Remove the row, or the docs will outlive the code that read "
            "them."
        )


def _cited_test_counts(text: str) -> set[int]:
    """Suite-total test counts asserted by `text`."""
    counts: set[int] = set()
    for line in text.splitlines():
        if not TEST_COUNT_CUE_RE.search(line):
            continue
        for match in TEST_COUNT_RE.finditer(line):
            counts.add(int(match.group(1).replace(",", "").replace("_", "")))
    return counts


def _collect_test_count(root: Path) -> int:
    """The number of tests pytest collects under tests/."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    match = re.search(r"^(\d+) tests? collected", result.stdout, re.MULTILINE)
    if match is None:
        raise ValueError(
            "could not read a collected-test count from pytest --collect-only"
        )
    return int(match.group(1))


def _check_test_counts(root: Path, warnings: list[str]) -> None:
    """A doc citing a suite test count must cite the current one.

    AGENTS.md makes a test-count change a reconciliation trigger. Collecting
    the suite is not free, so pytest only runs when a doc actually cites a
    count; today none do, and the check costs nothing until one does.
    """
    cited: dict[str, set[int]] = {}
    for path in _markdown_files(root):
        if not _is_living(root, path):
            continue
        counts = _cited_test_counts(path.read_text(encoding="utf-8"))
        if counts:
            cited[path.relative_to(root).as_posix()] = counts
    if not cited:
        return
    actual = _collect_test_count(root)
    for name, counts in sorted(cited.items()):
        stale = sorted(count for count in counts if count != actual)
        if stale:
            warnings.append(
                f"{name} cites a suite test count of "
                + ", ".join(str(count) for count in stale)
                + f", but pytest collects {actual}. Re-run `uv run pytest` and "
                "update the count (see 'Docs Are Downstream Of Code')."
            )


def _check_adr_refs(root: Path, warnings: list[str]) -> None:
    """Every cited ADR number must have a file. ADR numbers never get reused."""
    adr_dir = root / "docs" / "adr"
    known = {
        match.group(1)
        for path in adr_dir.glob("*.md")
        if (match := ADR_FILE_RE.match(path.name))
    }
    sources: list[tuple[str, str]] = [
        (path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        for path in _markdown_files(root)
    ]
    sources += [
        (path.relative_to(root).as_posix(), _comments_and_docstrings(path))
        for path in _source_files(root)
    ]
    for name, text in sources:
        dangling = sorted(
            {
                match.group(1)
                for match in ADR_REF_RE.finditer(text)
                if match.group(1) not in known
            }
        )
        if dangling:
            warnings.append(
                f"{name} cites ADR(s) with no file under docs/adr/: "
                + ", ".join(dangling)
                + ". Fix the reference or add the ADR."
            )


def _module_path(root: Path, dotted: str) -> Path | None:
    rel = Path(*dotted.split("."))
    for candidate in (
        root / "src" / rel.with_suffix(".py"),
        root / "src" / rel / "__init__.py",
    ):
        if candidate.exists():
            return candidate
    return None


def _string_literals(root: Path, path: Path) -> set[str]:
    """String literals in `path` and in the vexic modules it imports.

    A subcommand name reaches argparse (or a dispatch comparison) as a literal,
    so a command the code still knows is a literal somewhere in the module that
    handles it. Following imports one hop covers `vexic.cli`, which delegates
    `recorder` and `mcp-stdio` to sibling modules.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals = set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "vexic"
        ):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(
                alias.name for alias in node.names if alias.name.startswith("vexic")
            )
    for dotted in imported:
        target = _module_path(root, dotted)
        if target is None:
            continue
        literals.update(
            node.value
            for node in ast.walk(ast.parse(target.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    return literals


def _documented_commands(text: str) -> set[tuple[str, tuple[str, ...]]]:
    """(module, subcommands) for each Vexic CLI invocation asserted by `text`."""
    commands: set[tuple[str, tuple[str, ...]]] = set()
    candidates: list[str] = []
    for line in text.splitlines():
        candidates.append(line)
        candidates.extend(re.findall(r"`([^`\n]+)`", line))
    for candidate in candidates:
        stripped = candidate.strip().lstrip("$> ").strip()
        if not stripped.startswith(COMMAND_STARTS):
            continue
        tokens = stripped.split()
        module = None
        rest: list[str] = []
        for index, token in enumerate(tokens):
            if (
                token == "-m"
                and index + 1 < len(tokens)
                and tokens[index + 1].startswith("vexic")
            ):
                module, rest = tokens[index + 1], tokens[index + 2 :]
                break
            if token == "vexic":
                # The `vexic` console script is vexic.cli:main.
                module, rest = "vexic.cli", tokens[index + 1 :]
                break
        if module is None:
            continue
        subcommands: list[str] = []
        for token in rest:
            # Stop at the first flag, placeholder, or path: everything past it
            # is an argument value, not a subcommand name.
            if not SUBCOMMAND_RE.match(token):
                break
            subcommands.append(token)
        # Bounded to the CLI's real subcommand depth: past it, a bare token is an
        # argument value, not a subcommand name. See CLI_SUBCOMMAND_DEPTH.
        commands.add((module, tuple(subcommands[:CLI_SUBCOMMAND_DEPTH])))
    return commands


def _check_cli_refs(root: Path, warnings: list[str]) -> None:
    """Documented Vexic commands must name a live module and live subcommands."""
    for path in _markdown_files(root):
        if not _is_living(root, path):
            continue
        name = path.relative_to(root).as_posix()
        for module, subcommands in sorted(
            _documented_commands(path.read_text(encoding="utf-8"))
        ):
            target = _module_path(root, module)
            if target is None:
                warnings.append(
                    f"{name} documents `python -m {module}`, but that module "
                    "does not exist under src/. Fix the doc or restore the "
                    "module."
                )
                continue
            if not subcommands:
                continue
            literals = _string_literals(root, target)
            unknown = [token for token in subcommands if token not in literals]
            if unknown:
                warnings.append(
                    f"{name} documents subcommand(s) {', '.join(unknown)} for "
                    f"`{module}`, but the module no longer defines them. Fix "
                    "the doc or restore the command."
                )


def collect_warnings(root: Path) -> tuple[list[str], list[str]]:
    """Run every check against `root`. Returns (drift warnings, could-not-run)."""
    warnings: list[str] = []
    notes: list[str] = []
    checks = (
        ("ADR index", _check_adr_index),
        ("service surface", _check_service_surface),
        ("environment variables", _check_env_vars),
        ("path reference", _check_path_refs),
        ("CLI reference", _check_cli_refs),
        ("ADR reference", _check_adr_refs),
        ("test count", _check_test_counts),
    )
    for label, check in checks:
        try:
            check(root, warnings)
        except Exception as exc:  # fail safe: report, never block
            notes.append(f"Doc drift {label} check could not run: {exc!r}")
    return warnings, notes


SUMMARY = (
    "Doc drift check: ADR index, LocalMemoryService surface, documented "
    "environment variables, and doc references match the in-repo source of "
    "truth."
)


def main(ci: bool = False) -> int:
    if (
        not REPO_ROOT.joinpath(".git").exists()
        and not (REPO_ROOT / "docs" / "adr").exists()
    ):
        return 0

    warnings, notes = collect_warnings(REPO_ROOT)

    if ci:
        # CI fails closed: drift blocks the merge, and a check that could not
        # run is treated as failure rather than silently passing.
        for line in notes + warnings:
            print(line, file=sys.stderr)
        if warnings or notes:
            return 1
        print(SUMMARY)
        return 0

    if not warnings and not notes:
        _emit(SUMMARY, None)
        return 0

    body = "Doc drift check (read-only):\n\n" + "\n\n".join(notes + warnings)
    if warnings:
        system_message = "In-repo doc drift detected; reconcile per AGENTS.md."
    else:
        # Only "could not run" notes (e.g. partial checkout): do not imply drift.
        system_message = "Doc drift check could not fully run; see context."
    _emit(body, system_message)
    return 0


if __name__ == "__main__":
    if "--ci" in sys.argv[1:]:
        # No fail-safe wrapper in CI: an unexpected error must fail the job.
        sys.exit(main(ci=True))
    try:
        sys.exit(main())
    except Exception as exc:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": f"Doc drift hook errored: {exc!r}",
                }
            },
            sys.stdout,
        )
        sys.exit(0)
