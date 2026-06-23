#!/usr/bin/env python
"""SessionStart hook: warn when in-repo docs drift from in-repo code.

Read-only. It checks two in-repo invariants and reports drift; it never edits
files. A hook cannot read an external tracking system, so this only enforces the
in-repo half of the "Docs Are Downstream Of Code" loop in AGENTS.md:

1. docs/adr/README.md lists every ADR file under docs/adr/ (and no phantom
   entries), so "the index lists 0001-0004 but nine ADRs exist" cannot recur.
2. The MemoryService contract Protocol and LocalMemoryService expose the same
   operation surface, and AGENTS.md mentions each operation, so an operation
   added in code without a doc update is surfaced.

Closing the loop against the downstream tracking roadmap/todo (named in
AGENTS.md) stays a manual step under the reconciliation triggers there.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"
ADR_INDEX = ADR_DIR / "README.md"
CONTRACT = REPO_ROOT / "src" / "vexic" / "contract" / "__init__.py"
SERVICE = REPO_ROOT / "src" / "vexic" / "service.py"
AGENTS = REPO_ROOT / "AGENTS.md"

ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")
ADR_INDEX_RE = re.compile(r"^\|\s*(\d{4})\s*\|")


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

    # Match each operation as a backticked token (`op`) rather than a bare
    # substring, so e.g. the `rebuild` op is not considered documented just
    # because the prose word "Rebuildable" contains it. AGENTS.md backticks
    # every operation name, including run_dream_phase (documented as a
    # host-port op outside the bullet list), so this stays a true check.
    agents_text = AGENTS.read_text(encoding="utf-8")
    undocumented = sorted(op for op in service_ops if f"`{op}`" not in agents_text)
    if undocumented:
        warnings.append(
            "AGENTS.md does not mention service operation(s) present in code: "
            + ", ".join(undocumented)
            + ". Update the 'v0.1 Local Service Surface' section and reconcile "
            "the downstream tracking roadmap/todo (see AGENTS.md)."
        )


def main() -> int:
    if not REPO_ROOT.joinpath(".git").exists() and not ADR_DIR.exists():
        return 0

    warnings: list[str] = []
    checks = (
        ("ADR index", _check_adr_index),
        ("service surface", _check_service_surface),
    )
    notes: list[str] = []
    for label, check in checks:
        try:
            check(warnings)
        except Exception as exc:  # fail safe: report, never block
            notes.append(f"Doc drift {label} check could not run: {exc!r}")

    if not warnings and not notes:
        _emit(
            "Doc drift check: ADR index and LocalMemoryService surface match "
            "the in-repo source of truth.",
            None,
        )
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
