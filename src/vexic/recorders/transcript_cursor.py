"""Recorder-local transcript cursor state.

The Stop hook re-reads the Claude Code JSONL transcript on every invocation.
A cursor lets a run resume where the previous run stopped instead of re-POSTing
the whole session. It is an optimization only: the hosted source ledger
(`source_transcript_ledger`) remains the duplicate guard, so a missing, corrupt,
stale, or rotated cursor must degrade to a full reread rather than to a wrong
ingest. Every read failure here therefore returns `None` (full reread) and every
write failure is non-fatal to the caller.

The Stop hook runs asynchronously, so two ingest runs over the same
transcript can overlap. `write_cursor` is monotonic: it skips the write when an
existing on-disk cursor already covers the new one's `byte_offset`, so a
late-finishing older run cannot regress a cursor a newer run already advanced.
The read-then-compare is not atomic against a concurrent writer, but that
residual race only ever costs a redundant reread -- the ledger, not this file,
is what keeps ingest correct.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, NonNegativeInt


class TranscriptCursor(BaseModel):
    """Where a previous recorder run stopped in one transcript file.

    `byte_offset` is the first unread byte. `prefix_sha256` fingerprints every
    consumed byte, while `last_line_offset`/`last_line_sha256` also pin the final
    consumed line. The prefix digest catches an earlier same-length rewrite even
    when the final line is untouched. It is required so pre-digest cursor files
    fail closed to a full reread. Offsets are validated as non-negative, so a
    hand-mangled cursor file cannot drive a nonsense seek.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_session_id: str | None = None
    byte_offset: NonNegativeInt
    prefix_sha256: str
    last_line_offset: NonNegativeInt
    last_line_sha256: str


def line_sha256(raw_line: bytes) -> str:
    return hashlib.sha256(raw_line).hexdigest()


def cursor_path(cursor_dir: Path, transcript_path: Path) -> Path:
    key = str(transcript_path.resolve(strict=False))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return cursor_dir / f"{digest}.json"


def read_cursor(cursor_dir: Path, transcript_path: Path) -> TranscriptCursor | None:
    """Load the cursor for one transcript, or None when it cannot be trusted."""
    try:
        raw = cursor_path(cursor_dir, transcript_path).read_bytes()
        return TranscriptCursor.model_validate_json(raw)
    except Exception:
        # Missing, unreadable, or corrupt cursor state: fall back to a full
        # reread. The ledger, not this file, is what keeps ingest correct.
        return None


def write_cursor(cursor_dir: Path, transcript_path: Path, cursor: TranscriptCursor) -> None:
    """Persist `cursor`, skipping the write if it would regress an existing one.

    An existing on-disk cursor with `byte_offset >= cursor.byte_offset` wins and
    the write is skipped; a missing or corrupt existing cursor never blocks the
    write. See the module docstring for why the resulting compare-then-replace
    race window is acceptable.
    """
    existing = read_cursor(cursor_dir, transcript_path)
    if existing is not None and existing.byte_offset >= cursor.byte_offset:
        return

    path = cursor_path(cursor_dir, transcript_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(cursor.model_dump(mode="json"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
