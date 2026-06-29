from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecorderStatus:
    ok: bool
    operation: str
    source_session_id: str | None
    transcript_path: str | None
    inserted: int = 0
    skipped: int = 0
    rejected: int = 0
    ignored: int = 0
    error: str | None = None


def write_status(path: Path, status: RecorderStatus) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(status), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
