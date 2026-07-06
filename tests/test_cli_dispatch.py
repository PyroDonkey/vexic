from __future__ import annotations

import unittest
from unittest.mock import patch


class CliDispatchTests(unittest.TestCase):
    def test_mcp_stdio_subcommand_dispatches_to_stdio_main(self) -> None:
        from vexic.cli import main as vexic_main

        with patch("vexic.mcp_stdio.main", return_value=0) as stdio_main:
            code = vexic_main(
                [
                    "mcp-stdio",
                    "--db-path",
                    "memory.db",
                    "--tenant-id",
                    "local",
                    "--session-id",
                    "default",
                ]
            )

        self.assertEqual(code, 0)
        stdio_main.assert_called_once_with(
            [
                "--db-path",
                "memory.db",
                "--tenant-id",
                "local",
                "--session-id",
                "default",
            ]
        )

    def test_mcp_stdio_subcommand_propagates_argument_errors(self) -> None:
        # mcp_stdio.main requires --tenant-id and one of --db-path or
        # --api-base-url; argparse exits with SystemExit(2) when they are
        # missing. Patch main so the exit does not wrap pytest's captured
        # stdio streams.
        from vexic.cli import main as vexic_main

        with patch("vexic.mcp_stdio.main", side_effect=SystemExit(2)) as stdio_main:
            with self.assertRaises(SystemExit) as raised:
                vexic_main(["mcp-stdio"])

        self.assertEqual(raised.exception.code, 2)
        stdio_main.assert_called_once_with([])

    def test_unknown_command_prints_help_and_returns_2(self) -> None:
        from vexic.cli import main as vexic_main

        self.assertEqual(vexic_main(["bogus"]), 2)
        self.assertEqual(vexic_main([]), 2)


if __name__ == "__main__":
    unittest.main()
