"""Stdio MCP entry point runnable as ``python -m vexic.mcp_stdio_main``.

This is the installable equivalent of ``scripts/vexic-mcp-stdio.py``: with
exactly ``--recorder-config <path>`` it proxies stdio JSON-RPC to the hosted
MCP endpoint described by the recorder config; any other arguments fall
through to the local stdio MCP server with the hosted HTTP service factory.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from vexic.hosted_mcp import create_hosted_http_memory_service, run_recorder_config_proxy
from vexic.mcp_stdio import main as _mcp_stdio_main


def main(argv: list[str]) -> int:
    if argv[:1] == ["--recorder-config"] and len(argv) == 2:
        stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="")
        stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="")
        stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", newline="")
        try:
            return run_recorder_config_proxy(
                Path(argv[1]),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            stdin.detach()
            stdout.detach()
            stderr.detach()
    return _mcp_stdio_main(argv, service_factory=create_hosted_http_memory_service)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
