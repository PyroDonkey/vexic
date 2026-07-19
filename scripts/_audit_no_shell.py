#!/usr/bin/env python3
"""Emit release audit dump when shell git/gh are unavailable to the agent."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tmp_audit_out.txt"


def run(cmd: list[str]) -> str:
    p = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    out = p.stdout
    if p.stderr:
        out += p.stderr
    if p.returncode != 0:
        out += f"\n[exit {p.returncode}]\n"
    return out


def main() -> int:
    sections: list[str] = []

    sections.append("=== 1. git rev-parse origin/main origin/dev HEAD ===")
    sections.append(run(["git", "rev-parse", "origin/main", "origin/dev", "HEAD"]).rstrip())

    sections.append("\n=== 2. git log --oneline origin/main..dev ===")
    sections.append(run(["git", "log", "--oneline", "origin/main..dev"]).rstrip())

    sections.append("\n=== 3. git diff --stat origin/main...dev ===")
    sections.append(run(["git", "diff", "--stat", "origin/main...dev"]).rstrip())

    sections.append("\n=== 4. git diff --name-only origin/main...dev ===")
    sections.append(run(["git", "diff", "--name-only", "origin/main...dev"]).rstrip())

    sections.append("\n=== 5. git diff origin/main...dev (Python source files) ===")
    sections.append(
        run(
            [
                "git",
                "diff",
                "origin/main...dev",
                "--",
                "src/vexic/mcp_http.py",
                "src/vexic/recorders/cli.py",
                "src/vexic/recorders/hosted_ingest.py",
                "src/vexic/recorders/hosted_prime.py",
                "src/vexic/recorders/status.py",
            ]
        ).rstrip()
    )

    sections.append("\n=== 6. git diff origin/main...dev (version files) ===")
    sections.append(
        run(
            [
                "git",
                "diff",
                "origin/main...dev",
                "--",
                "pyproject.toml",
                "src/vexic/__init__.py",
                "tests/test_packaging_surface.py",
            ]
        ).rstrip()
    )

    sections.append("\n=== 7. gh pr view 253 ===")
    gh = run(
        [
            "gh",
            "pr",
            "view",
            "253",
            "--json",
            "title,body,baseRefName,headRefName,commits,files,state,mergeable,statusCheckRollup",
        ]
    ).rstrip()
    sections.append(gh)
    try:
        sections.append("\n=== 7b. gh pr view 253 (pretty) ===")
        sections.append(json.dumps(json.loads(gh.split("[exit")[0].strip()), indent=2))
    except Exception as exc:  # noqa: BLE001
        sections.append(f"(json parse failed: {exc})")

    OUT.write_text("\n".join(sections) + "\n", encoding="utf-8")
    print(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
