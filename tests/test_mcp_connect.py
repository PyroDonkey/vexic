from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from vexic.recorders import mcp_connect

CREDS = dict(
    base_url="https://memory.example.com",
    api_key="vx_secret_key_value",
    project_id="proj-1",
    session_id="sess-1",
    agent_id="agent-1",
)


def test_launcher_argv_pip_mode_when_no_repo_root() -> None:
    argv = mcp_connect.build_launcher_argv("~/.vexic/codex-mcp.json", None)
    assert argv == [
        sys.executable,
        "-m",
        "vexic.mcp_stdio_main",
        "--recorder-config",
        "~/.vexic/codex-mcp.json",
    ]


def test_launcher_argv_source_checkout_mode(tmp_path: Path) -> None:
    repo_root = tmp_path / "vexic"
    (repo_root / "scripts").mkdir(parents=True)
    argv = mcp_connect.build_launcher_argv("~/.vexic/codex-mcp.json", repo_root)
    assert argv[1:5] == [
        "run",
        "--with-editable",
        f"{repo_root}[local-embed]",
        "python",
    ]
    assert argv[5] == str(repo_root / "scripts" / "vexic-mcp-stdio.py")
    assert argv[-2:] == ["--recorder-config", "~/.vexic/codex-mcp.json"]


def test_mcp_add_command_starts_with_client_and_names_creds_path() -> None:
    command = mcp_connect.build_mcp_add_command(
        "codex", "~/.vexic/codex-mcp.json", None
    )
    assert command.startswith("codex mcp add vexic -- ")
    assert "~/.vexic/codex-mcp.json" in command


def test_mcp_add_command_never_embeds_raw_key() -> None:
    # The command is derived only from the creds *path*, so no key can appear.
    command = mcp_connect.build_mcp_add_command(
        "codex", "~/.vexic/codex-mcp.json", None
    )
    assert "vx_" not in command
    assert CREDS["api_key"] not in command


def test_write_mcp_creds_file_is_owner_only_and_five_field_shape(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".vexic" / "codex-mcp.json"
    mcp_connect.write_mcp_creds_file(path, **CREDS)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == CREDS
    assert "status_path" not in payload


def test_write_mcp_creds_file_matches_recorder_proxy_config(tmp_path: Path) -> None:
    proxy = pytest.importorskip("vexic.hosted_mcp")
    path = tmp_path / ".vexic" / "codex-mcp.json"
    mcp_connect.write_mcp_creds_file(path, **CREDS)
    parsed = proxy._RecorderProxyConfig.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    assert parsed.api_key == CREDS["api_key"]
    assert parsed.agent_id == CREDS["agent_id"]


def test_install_codex_connect_writes_path_and_returns_command(
    tmp_path: Path,
) -> None:
    result = mcp_connect.install_codex_connect(home=tmp_path, **CREDS)
    assert result.creds_path == tmp_path / ".vexic" / "codex-mcp.json"
    assert result.creds_path.is_file()
    assert stat.S_IMODE(result.creds_path.stat().st_mode) == 0o600
    assert result.command.startswith("codex mcp add vexic -- ")
    assert "vx_" not in result.command


def test_install_generic_connect_writes_named_file_and_instructions(
    tmp_path: Path,
) -> None:
    result = mcp_connect.install_generic_connect(
        home=tmp_path, name="openclaw", **CREDS
    )
    assert result.creds_path == tmp_path / ".vexic" / "openclaw-mcp.json"
    assert result.creds_path.is_file()
    assert "vx_" not in result.command
    assert result.instructions is not None
    assert "MCP" in result.instructions
