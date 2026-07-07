"""Client-agnostic MCP connect leg for `vexic setup` (ADR 0027).

Setup writes an owner-only credential file of the same
``base_url``/``api_key``/``project_id``/``session_id``/``agent_id?`` shape the
stdio proxy already reads (``vexic.hosted_mcp._RecorderProxyConfig``) and then
*prints* the client's own ``mcp add`` command. It never runs the command, never
mutates client config, and never embeds a raw key: the printed command names
only the local stdio launcher plus the *path* to the credential file. The user
running that command is the deliberate, per-client opt-in.

The launcher/creds helpers here are intentionally copied from
``claude_setup`` so this module stays self-contained; a later increment inverts
the dependency.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from vexic.fs_permissions import ensure_owner_only


@dataclass(frozen=True)
class McpConnectResult:
    """Outcome of a connect install: where creds landed and what to print."""

    creds_path: Path
    command: str
    instructions: str | None = None


def _bash_safe(value: str) -> str:
    return value.replace("\\", "/")


def _repo_root() -> Path | None:
    """Return the Vexic source checkout root, or None when running from an install."""
    root = Path(__file__).resolve().parents[3]
    if not (root / "scripts" / "vexic-mcp-stdio.py").is_file():
        return None
    return root


def _uv_executable() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise FileNotFoundError("uv executable was not found on PATH")
    return executable


def _mcp_stdio_launcher(repo_root: Path) -> Path:
    return repo_root / "scripts" / "vexic-mcp-stdio.py"


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


def _ensure_owner_only(path: Path) -> None:
    try:
        ensure_owner_only(path)
    except PermissionError as exc:
        raise PermissionError("mcp creds must have owner-only permissions") from exc


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
            "mcp creds owner-only permissions could not be enforced"
        ) from exc
    except Exception:
        temp_path.unlink(missing_ok=True)
        if replaced:
            path.unlink(missing_ok=True)
        raise


def build_launcher_argv(recorder_config_arg: str, repo_root: Path | None) -> list[str]:
    """Return the local stdio launcher argv (pip install vs source checkout)."""
    if repo_root is None:
        return [
            sys.executable,
            "-m",
            "vexic.mcp_stdio_main",
            "--recorder-config",
            recorder_config_arg,
        ]
    return [
        _uv_executable(),
        "run",
        "--with-editable",
        # Install the local-embed extra so search_long_term can embed queries.
        f"{repo_root}[local-embed]",
        "python",
        str(_mcp_stdio_launcher(repo_root)),
        "--recorder-config",
        recorder_config_arg,
    ]


def build_mcp_add_command(
    client_binary: str, recorder_config_arg: str, repo_root: Path | None
) -> str:
    """Return the printed ``<client> mcp add vexic -- <launcher argv>`` command.

    Derived only from the creds *path*, so no raw key can appear.
    """
    argv = build_launcher_argv(recorder_config_arg, repo_root)
    launcher = shlex.join(_bash_safe(part) for part in argv)
    return f"{client_binary} mcp add vexic -- {launcher}"


def write_mcp_creds_file(
    path: Path,
    *,
    base_url: str,
    api_key: str,
    project_id: str,
    session_id: str,
    agent_id: str | None,
) -> None:
    """Owner-only atomic write of the 5-field ``_RecorderProxyConfig`` shape."""
    _write_secret_json(
        path,
        {
            "base_url": base_url,
            "api_key": api_key,
            "project_id": project_id,
            "session_id": session_id,
            "agent_id": agent_id,
        },
    )


def install_codex_connect(
    *,
    home: Path,
    base_url: str,
    api_key: str,
    project_id: str,
    session_id: str,
    agent_id: str | None,
) -> McpConnectResult:
    """Write ``~/.vexic/codex-mcp.json`` and return the ``codex mcp add`` command."""
    creds_path = home / ".vexic" / "codex-mcp.json"
    write_mcp_creds_file(
        creds_path,
        base_url=base_url,
        api_key=api_key,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
    )
    command = build_mcp_add_command(
        "codex", _recorder_config_arg(creds_path, home), _repo_root()
    )
    return McpConnectResult(creds_path=creds_path, command=command)


def install_generic_connect(
    *,
    home: Path,
    name: str,
    base_url: str,
    api_key: str,
    project_id: str,
    session_id: str,
    agent_id: str | None,
) -> McpConnectResult:
    """Write ``~/.vexic/<name>-mcp.json`` and return the launcher command.

    Clients without a dedicated installer get the raw launcher argv plus an
    instruction to add it as a stdio server to their own MCP config.
    """
    creds_path = home / ".vexic" / f"{name}-mcp.json"
    write_mcp_creds_file(
        creds_path,
        base_url=base_url,
        api_key=api_key,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
    )
    argv = build_launcher_argv(_recorder_config_arg(creds_path, home), _repo_root())
    command = shlex.join(_bash_safe(part) for part in argv)
    instructions = (
        "Add this stdio server to your MCP client's config: register the command "
        f"above as an MCP server named 'vexic'. Its credentials are read from "
        f"{creds_path}."
    )
    return McpConnectResult(
        creds_path=creds_path, command=command, instructions=instructions
    )
