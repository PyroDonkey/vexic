#!/usr/bin/env python
"""PreToolUse hook: fail closed against the Vexic memory invariants.

The memory invariants in AGENTS.md are enforced in code and tests, but not at
the agent harness layer. This hook closes the harness-layer gap by denying a
narrow set of high-confidence, irreversible violations issued through the
project's `sqlite3` CLI (the only direct DB write path, gated by the
`Bash(sqlite3 *)` permission):

1. Invariant #1 - Tier 1 `messages` is append-only. Deny anything that mutates
   or removes existing transcript rows: `UPDATE messages`, `DELETE FROM
   messages`, `INSERT OR REPLACE`/`REPLACE INTO messages` (REPLACE deletes the
   prior row), and `INSERT ... ON CONFLICT ... DO UPDATE` upserts on messages.
   A plain `INSERT INTO messages` (appending a new row) stays allowed.
2. Host extension tables such as `background_tool_audit` are host-owned. Deny
   `DROP`/`ALTER`/`TRUNCATE` against it, and `CREATE [VIRTUAL|TEMP|TEMPORARY]
   TABLE background_tool_audit`.

Scope and conservatism. To avoid over-blocking, the hook only inspects commands
that actually invoke `sqlite3`; a command that merely *mentions* SQL text
(e.g. `rg "DELETE FROM messages"`, a `grep`, or a Python heredoc that prints
the string) is allowed, because it does not execute against the database. It
also allows non-Bash tools, read-only SQL such as `SELECT`, mutations of other
tables, SQL piped from a file (which it cannot read), and anything it cannot
confidently classify. Like the other hooks here, any internal error fails safe
(allow), never blocking the agent.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

# Tier 1 append-only table (Invariant #1).
APPEND_ONLY_TABLE = "messages"
# Host-owned extension table Vexic must not create/own/drop.
HOST_OWNED_TABLE = "background_tool_audit"

# A table reference: a bare identifier or a quoted/bracketed/schema-qualified
# form, e.g. messages, "messages", `messages`, [messages], main.messages.
_TABLE = (
    r"(?:[\"`\[]?\w+[\"`\]]?\s*\.\s*)?"  # optional schema/db qualifier
    r"[\"`\[]?{name}[\"`\]]?"
)


def _table_ref(name: str) -> str:
    return _TABLE.format(name=re.escape(name))


# UPDATE <messages> SET ...  (mutating Tier 1 rows)
_UPDATE_MESSAGES = re.compile(
    r"\bUPDATE\s+" + _table_ref(APPEND_ONLY_TABLE) + r"\b\s+SET\b",
    re.IGNORECASE,
)
# DELETE FROM <messages>  (deleting Tier 1 rows)
_DELETE_MESSAGES = re.compile(
    r"\bDELETE\s+FROM\s+" + _table_ref(APPEND_ONLY_TABLE) + r"\b",
    re.IGNORECASE,
)
# INSERT OR REPLACE INTO <messages> / REPLACE INTO <messages>
# (REPLACE deletes any conflicting existing row, violating append-only).
_REPLACE_MESSAGES = re.compile(
    r"\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+"
    + _table_ref(APPEND_ONLY_TABLE)
    + r"\b",
    re.IGNORECASE,
)
# INSERT ... INTO <messages> ... ON CONFLICT ... DO UPDATE
# (an upsert that mutates an existing transcript row).
_UPSERT_MESSAGES = re.compile(
    r"\bINSERT\b[\s\S]*?\bINTO\s+"
    + _table_ref(APPEND_ONLY_TABLE)
    + r"\b[\s\S]*?\bON\s+CONFLICT\b[\s\S]*?\bDO\s+UPDATE\b",
    re.IGNORECASE,
)
# DROP/ALTER/TRUNCATE ... <background_tool_audit>  (host-owned table)
_MUTATE_HOST_TABLE = re.compile(
    r"\b(?:DROP|ALTER|TRUNCATE)\s+TABLE\b[\s\S]*?"
    + _table_ref(HOST_OWNED_TABLE)
    + r"\b",
    re.IGNORECASE,
)
# CREATE [VIRTUAL|TEMP|TEMPORARY] TABLE [IF NOT EXISTS] ... <background_tool_audit>
_CREATE_HOST_TABLE = re.compile(
    r"\bCREATE\s+(?:VIRTUAL\s+|TEMP\s+|TEMPORARY\s+)?TABLE\b"
    r"(?:\s+IF\s+NOT\s+EXISTS)?[\s\S]*?"
    + _table_ref(HOST_OWNED_TABLE)
    + r"\b",
    re.IGNORECASE,
)

# (pattern, human-readable reason) checked in order.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        _UPDATE_MESSAGES,
        "Tier 1 `messages` is append-only (AGENTS.md Memory Invariant #1): "
        "never UPDATE transcript rows.",
    ),
    (
        _DELETE_MESSAGES,
        "Tier 1 `messages` is append-only (AGENTS.md Memory Invariant #1): "
        "never DELETE transcript rows.",
    ),
    (
        _REPLACE_MESSAGES,
        "Tier 1 `messages` is append-only (AGENTS.md Memory Invariant #1): "
        "INSERT OR REPLACE / REPLACE INTO deletes the existing transcript row.",
    ),
    (
        _UPSERT_MESSAGES,
        "Tier 1 `messages` is append-only (AGENTS.md Memory Invariant #1): "
        "an ON CONFLICT DO UPDATE upsert mutates an existing transcript row.",
    ),
    (
        _MUTATE_HOST_TABLE,
        "`background_tool_audit` is a host-owned extension table (AGENTS.md "
        "Architecture Boundaries): Vexic must not drop, alter, or truncate it.",
    ),
    (
        _CREATE_HOST_TABLE,
        "`background_tool_audit` is a host-owned extension table (AGENTS.md "
        "Architecture Boundaries): Vexic schema init must not create or take "
        "ownership of it.",
    ),
]


def _emit_deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


def _emit_allow(note: str | None = None) -> None:
    """Allow by emitting nothing (normal permission flow).

    A PreToolUse hook that wants to defer to normal flow emits no decision. We
    keep an optional note path only for the fail-safe error case, where we want
    a trace without blocking; the note is surfaced as additionalContext.
    """
    if note is None:
        return
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": note,
            }
        },
        sys.stdout,
    )


def _violation_reason(command: str) -> str | None:
    for pattern, reason in _RULES:
        if pattern.search(command):
            return reason
    return None


def _is_sqlite3_executable(token: str) -> bool:
    name = token.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in {"sqlite3", "sqlite3.exe"}


def _invokes_sqlite3(command: str) -> bool:
    try:
        if "|" in command:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="|")
            lexer.whitespace_split = True
            tokens = list(lexer)
        else:
            tokens = shlex.split(command)
    except ValueError:
        return False

    segment_start = True
    for token in tokens:
        if token == "|":
            segment_start = True
            continue
        if segment_start and _is_sqlite3_executable(token):
            return True
        segment_start = False
    return False


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    event = json.loads(raw)

    # Only Bash commands carry raw SQL we can confidently classify. Anything
    # else (Edit, Write, MCP tools, ...) is allowed through to normal flow.
    if event.get("tool_name") != "Bash":
        return 0

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return 0

    # Only classify commands that actually invoke sqlite3; a command that merely
    # mentions SQL text (rg/grep/echo/heredoc) is not a database write.
    if not _invokes_sqlite3(command):
        return 0

    reason = _violation_reason(command)
    if reason is not None:
        _emit_deny(
            reason
            + " Retire or supersede in place instead; this command was blocked "
            "by the write-target guardrail."
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail safe: allow, never block the agent
        _emit_allow(f"Write-target guardrail hook errored, allowing: {exc!r}")
        sys.exit(0)
