from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "recorder":
        from vexic.recorders.cli import main as recorder_main

        return recorder_main(args[1:])
    if len(args) >= 2 and args[:2] == ["setup", "claude-code"]:
        from vexic.recorders.cli import main as recorder_main

        return recorder_main(["setup-claude-code", *args[2:]])
    if len(args) >= 2 and args[:2] == ["setup", "codex"]:
        from vexic.recorders.cli import main as recorder_main

        return recorder_main(["setup-codex", *args[2:]])
    if len(args) >= 2 and args[:2] == ["setup", "mcp-client"]:
        from vexic.recorders.cli import main as recorder_main

        return recorder_main(["setup-mcp-client", *args[2:]])
    if args and args[0] == "operator":
        from vexic.operator_cli import main as operator_main

        return operator_main(args[1:])
    if args and args[0] == "mcp-stdio":
        from vexic.mcp_stdio import main as stdio_main

        return stdio_main(args[1:])

    parser = argparse.ArgumentParser(
        prog="vexic",
        description="Vexic command-line interface.",
        epilog=(
            "subcommands:\n"
            "  recorder            host recorder commands\n"
            "  setup claude-code   install the Claude Code recorder hooks\n"
            "  setup codex         print the opt-in `codex mcp add` connect command\n"
            "  setup mcp-client    print the opt-in connect command for a generic MCP client\n"
            "  mcp-stdio           run the read-only stdio MCP server\n"
            "  operator            operator memory audit and recovery tooling"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?")
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
