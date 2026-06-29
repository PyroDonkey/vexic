from __future__ import annotations

import json
import os
import shlex
import stat
import uuid
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
    mcp_config_path: Path | None = None


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
    return value.strip()


def _bash_safe(value: str) -> str:
    return value.replace("\\", "/")


def _ensure_owner_only(path: Path) -> None:
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError("recorder config must have owner-only permissions")


def _write_secret_json(path: Path, payload: dict[str, object]) -> None:
    text = json.dumps(payload, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    replaced = False
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as config:
            config.write(text)
        temp_path.chmod(0o600)
        _ensure_owner_only(temp_path)
        os.replace(temp_path, path)
        replaced = True
        path.chmod(0o600)
        _ensure_owner_only(path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        if replaced:
            path.unlink(missing_ok=True)
        raise PermissionError(
            "recorder config owner-only permissions could not be enforced"
        ) from exc
    except Exception:
        temp_path.unlink(missing_ok=True)
        if replaced:
            path.unlink(missing_ok=True)
        raise


def _write_mcp_config(project_root: Path, config_path: Path) -> Path:
    mcp_path = project_root / ".mcp.json"
    config = _load_json(mcp_path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers
    servers["vexic"] = {
        "command": "uv",
        "args": [
            "run",
            "python",
            "scripts/vexic-mcp-stdio.py",
            "--recorder-config",
            str(config_path),
        ],
    }
    mcp_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    return mcp_path


def _remove_mcp_config(project_root: Path) -> bool:
    mcp_path = project_root / ".mcp.json"
    config = _load_json(mcp_path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or "vexic" not in servers:
        return False
    del servers["vexic"]
    mcp_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    return True


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
    project_root: Path | None = None,
) -> ClaudeCodeSetupResult:
    base_url = _require_nonblank("base_url", base_url)
    api_key = _require_nonblank("api_key", api_key)
    project_id = _require_nonblank("project_id", project_id)
    session_id = _require_nonblank("session_id", session_id)
    settings_path, config_path, status_path = _paths(home)
    hook_command = f"{_bash_safe(command)} --config {shlex.quote(_bash_safe(str(config_path)))}"

    _write_secret_json(
        config_path,
        {
            "base_url": base_url,
            "api_key": api_key,
            "project_id": project_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "status_path": str(status_path),
        },
    )

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
                    "async": False,
                    "timeout": 120,
                    "vexicHookId": VEXIC_HOOK_ID,
                }
            ]
        }
    )
    hooks["Stop"] = stop_groups
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, sort_keys=True), encoding="utf-8")
    mcp_config_path = _write_mcp_config(project_root, config_path) if project_root else None

    return ClaudeCodeSetupResult(
        settings_path=settings_path,
        config_path=config_path,
        status_path=status_path,
        command=hook_command,
        mcp_config_path=mcp_config_path,
    )


def uninstall_claude_code_setup(*, home: Path, project_root: Path | None = None) -> bool:
    settings_path, _config_path, _status_path = _paths(home)
    settings = _load_json(settings_path)
    hooks = settings.get("hooks")
    mcp_changed = _remove_mcp_config(project_root) if project_root else False
    if not isinstance(hooks, dict):
        return mcp_changed
    stop_groups, changed = _without_vexic_hook(hooks.get("Stop"))
    if changed:
        hooks["Stop"] = stop_groups
        settings_path.write_text(json.dumps(settings, sort_keys=True), encoding="utf-8")
    return changed or mcp_changed
