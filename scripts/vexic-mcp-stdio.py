from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vexic.mcp_stdio import main
from vexic.hosted_mcp import create_hosted_http_memory_service, run_recorder_config_proxy


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--recorder-config"] and len(args) == 2:
        stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="")
        stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="")
        stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", newline="")
        try:
            raise SystemExit(
                run_recorder_config_proxy(
                    Path(args[1]),
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                )
            )
        finally:
            stdin.detach()
            stdout.detach()
            stderr.detach()
    raise SystemExit(main(args, service_factory=create_hosted_http_memory_service))
