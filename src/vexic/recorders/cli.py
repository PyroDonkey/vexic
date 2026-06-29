from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from vexic.contract import SourceTranscriptMessage
from vexic.hosted import HOSTED_WRITE_MAX_CHARS, HOSTED_WRITE_MAX_MESSAGES
from vexic.recorders.claude_code import iter_claude_code_source_messages
from vexic.recorders.claude_setup import (
    install_claude_code_setup,
    uninstall_claude_code_setup,
)
from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
from vexic.recorders.status import RecorderStatus, write_status


class MissingIngestOption(ValueError):
    pass


class _RecorderIngestConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_key: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    status_path: Path | None = None


class _ClaudeHookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transcript_path: str
    session_id: str | None = None

    @field_validator("transcript_path")
    @classmethod
    def _transcript_path_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("hook input transcript_path must be a nonblank string")
        return value


def _iter_hosted_message_batches(
    messages: list[SourceTranscriptMessage],
) -> Iterator[list[SourceTranscriptMessage]]:
    if not messages:
        yield []
        return

    batch: list[SourceTranscriptMessage] = []
    batch_chars = 0
    for message in messages:
        message_chars = len(message.message_json)
        if message_chars > HOSTED_WRITE_MAX_CHARS:
            raise ValueError(
                f"source message {message.source_message_id} exceeds hosted ingest payload cap"
            )
        if batch and (
            len(batch) >= HOSTED_WRITE_MAX_MESSAGES
            or batch_chars + message_chars > HOSTED_WRITE_MAX_CHARS
        ):
            yield batch
            batch = []
            batch_chars = 0
        batch.append(message)
        batch_chars += message_chars

    if batch:
        yield batch


def _argv_status_path(argv: list[str]) -> Path | None:
    for index, value in enumerate(argv):
        if value == "--status-path" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if value.startswith("--status-path="):
            return Path(value.split("=", 1)[1])
    return None


def _try_write_status(path: Path | None, status: RecorderStatus) -> str | None:
    if path is None:
        return None
    try:
        write_status(path, status)
    except Exception as exc:
        return f"status write failed: {type(exc).__name__}"
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vexic recorder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--hook-input", type=Path)
    ingest.add_argument("--config", type=Path)
    ingest.add_argument("--base-url")
    ingest.add_argument("--api-key")
    ingest.add_argument("--project-id")
    ingest.add_argument("--session-id")
    ingest.add_argument("--agent-id")
    ingest.add_argument("--timeout-seconds", type=float, default=10.0)
    ingest.add_argument("--forbidden-value", action="append", default=[])
    ingest.add_argument("--status-path", type=Path)

    setup = subparsers.add_parser("setup-claude-code")
    setup.add_argument("--home", type=Path, default=Path.home())
    setup.add_argument("--base-url", required=True)
    setup.add_argument("--api-key", required=True)
    setup.add_argument("--project-id", required=True)
    setup.add_argument("--session-id", required=True)
    setup.add_argument("--agent-id")
    setup.add_argument(
        "--hook-command",
        dest="hook_command",
        default=subprocess.list2cmdline(
            [sys.executable, "-m", "vexic.cli", "recorder", "ingest"]
        ),
    )

    uninstall = subparsers.add_parser("uninstall-claude-code")
    uninstall.add_argument("--home", type=Path, default=Path.home())
    return parser


def _load_config(path: Path) -> _RecorderIngestConfigFile:
    try:
        return _RecorderIngestConfigFile.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ValueError(f"invalid recorder config: {exc}") from exc


def _apply_ingest_config(args: argparse.Namespace) -> None:
    if args.config is not None:
        config = _load_config(args.config)
        provided = config.model_fields_set
        for name in ("base_url", "api_key", "project_id", "session_id", "agent_id"):
            if name in provided:
                setattr(args, name, getattr(config, name))
        if "status_path" in provided and config.status_path is not None:
            args.status_path = config.status_path

    missing = [
        option
        for option, value in (
            ("--base-url", args.base_url),
            ("--api-key", args.api_key),
            ("--project-id", args.project_id),
            ("--session-id", args.session_id),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise MissingIngestOption(f"missing required ingest option: {missing[0]}")


def _read_hook_payload(path: Path | None) -> _ClaudeHookPayload:
    raw = path.read_text(encoding="utf-8") if path is not None else sys.stdin.read()
    try:
        return _ClaudeHookPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid hook input: {exc}") from exc


def _ingest(args: argparse.Namespace) -> int:
    payload = _read_hook_payload(args.hook_input)
    transcript_path = payload.transcript_path
    source_session_id = payload.session_id

    messages = []
    ignored = 0
    for message in iter_claude_code_source_messages([Path(transcript_path)]):
        if message is None:
            ignored += 1
        else:
            messages.append(message)

    config = HostedIngestConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        project_id=args.project_id,
        session_id=args.session_id,
        agent_id=args.agent_id,
        timeout_seconds=args.timeout_seconds,
    )
    items = []
    batches = list(_iter_hosted_message_batches(messages))
    for batch in batches:
        result = post_source_messages(
            config,
            messages=batch,
            forbidden_values=tuple(args.forbidden_value),
        )
        batch_items = result.get("items")
        if isinstance(batch_items, list):
            items.extend(batch_items)
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
    error = _try_write_status(args.status_path, status)
    if error is not None:
        raise RuntimeError(error)
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


def _setup_claude_code(args: argparse.Namespace) -> int:
    result = install_claude_code_setup(
        home=args.home,
        base_url=args.base_url,
        api_key=args.api_key,
        project_id=args.project_id,
        session_id=args.session_id,
        agent_id=args.agent_id,
        command=args.hook_command,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "settings_path": str(result.settings_path),
                "config_path": str(result.config_path),
                "status_path": str(result.status_path),
                "hook_command": result.command,
            },
            sort_keys=True,
        )
    )
    return 0


def _uninstall_claude_code(args: argparse.Namespace) -> int:
    removed = uninstall_claude_code_setup(home=args.home)
    print(json.dumps({"ok": True, "removed": removed}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    parser = _parser()
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        if _argv_status_path(raw_argv) is not None:
            _try_write_status(
                _argv_status_path(raw_argv),
                RecorderStatus(
                    ok=False,
                    operation="ingest",
                    source_session_id=None,
                    transcript_path=None,
                    error="argument parsing failed",
                ),
            )
        return exc.code if isinstance(exc.code, int) else 2

    try:
        if args.command == "ingest":
            _apply_ingest_config(args)
            return _ingest(args)
        if args.command == "setup-claude-code":
            return _setup_claude_code(args)
        if args.command == "uninstall-claude-code":
            return _uninstall_claude_code(args)
        raise ValueError(f"unknown command: {args.command}")
    except Exception as exc:
        _try_write_status(
            getattr(args, "status_path", None),
            RecorderStatus(
                ok=False,
                operation=args.command,
                source_session_id=None,
                transcript_path=None,
                error="argument parsing failed" if isinstance(exc, MissingIngestOption) else str(exc),
            ),
        )
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
