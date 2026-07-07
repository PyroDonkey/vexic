"""CLI tests for the additive `setup codex` and `setup mcp-client` connect legs.

These share the credential-resolution (`--token` XOR manual flags) with
`setup claude-code` but perform the MCP-connect leg only (ADR 0027): they write
an owner-only creds file and print the vendor `mcp add` / launcher command. They
install no recorder hooks, no settings.json, and no `.mcp.json`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vexic.recorders.cli import main as recorder_main
from vexic.recorders.setup_exchange import SetupExchangeResult


def _assert_no_other_setup_files(test: unittest.TestCase, home: Path) -> None:
    """No hooks/settings/recorder/.mcp.json artifacts anywhere under home."""
    for name in (
        ".claude/settings.json",
        ".vexic/claude-code-recorder.json",
        ".mcp.json",
    ):
        test.assertFalse((home / name).exists(), f"unexpected file: {name}")
    for path in home.rglob("*"):
        test.assertNotEqual(path.name, ".mcp.json", f"unexpected .mcp.json: {path}")
        test.assertNotEqual(path.name, "settings.json", f"unexpected settings: {path}")


class SetupCodexTests(unittest.TestCase):
    def test_setup_codex_token_writes_owner_only_creds_and_prints_add(self) -> None:
        def fake_exchange(config, *, token):
            self.assertEqual(config.base_url, "https://api.example.test")
            self.assertEqual(token, "vxsetup_secret")
            return SetupExchangeResult(
                api_key="vx_exchanged",
                key_id="key-1",
                project_id="exchanged-project",
                session_id="exchanged-session",
                agent_id="exchanged-agent",
            )

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.cli.exchange_setup_token", fake_exchange),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    [
                        "setup-codex",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--token",
                        "vxsetup_secret",
                    ]
                )

            self.assertEqual(code, 0)
            creds_path = home / ".vexic" / "codex-mcp.json"
            self.assertTrue(creds_path.exists())

            probe = home / "probe"
            probe.write_text("", encoding="utf-8")
            probe.chmod(0o600)
            if stat.S_IMODE(probe.stat().st_mode) == 0o600:
                self.assertEqual(stat.S_IMODE(creds_path.stat().st_mode), 0o600)

            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(creds),
                {"base_url", "api_key", "project_id", "session_id", "agent_id"},
            )
            self.assertEqual(creds["api_key"], "vx_exchanged")

            out = json.loads(stdout.getvalue())
            self.assertTrue(out["ok"])
            self.assertEqual(out["creds_path"], str(creds_path))
            self.assertTrue(out["connect_command"].startswith("codex mcp add vexic -- "))
            self.assertIn(str(creds_path.name), out["connect_command"])
            # No raw key leaks into stdout or the printed command.
            self.assertNotIn("vx_exchanged", stdout.getvalue())
            self.assertNotIn("vx_exchanged", stderr.getvalue())
            self.assertIn("codex mcp add vexic", stderr.getvalue())

            _assert_no_other_setup_files(self, home)

    def test_setup_codex_manual_creds_path_works(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stdout = io.StringIO()
            with (
                patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = recorder_main(
                    [
                        "setup-codex",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_manual",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )

            self.assertEqual(code, 0)
            exchange_mock.assert_not_called()
            creds = json.loads(
                (home / ".vexic" / "codex-mcp.json").read_text(encoding="utf-8")
            )
            self.assertEqual(creds["api_key"], "vx_manual")
            self.assertEqual(creds["project_id"], "project-a")
            self.assertEqual(creds["session_id"], "session-a")
            self.assertIsNone(creds["agent_id"])
            self.assertNotIn("vx_manual", stdout.getvalue())
            _assert_no_other_setup_files(self, home)

    def test_setup_codex_token_and_manual_creds_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    [
                        "setup-codex",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--token",
                        "vxsetup_secret",
                        "--api-key",
                        "vx_manual",
                    ]
                )

            self.assertEqual(code, 2)
            self.assertIn("mutually exclusive", stderr.getvalue())
            self.assertNotIn("vx_manual", stderr.getvalue())
            self.assertNotIn("vxsetup_secret", stderr.getvalue())
            exchange_mock.assert_not_called()
            self.assertFalse((home / ".vexic" / "codex-mcp.json").exists())

    def test_setup_codex_via_top_level_cli_dispatch(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = vexic_main(
                    [
                        "setup",
                        "codex",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_manual",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue((home / ".vexic" / "codex-mcp.json").exists())


class SetupMcpClientTests(unittest.TestCase):
    def test_setup_mcp_client_writes_creds_and_prints_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    [
                        "setup-mcp-client",
                        "myagent",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_manual",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )

            self.assertEqual(code, 0)
            exchange_mock.assert_not_called()
            creds_path = home / ".vexic" / "myagent-mcp.json"
            self.assertTrue(creds_path.exists())
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            self.assertEqual(creds["api_key"], "vx_manual")

            out = json.loads(stdout.getvalue())
            self.assertTrue(out["ok"])
            self.assertEqual(out["creds_path"], str(creds_path))
            self.assertNotIn("vx_manual", stdout.getvalue())
            self.assertNotIn("vx_manual", stderr.getvalue())
            # Generic path prints launcher command + human instructions.
            self.assertIn("myagent-mcp.json", stderr.getvalue())
            self.assertIn(str(creds_path), stderr.getvalue())
            _assert_no_other_setup_files(self, home)

    def test_setup_mcp_client_rejects_unsafe_name(self) -> None:
        for bad in ("", "  ", "../evil", "a/b", "a\\b"):
            with tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                stderr = io.StringIO()
                with (
                    patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
                    contextlib.redirect_stderr(stderr),
                ):
                    code = recorder_main(
                        [
                            "setup-mcp-client",
                            bad,
                            "--home",
                            str(home),
                            "--base-url",
                            "https://api.example.test",
                            "--api-key",
                            "vx_manual",
                            "--project-id",
                            "project-a",
                            "--session-id",
                            "session-a",
                        ]
                    )
                self.assertEqual(code, 2, f"name {bad!r} should be rejected")
                exchange_mock.assert_not_called()
                self.assertFalse(any(home.rglob("*-mcp.json")))


class UninstallClientTests(unittest.TestCase):
    def test_uninstall_codex_deletes_creds_and_prints_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            creds_path = home / ".vexic" / "codex-mcp.json"
            creds_path.parent.mkdir(parents=True)
            creds_path.write_text("{}", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["uninstall-codex", "--home", str(home)]
                )

            self.assertEqual(code, 0)
            self.assertFalse(creds_path.exists())
            self.assertTrue(json.loads(stdout.getvalue())["removed"])
            self.assertIn("codex mcp remove vexic", stderr.getvalue())

    def test_uninstall_codex_missing_creds_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stdout = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = recorder_main(["uninstall-codex", "--home", str(home)])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(stdout.getvalue())["removed"])

    def test_uninstall_mcp_client_deletes_creds_and_prints_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            creds_path = home / ".vexic" / "myagent-mcp.json"
            creds_path.parent.mkdir(parents=True)
            creds_path.write_text("{}", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["uninstall-mcp-client", "myagent", "--home", str(home)]
                )

            self.assertEqual(code, 0)
            self.assertFalse(creds_path.exists())
            self.assertTrue(json.loads(stdout.getvalue())["removed"])
            self.assertIn("myagent mcp remove vexic", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
