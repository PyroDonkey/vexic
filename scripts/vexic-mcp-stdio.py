from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vexic.mcp_stdio import main
from vexic.hosted_mcp import create_hosted_http_memory_service, run_recorder_config_proxy


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--recorder-config"] and len(args) == 2:
        raise SystemExit(
            run_recorder_config_proxy(
                Path(args[1]),
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        )
    raise SystemExit(main(args, service_factory=create_hosted_http_memory_service))
