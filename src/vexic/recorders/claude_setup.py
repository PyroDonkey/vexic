from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VEXIC_HOOK_ID = "vexic-claude-code-recorder"


@dataclass(frozen=True)
class ClaudeCodeSetupResult:
    settings_path: Path
    config_path: Path
    status_path: Path
    command: str


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _paths(home: Path) -> tuple[Path, Path, Path]:
    return (
        home / ".claude" / "settings.json",
        home / ".vexic" / "claude-code-recorder.json",
        home / ".vexic" / "claude-code-recorder-status.json",
    )


def _require_nonblank(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{name} must be nonblank")
    return value


def _chmod_owner_only(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _without_vexic_hook(stop_groups: Any) -> tuple[list[dict[str, Any]], bool]:
    groups = stop_groups if isinstance(stop_groups, list) else []
    changed = False
    kept_groups: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            kept_groups.append(group)
            continue
        kept_hooks = [
            hook
            for hook in hooks
            if not (isinstance(hook, dict) and hook.get("vexicHookId") == VEXIC_HOOK_ID)
        ]
        if len(kept_hooks) != len(hooks):
            changed = True
        if kept_hooks:
            next_group = dict(group)
            next_group["hooks"] = kept_hooks
            kept_groups.append(next_group)
    return kept_groups, changed


def install_claude_code_setup(
    *,
    home: Path,
    base_url: str,
    api_key: str,
    project_id: str,
    session_id: str,
    agent_id: str | None,
    command: str,
) -> ClaudeCodeSetupResult:
    api_key = _require_nonblank("api_key", api_key)
    project_id = _require_nonblank("project_id", project_id)
    session_id = _require_nonblank("session_id", session_id)
    settings_path, config_path, status_path = _paths(home)
    hook_command = f"{command} --config {subprocess.list2cmdline([str(config_path)])}"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.touch(mode=0o600, exist_ok=True)
    _chmod_owner_only(config_path)
    config_path.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "api_key": api_key,
                "project_id": project_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "status_path": str(status_path),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _chmod_owner_only(config_path)

    settings = _load_json(settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    stop_groups, _changed = _without_vexic_hook(hooks.get("Stop"))
    stop_groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command,
                    "async": True,
                    "timeout": 120,
                    "vexicHookId": VEXIC_HOOK_ID,
                }
            ]
        }
    )
    hooks["Stop"] = stop_groups
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, sort_keys=True), encoding="utf-8")

    return ClaudeCodeSetupResult(
        settings_path=settings_path,
        config_path=config_path,
        status_path=status_path,
        command=hook_command,
    )


def uninstall_claude_code_setup(*, home: Path) -> bool:
    settings_path, _config_path, _status_path = _paths(home)
    settings = _load_json(settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    stop_groups, changed = _without_vexic_hook(hooks.get("Stop"))
    if not changed:
        return False
    hooks["Stop"] = stop_groups
    settings_path.write_text(json.dumps(settings, sort_keys=True), encoding="utf-8")
    return True
