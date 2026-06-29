from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from vexic.recorders.claude_code import iter_claude_code_source_messages
from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
from vexic.recorders.status import RecorderStatus, write_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vexic recorder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--hook-input", type=Path)
    ingest.add_argument("--base-url", required=True)
    ingest.add_argument("--api-key", required=True)
    ingest.add_argument("--project-id", required=True)
    ingest.add_argument("--session-id", required=True)
    ingest.add_argument("--agent-id")
    ingest.add_argument("--timeout-seconds", type=float, default=10.0)
    ingest.add_argument("--forbidden-value", action="append", default=[])
    ingest.add_argument("--status-path", type=Path)
    return parser


def _read_hook_payload(path: Path | None) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8") if path is not None else sys.stdin.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")
    return payload


def _ingest(args: argparse.Namespace) -> int:
    payload = _read_hook_payload(args.hook_input)
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        raise ValueError("hook input transcript_path must be a nonblank string")

    source_session_id = payload.get("session_id")
    if not isinstance(source_session_id, str):
        source_session_id = None

    messages = []
    ignored = 0
    for message in iter_claude_code_source_messages([Path(transcript_path)]):
        if message is None:
            ignored += 1
        else:
            messages.append(message)

    result = post_source_messages(
        HostedIngestConfig(
            base_url=args.base_url,
            api_key=args.api_key,
            project_id=args.project_id,
            session_id=args.session_id,
            agent_id=args.agent_id,
            timeout_seconds=args.timeout_seconds,
        ),
        messages=messages,
        forbidden_values=tuple(args.forbidden_value),
    )
    items = result.get("items")
    if not isinstance(items, list):
        items = []
    inserted = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "inserted")
    skipped = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "skipped")
    rejected = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "rejected")

    status = RecorderStatus(
        ok=True,
        operation="ingest",
        source_session_id=source_session_id,
        transcript_path=transcript_path,
        inserted=inserted,
        skipped=skipped,
        rejected=rejected,
        ignored=ignored,
    )
    if args.status_path is not None:
        write_status(args.status_path, status)
    print(
        json.dumps(
            {
                "ok": True,
                "inserted": inserted,
                "skipped": skipped,
                "rejected": rejected,
                "ignored": ignored,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ingest":
            return _ingest(args)
        raise ValueError(f"unknown command: {args.command}")
    except Exception as exc:
        if getattr(args, "status_path", None) is not None:
            write_status(
                args.status_path,
                RecorderStatus(
                    ok=False,
                    operation=args.command,
                    source_session_id=None,
                    transcript_path=None,
                    error=str(exc),
                ),
            )
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
