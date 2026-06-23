from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vexic.mcp_stdio import main
from vexic_hosted_mcp import create_hosted_http_memory_service


if __name__ == "__main__":
    raise SystemExit(main(service_factory=create_hosted_http_memory_service))
