import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_next_env_tracks_next_route_types() -> None:
    text = (ROOT / "console" / "next-env.d.ts").read_text(encoding="utf-8")

    assert re.search(r"import\s+[\"']\./\.next/types/routes\.d\.ts[\"'];", text)
