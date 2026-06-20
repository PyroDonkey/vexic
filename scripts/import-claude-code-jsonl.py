from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vexic.contract import (
    IngestSourceTranscriptRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    SourceTranscriptMessage,
    TrustBoundary,
)
from vexic.service import LocalMemoryService
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


def _source_message(row: dict[str, Any]) -> SourceTranscriptMessage | None:
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

    text = _content_text(message.get("content"))
    if text is None:
        return None

    role = message.get("role")
    if row["type"] == "user" and role == "user":
        model_message = ModelRequest(parts=[UserPromptPart(content=text)])
    elif row["type"] == "assistant" and role == "assistant":
        model_message = ModelResponse(parts=[TextPart(content=text)])
    else:
        return None

    return SourceTranscriptMessage(
        source_host=SOURCE_HOST,
        source_session_id=source_session_id,
        source_message_id=source_message_id,
        message_json=single_message_adapter.dump_json(model_message).decode(),
    )


def _read_messages(paths: list[Path]) -> tuple[list[SourceTranscriptMessage], int]:
    messages: list[SourceTranscriptMessage] = []
    ignored = 0
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    ignored += 1
                    continue
                if not isinstance(row, dict):
                    ignored += 1
                    continue
                message = _source_message(row)
                if message is None:
                    ignored += 1
                else:
                    messages.append(message)
    return messages, ignored


def _scope(args: argparse.Namespace) -> MemoryScope:
    return MemoryScope(
        tenant_id=args.tenant_id,
        project_id=args.project_id,
        user_id=args.user_id,
        session_id=args.session_id,
        principal=Principal(
            principal_id=args.principal_id,
            principal_type=PrincipalType.SERVICE,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities={MemoryCapability.WRITE},
    )


async def _run(args: argparse.Namespace) -> dict[str, int]:
    service = LocalMemoryService(
        db_path=args.db_path,
        tenant_id=args.tenant_id,
        forbidden_secret_values=tuple(args.forbidden_value),
    )
    service.init_schema()
    messages, ignored = _read_messages(args.jsonl_path)
    result = await service.ingest_source_transcript(
        IngestSourceTranscriptRequest(
            scope=_scope(args),
            messages=messages,
            redaction=RedactionContext(forbidden_values=tuple(args.forbidden_value)),
        )
    )
    counts = {"inserted": 0, "skipped": 0, "rejected": 0, "ignored": ignored}
    for item in result.items:
        counts[item.status] += 1
    return counts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Claude Code JSONL into Vexic.")
    parser.add_argument("jsonl_path", nargs="+", type=Path)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--session-id", default="default")
    parser.add_argument("--project-id")
    parser.add_argument("--user-id")
    parser.add_argument("--principal-id", default="vexic-claude-code-importer")
    parser.add_argument("--forbidden-value", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        summary = asyncio.run(_run(_parse_args(argv or sys.argv[1:])))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
