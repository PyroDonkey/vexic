from __future__ import annotations

import json
import os
import uuid
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
    # Async Stop hooks can overlap and write this file concurrently, so the
    # status lands via a temp file renamed onto the target (mirroring
    # write_cursor). os.replace is atomic on the same filesystem, so a reader
    # never sees a half-written file and a failed write leaves the prior status
    # intact instead of a truncated one.
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(status), sort_keys=True, indent=2) + "\n"
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
