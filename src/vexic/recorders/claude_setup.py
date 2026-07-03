from __future__ import annotations

import contextlib
import json
import os
import shutil
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vexic.fs_permissions import ensure_owner_only

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


def _repo_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    if not (root / "scripts" / "vexic-mcp-stdio.py").is_file():
        raise RuntimeError("Claude Code setup must run from a Vexic source checkout")
    return root


def _uv_executable() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise FileNotFoundError("uv executable was not found on PATH")
    return executable


def _uv_run_editable_args(*tail: str, uv_executable: str | None = None) -> list[str]:
    return [uv_executable or _uv_executable(), "run", "--with-editable", str(_repo_root()), *tail]


def default_recorder_hook_command() -> str:
    return shlex.join(
        _bash_safe(part)
        for part in _uv_run_editable_args("python", "-m", "vexic.cli", "recorder", "ingest")
    )


def _ensure_owner_only(path: Path) -> None:
    try:
        ensure_owner_only(path)
    except PermissionError as exc:
        raise PermissionError(
            "recorder config must have owner-only permissions"
        ) from exc


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


def _recorder_config_arg(config_path: Path, home: Path) -> str:
    try:
        if home.resolve(strict=False) == Path.home().resolve(strict=False):
            relative = config_path.resolve(strict=False).relative_to(
                home.resolve(strict=False)
            )
            return f"~/{relative.as_posix()}"
    except ValueError:
        pass
    return str(config_path)


def _hook_command(command: str, config_path: Path) -> str:
    return f"{_bash_safe(command)} --config {shlex.quote(_bash_safe(str(config_path)))}"


def _prime_command(command: str) -> str:
    command = command.rstrip()
    suffix = " recorder ingest"
    if command.endswith(suffix):
        return command[: -len(suffix)] + " recorder prime"
    raise ValueError("prime_command is required when command does not end with recorder ingest")


def _mcp_stdio_launcher() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "vexic-mcp-stdio.py"


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    text = json.dumps(payload, sort_keys=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _write_mcp_config(
    project_root: Path,
    config_path: Path,
    home: Path,
    uv_executable: str,
    config: dict[str, Any] | None = None,
) -> Path:
    if not project_root.is_dir():
        raise ValueError("project_root must be an existing directory")
    mcp_path = project_root / ".mcp.json"
    next_config = dict(_load_json(mcp_path) if config is None else config)
    servers = next_config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    else:
        servers = dict(servers)
    next_config["mcpServers"] = servers
    servers["vexic"] = {
        "command": uv_executable,
        "args": [
            "run",
            "--with-editable",
            # Install the local-embed extra so search_long_term can embed queries.
            f"{_repo_root()}[local-embed]",
            "python",
            str(_mcp_stdio_launcher()),
            "--recorder-config",
            _recorder_config_arg(config_path, home),
        ],
    }
    _write_json_atomic(mcp_path, next_config)
    return mcp_path


def _remove_mcp_config(project_root: Path) -> bool:
    mcp_path = project_root / ".mcp.json"
    config = _load_json(mcp_path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or "vexic" not in servers:
        return False
    del servers["vexic"]
    _write_json_atomic(mcp_path, config)
    return True


def _set_mcpjson_disabled(settings: dict[str, Any], name: str) -> None:
    disabled = settings.get("disabledMcpjsonServers")
    disabled_names = disabled if isinstance(disabled, list) else []
    settings["disabledMcpjsonServers"] = [
        item for item in disabled_names if item != name
    ] + [name]

    enabled = settings.get("enabledMcpjsonServers")
    if isinstance(enabled, list):
        settings["enabledMcpjsonServers"] = [item for item in enabled if item != name]


def _remove_mcpjson_choice(settings: dict[str, Any], name: str) -> bool:
    changed = False
    for key in ("disabledMcpjsonServers", "enabledMcpjsonServers"):
        values = settings.get(key)
        if isinstance(values, list) and name in values:
            settings[key] = [item for item in values if item != name]
            changed = True
    return changed


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


def _restore_secret_config(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.write_bytes(previous)
    path.chmod(0o600)
    _ensure_owner_only(path)


def install_claude_code_setup(
    *,
    home: Path,
    base_url: str,
    api_key: str,
    project_id: str,
    session_id: str,
    agent_id: str | None,
    command: str,
    prime_command: str | None = None,
    project_root: Path | None = None,
) -> ClaudeCodeSetupResult:
    base_url = _require_nonblank("base_url", base_url)
    api_key = _require_nonblank("api_key", api_key)
    project_id = _require_nonblank("project_id", project_id)
    session_id = _require_nonblank("session_id", session_id)
    settings_path, config_path, status_path = _paths(home)
    hook_command = _hook_command(command, config_path)
    prime_hook_command = _hook_command(prime_command or _prime_command(command), config_path)
    uv_executable = _uv_executable() if project_root else ""
    if project_root and not project_root.is_dir():
        raise ValueError("project_root must be an existing directory")
    if project_root and not os.access(project_root, os.W_OK):
        raise PermissionError("project_root must be writable")

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
    session_start_groups, _changed = _without_vexic_hook(hooks.get("SessionStart"))
    session_start_groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": prime_hook_command,
                    "async": False,
                    "timeout": 30,
                    "vexicHookId": VEXIC_HOOK_ID,
                }
            ]
        }
    )
    hooks["SessionStart"] = session_start_groups
    if project_root:
        _set_mcpjson_disabled(settings, "vexic")
    mcp_path = project_root / ".mcp.json" if project_root else None
    mcp_config = _load_json(mcp_path) if mcp_path else None

    previous_config = config_path.read_bytes() if config_path.exists() else None
    previous_mcp = mcp_path.read_bytes() if mcp_path and mcp_path.exists() else None
    mcp_config_path = None
    try:
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
        mcp_config_path = (
            _write_mcp_config(project_root, config_path, home, uv_executable, mcp_config)
            if project_root
            else None
        )
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(settings_path, settings)
    except Exception:
        if mcp_config_path is not None:
            with contextlib.suppress(Exception):
                if previous_mcp is None:
                    mcp_config_path.unlink(missing_ok=True)
                else:
                    mcp_config_path.write_bytes(previous_mcp)
        _restore_secret_config(config_path, previous_config)
        raise

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
    choice_changed = _remove_mcpjson_choice(settings, "vexic") if project_root else False
    if not isinstance(hooks, dict):
        if choice_changed:
            _write_json_atomic(settings_path, settings)
        return mcp_changed or choice_changed
    changed = choice_changed
    for name in ("Stop", "SessionStart"):
        groups, hook_changed = _without_vexic_hook(hooks.get(name))
        if hook_changed:
            hooks[name] = groups
            changed = True
    if changed:
        _write_json_atomic(settings_path, settings)
    return changed or mcp_changed
