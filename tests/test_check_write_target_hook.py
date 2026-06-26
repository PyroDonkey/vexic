from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_write_target", ROOT / ".claude" / "hooks" / "check_write_target.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: dict[str, object],
) -> dict[str, object]:
    hook = _load_hook()
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(event)))
    assert hook.main() == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else {}


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _decision(payload: dict[str, object]) -> str | None:
    if not payload:
        return None
    return payload["hookSpecificOutput"].get("permissionDecision")  # type: ignore[index]


DENY_COMMANDS = [
    "sqlite3 memory.db 'DELETE FROM messages WHERE id=1'",
    "/usr/bin/sqlite3 memory.db 'DELETE FROM messages WHERE id=1'",
    "sqlite3 memory.db \"UPDATE   messages SET body='x'\"",
    "sqlite3 memory.db 'delete from MESSAGES'",
    "sqlite3 memory.db 'DROP TABLE background_tool_audit'",
    "sqlite3 memory.db 'ALTER TABLE background_tool_audit ADD COLUMN c'",
    "sqlite3 memory.db 'CREATE TABLE IF NOT EXISTS background_tool_audit (id)'",
    'sqlite3 memory.db \'DELETE FROM main."messages"\'',
    # INSERT OR REPLACE / REPLACE INTO messages (deletes existing row).
    "sqlite3 memory.db 'INSERT OR REPLACE INTO messages VALUES (1)'",
    "sqlite3 memory.db 'REPLACE INTO messages VALUES (1)'",
    # ON CONFLICT DO UPDATE upsert on messages.
    "sqlite3 memory.db 'INSERT INTO messages(id) VALUES(1) "
    "ON CONFLICT(id) DO UPDATE SET id=2'",
    # Host-owned table via virtual/temp/temporary CREATE.
    "sqlite3 memory.db 'CREATE VIRTUAL TABLE background_tool_audit USING fts5(x)'",
    "sqlite3 memory.db 'CREATE TEMP TABLE background_tool_audit (id)'",
    "sqlite3 memory.db 'CREATE TEMPORARY TABLE background_tool_audit (id)'",
    "cat setup.sql | sqlite3 memory.db 'DELETE FROM messages WHERE id=1'",
    "cat setup.sql|sqlite3 memory.db 'DELETE FROM messages WHERE id=1'",
]

ALLOW_COMMANDS = [
    "git status",
    "sqlite3 memory.db 'SELECT * FROM messages LIMIT 5'",
    "sqlite3 memory.db 'DELETE FROM memory_candidates WHERE id=2'",
    # Appending a new transcript row is allowed.
    "sqlite3 memory.db 'INSERT INTO messages (body) VALUES (1)'",
    # Commands that only MENTION SQL text but do not invoke sqlite3 (P2a).
    'rg -n "DELETE FROM messages" .',
    'grep "UPDATE messages SET" notes.txt',
    "python -c \"print('DELETE FROM messages')\"",
    "echo 'DROP TABLE background_tool_audit'",
    "echo \"sqlite3 memory.db 'DELETE FROM messages'\"",
]


@pytest.mark.parametrize("command", DENY_COMMANDS)
def test_denies_invariant_violations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    payload = _run(monkeypatch, capsys, _bash(command))
    assert _decision(payload) == "deny"
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]  # type: ignore[index]
    assert reason


@pytest.mark.parametrize("command", ALLOW_COMMANDS)
def test_allows_safe_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    payload = _run(monkeypatch, capsys, _bash(command))
    assert _decision(payload) is None


def test_ignores_non_bash_tools(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "f", "old_string": "DELETE FROM messages"},
    }
    payload = _run(monkeypatch, capsys, event)
    assert _decision(payload) is None


def test_empty_or_malformed_input_allows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(monkeypatch, capsys, {}) == {}
    assert _run(monkeypatch, capsys, {"tool_name": "Bash"}) == {}
    assert (
        _run(monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": ""}})
        == {}
    )
