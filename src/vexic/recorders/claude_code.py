from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vexic.contract import PRIME_CONTEXT_HEADER, SourceTranscriptMessage
from vexic.recorders.transcript_cursor import TranscriptCursor, line_sha256
from vexic.storage import single_message_adapter

SOURCE_HOST = "claude-code"


def _content_text(content: object) -> str | None:
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if not isinstance(content, list):
        return None
    parts = [
        part["text"].strip()
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
        and part["text"].strip()
    ]
    return "\n".join(parts) or None


def source_message_from_claude_code_row(
    row: dict[str, Any],
) -> SourceTranscriptMessage | None:
    if row.get("type") not in {"user", "assistant"}:
        return None
    if row.get("isMeta") or row.get("isSidechain"):
        return None
    source_session_id = row.get("sessionId")
    source_message_id = row.get("uuid")
    message = row.get("message")
    if not (
        isinstance(source_session_id, str)
        and isinstance(source_message_id, str)
        and isinstance(message, dict)
    ):
        return None
    source_session_id = source_session_id.strip()
    source_message_id = source_message_id.strip()
    if not source_session_id or not source_message_id:
        return None

    text = _content_text(message.get("content"))
    if text is None:
        return None
    if PRIME_CONTEXT_HEADER in text:
        # Injected SessionStart priming recap, echoed back into the JSONL
        # transcript by the host; never re-ingest it into Tier 1 (WI-6).
        return None

    role = message.get("role")
    if row["type"] == "user" and role == "user":
        model_message = ModelRequest(parts=[UserPromptPart(content=text)])
    elif row["type"] == "assistant" and role == "assistant":
        model_message = ModelResponse(parts=[TextPart(content=text)])
    else:
        return None

    try:
        return SourceTranscriptMessage(
            source_host=SOURCE_HOST,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            message_json=single_message_adapter.dump_json(model_message).decode(),
        )
    except ValueError:
        return None


def _source_message_from_line(raw_line: bytes) -> SourceTranscriptMessage | None:
    """Parse one non-blank JSONL line into a message, or None for an ignored row."""
    try:
        row = json.loads(raw_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(row, dict):
        return None
    return source_message_from_claude_code_row(row)


def iter_claude_code_source_messages(
    paths: list[Path],
) -> Iterator[SourceTranscriptMessage | None]:
    for path in paths:
        with path.open("rb") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                yield _source_message_from_line(raw_line)


@dataclass(frozen=True)
class TranscriptScan:
    """Result of reading a transcript, optionally resumed from a cursor.

    `cursor` is the cursor the caller should persist *after* the scanned
    messages have been ingested, never before: a cursor written ahead of a
    failed POST would skip those rows on the next run.
    """

    messages: list[SourceTranscriptMessage]
    ignored: int
    cursor: TranscriptCursor | None
    resumed: bool


def scan_claude_code_transcript(
    path: Path,
    *,
    cursor: TranscriptCursor | None = None,
    source_session_id: str | None = None,
) -> TranscriptScan:
    """Read a Claude Code transcript, resuming from `cursor` when it still fits.

    A cursor that is missing, stale, or no longer matches the file (truncated,
    rotated, or rewritten) is discarded and the whole transcript is reread. The
    returned cursor only ever advances past newline-terminated lines, so a row
    that is still being written is re-read (and deduped by the ledger) instead
    of being skipped.
    """
    messages: list[SourceTranscriptMessage] = []
    ignored = 0
    with path.open("rb") as handle:
        start = _resume_offset(handle, cursor, source_session_id)
        resumed = start > 0
        last_line_offset = cursor.last_line_offset if resumed and cursor else 0
        last_line_sha256 = cursor.last_line_sha256 if resumed and cursor else ""
        committed = start
        offset = start
        handle.seek(start)
        while True:
            raw_line = handle.readline()
            if not raw_line:
                break
            complete = raw_line.endswith(b"\n")
            if raw_line.strip():
                message = _source_message_from_line(raw_line)
                if message is None:
                    ignored += 1
                else:
                    messages.append(message)
            if complete:
                last_line_offset = offset
                last_line_sha256 = line_sha256(raw_line)
                committed = offset + len(raw_line)
            offset += len(raw_line)

    next_cursor = (
        TranscriptCursor(
            source_session_id=source_session_id,
            byte_offset=committed,
            last_line_offset=last_line_offset,
            last_line_sha256=last_line_sha256,
        )
        if last_line_sha256
        else None
    )
    return TranscriptScan(
        messages=messages,
        ignored=ignored,
        cursor=next_cursor,
        resumed=resumed,
    )


def _resume_offset(
    handle: Any,
    cursor: TranscriptCursor | None,
    source_session_id: str | None,
) -> int:
    """Byte offset to resume from, or 0 when the cursor cannot be trusted."""
    if cursor is None or cursor.byte_offset <= 0:
        return 0
    if source_session_id is not None and cursor.source_session_id != source_session_id:
        # Same path, different Claude Code session: the file was replaced.
        return 0
    length = cursor.byte_offset - cursor.last_line_offset
    if length <= 0:
        return 0
    handle.seek(cursor.last_line_offset)
    raw_line = handle.read(length)
    if len(raw_line) != length or line_sha256(raw_line) != cursor.last_line_sha256:
        # The row the cursor stopped after is gone or no longer matches: the
        # transcript was truncated, rotated, or rewritten. Reread all of it.
        return 0
    return cursor.byte_offset
