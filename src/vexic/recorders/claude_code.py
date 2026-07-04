from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vexic.contract import PRIME_CONTEXT_HEADER, SourceTranscriptMessage
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


def iter_claude_code_source_messages(
    paths: list[Path],
) -> Iterator[SourceTranscriptMessage | None]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    yield None
                    continue
                if not isinstance(row, dict):
                    yield None
                    continue
                message = source_message_from_claude_code_row(row)
                if message is None:
                    yield None
                else:
                    yield message
