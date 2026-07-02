#!/usr/bin/env python
"""Read-only branch drift check for optional local agent hooks.

Read-only: it fetches remote refs and reports drift, but never merges, resets,
or switches branches. Agents must still do the sync deliberately on `dev`.
"""

from __future__ import annotations

import json
import subprocess
import sys

FETCH_TIMEOUT_SECONDS = 20
DEFAULT_BRANCH = "main"
WORK_BRANCH = "dev"


def _git(*args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _comparison_error(
    left: str, right: str, result: subprocess.CompletedProcess[str]
) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    if not detail:
        detail = f"git rev-list exited {result.returncode}"
    return (
        f"Unable to compute drift for `{left}...{right}` "
        f"(missing ref/branch?): {detail}"
    )


def _left_right(
    left: str, right: str, notes: list[str] | None = None
) -> tuple[int, int] | None:
    result = _git("rev-list", "--left-right", "--count", f"{left}...{right}")
    if result.returncode != 0:
        if notes is not None:
            notes.append(_comparison_error(left, right, result))
        return None
    parts = result.stdout.split()
    if len(parts) != 2:
        if notes is not None:
            notes.append(_comparison_error(left, right, result))
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        if notes is not None:
            notes.append(_comparison_error(left, right, result))
        return None


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


def main() -> int:
    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return 0

    notes: list[str] = []
    fetch = _git("fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)
    if fetch.returncode != 0:
        notes.append(
            "`git fetch origin` failed; branch drift below may be stale."
        )

    warnings: list[str] = []
    comparisons_ok = 0

    dev_vs_main = _left_right(f"origin/{DEFAULT_BRANCH}", WORK_BRANCH, notes)
    if dev_vs_main is not None:
        comparisons_ok += 1
        behind, _ = dev_vs_main
        if behind > 0:
            warnings.append(
                f"`{WORK_BRANCH}` is BEHIND `origin/{DEFAULT_BRANCH}` by "
                f"{behind} commit(s). This is the expected state right after a "
                f"release merge; fast-forward `{WORK_BRANCH}` first:\n"
                f"    git switch {WORK_BRANCH} && git fetch origin && "
                f"git merge --ff-only origin/{DEFAULT_BRANCH} && "
                f"git push origin {WORK_BRANCH}\n"
                f"If --ff-only fails the branches have truly diverged: stop "
                f"and report per docs/branch-sync.md instead of merging."
            )

    dev_vs_origin_dev = _left_right(f"origin/{WORK_BRANCH}", WORK_BRANCH, notes)
    if dev_vs_origin_dev is not None:
        comparisons_ok += 1
        behind, _ = dev_vs_origin_dev
        if behind > 0:
            warnings.append(
                f"`{WORK_BRANCH}` is BEHIND `origin/{WORK_BRANCH}` by "
                f"{behind} commit(s). Pull before working:\n"
                f"    git pull --ff-only origin {WORK_BRANCH}"
            )

    local_main = _left_right(f"origin/{DEFAULT_BRANCH}", DEFAULT_BRANCH, notes)
    if local_main is not None:
        comparisons_ok += 1
        behind, _ = local_main
        if behind > 0:
            warnings.append(
                f"Local `{DEFAULT_BRANCH}` is behind `origin/{DEFAULT_BRANCH}` "
                f"by {behind} commit(s). Refresh when convenient:\n"
                f"    git fetch origin {DEFAULT_BRANCH}:{DEFAULT_BRANCH}"
            )

    if comparisons_ok == 3 and not warnings and not notes:
        _emit(
            f"Branch sync check: `{WORK_BRANCH}` is up to date with "
            f"`origin/{DEFAULT_BRANCH}` and `origin/{WORK_BRANCH}`.",
            None,
        )
        return 0

    body = "Branch sync check (read-only):\n\n" + "\n\n".join(notes + warnings)
    _emit(body, "Branch drift detected; see sync commands in context.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": f"Branch sync hook errored: {exc!r}",
                }
            },
            sys.stdout,
        )
        sys.exit(0)
