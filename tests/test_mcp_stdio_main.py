from __future__ import annotations

import io
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _fake_std_stream() -> types.SimpleNamespace:
    return types.SimpleNamespace(buffer=io.BytesIO())


class McpStdioMainDispatchTests(unittest.TestCase):
    def test_recorder_config_args_dispatch_to_recorder_config_proxy(self) -> None:
        from vexic import mcp_stdio_main

        with (
            patch("sys.stdin", _fake_std_stream()),
            patch("sys.stdout", _fake_std_stream()),
            patch("sys.stderr", _fake_std_stream()),
            patch(
                "vexic.mcp_stdio_main.run_recorder_config_proxy", return_value=7
            ) as proxy,
        ):
            code = mcp_stdio_main.main(["--recorder-config", "/tmp/recorder.json"])

        self.assertEqual(code, 7)
        proxy.assert_called_once()
        args, kwargs = proxy.call_args
        self.assertEqual(args, (Path("/tmp/recorder.json"),))
        self.assertEqual(set(kwargs), {"stdin", "stdout", "stderr"})

    def test_other_args_dispatch_to_mcp_stdio_main_with_hosted_factory(self) -> None:
        from vexic import mcp_stdio_main
        from vexic.hosted_mcp import create_hosted_http_memory_service

        with patch("vexic.mcp_stdio_main._mcp_stdio_main", return_value=3) as fallback:
            code = mcp_stdio_main.main(["--db-path", "memory.db"])

        self.assertEqual(code, 3)
        fallback.assert_called_once_with(
            ["--db-path", "memory.db"],
            service_factory=create_hosted_http_memory_service,
        )

    def test_module_is_runnable_with_python_dash_m(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root / "src"), env.get("PYTHONPATH", "")]
        )

        result = subprocess.run(
            [sys.executable, "-m", "vexic.mcp_stdio_main", "--bogus-flag"],
            input="",
            text=True,
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
        )

        # argparse rejects the bogus flag; the point is that the module
        # resolves and executes as `python -m vexic.mcp_stdio_main`.
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("No module named", result.stderr)


if __name__ == "__main__":
    unittest.main()
