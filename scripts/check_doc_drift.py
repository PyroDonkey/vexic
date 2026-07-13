#!/usr/bin/env python
"""Warn when in-repo docs drift from in-repo code.

Also runs in CI with `--ci`: same checks, but findings go to stderr and drift
exits 1 so a drifting PR cannot merge.

Read-only. It checks three in-repo invariants and reports drift; it never edits
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

Closing the loop against the downstream tracking roadmap/todo stays a manual
step under the reconciliation triggers in AGENTS.md.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = REPO_ROOT / "docs" / "adr"
ADR_INDEX = ADR_DIR / "README.md"
CONTRACT = REPO_ROOT / "src" / "vexic" / "contract" / "__init__.py"
SERVICE = REPO_ROOT / "src" / "vexic" / "service.py"
AGENTS = REPO_ROOT / "AGENTS.md"
CONFIG_DOC = REPO_ROOT / "docs" / "configuration.md"
CODE_DIRS = (REPO_ROOT / "src", REPO_ROOT / "adapters")

ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")
ADR_INDEX_RE = re.compile(r"^\|\s*(\d{4})\s*\|")

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


def _check_adr_index(warnings: list[str]) -> None:
    files = {
        match.group(1)
        for path in ADR_DIR.glob("*.md")
        if (match := ADR_FILE_RE.match(path.name))
    }
    listed = {
        match.group(1)
        for line in ADR_INDEX.read_text(encoding="utf-8").splitlines()
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
    rest = agents_text[start + len(header):]
    end = len(rest)
    for marker in ("\n## ", "\n### ", "\n---"):
        idx = rest.find(marker)
        if idx != -1:
            end = min(end, idx)
    return rest[:end]


def _check_service_surface(warnings: list[str]) -> None:
    contract_ops = _async_methods(CONTRACT, "MemoryService")
    service_ops = _async_methods(SERVICE, "LocalMemoryService")
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
    # holds all 12 ops, including run_dream_phase, which is documented there as
    # a host-port op (in prose, not the bullet list), so this stays a true
    # check with no false positive on it.
    surface = _service_surface_section(AGENTS.read_text(encoding="utf-8"))
    undocumented = sorted(op for op in service_ops if f"`{op}`" not in surface)
    if undocumented:
        warnings.append(
            "The 'v0.1 Local Service Surface' section of AGENTS.md does not "
            "list service operation(s) present in code: "
            + ", ".join(undocumented)
            + ". Update that section and reconcile the downstream tracking "
            "roadmap/todo (see AGENTS.md)."
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


def _check_env_vars(warnings: list[str]) -> None:
    read: set[str] = set()
    mentioned: set[str] = set()
    for code_dir in CODE_DIRS:
        for path in code_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            read |= _env_names_read(tree)
            mentioned |= _names_mentioned(tree)

    documented = {
        name
        for line in CONFIG_DOC.read_text(encoding="utf-8").splitlines()
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


def main(ci: bool = False) -> int:
    if not REPO_ROOT.joinpath(".git").exists() and not ADR_DIR.exists():
        return 0

    warnings: list[str] = []
    checks = (
        ("ADR index", _check_adr_index),
        ("service surface", _check_service_surface),
        ("environment variables", _check_env_vars),
    )
    notes: list[str] = []
    for label, check in checks:
        try:
            check(warnings)
        except Exception as exc:  # fail safe: report, never block
            notes.append(f"Doc drift {label} check could not run: {exc!r}")

    if ci:
        # CI fails closed: drift blocks the merge, and a check that could not
        # run is treated as failure rather than silently passing.
        for line in notes + warnings:
            print(line, file=sys.stderr)
        if warnings or notes:
            return 1
        print(
            "Doc drift check: ADR index, LocalMemoryService surface, and the "
            "documented environment variables match the in-repo source of truth."
        )
        return 0

    if not warnings and not notes:
        _emit(
            "Doc drift check: ADR index, LocalMemoryService surface, and the "
            "documented environment variables match the in-repo source of truth.",
            None,
        )
        return 0

    body = "Doc drift check (read-only):\n\n" + "\n\n".join(notes + warnings)
    if warnings:
        system_message = (
            "In-repo doc drift detected; reconcile per AGENTS.md."
        )
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
