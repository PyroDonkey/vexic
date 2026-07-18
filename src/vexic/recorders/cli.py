from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from vexic.contract import (
    IngestSourceTranscriptResult,
    SourceTranscriptIngestItemResult,
    SourceTranscriptMessage,
)
from vexic.hosted import HOSTED_WRITE_MAX_CHARS, HOSTED_WRITE_MAX_MESSAGES
from vexic.recorders.claude_code import scan_claude_code_transcript
from vexic.recorders.claude_setup import (
    default_recorder_hook_command,
    install_claude_code_setup,
    uninstall_claude_code_setup,
)
from vexic.recorders.hosted_prime import (
    DEFAULT_PRIME_MAX_CHARS,
    PRIME_DEADLINE_SECONDS,
    HostedPrimeConfig,
    fetch_prime_context,
    post_trigger_dream_phase,
)
from vexic.recorders.hosted_ingest import (
    HostedIngestConfig,
    HostedIngestTransportError,
    post_source_messages,
)
from vexic.recorders.mcp_connect import install_codex_connect, install_generic_connect
from vexic.recorders.setup_exchange import SetupExchangeConfig, exchange_setup_token
from vexic.recorders.status import RecorderStatus, write_status
from vexic.recorders.transcript_cursor import (
    TranscriptCursor,
    read_cursor,
    write_cursor,
)

# Client names become the `~/.vexic/<name>-mcp.json` creds filename, so keep
# them to a safe single filename component (no path separators or traversal).
_CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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


class _ClaudeSessionStartHookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: str | None = None


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


def _source_key(
    item: SourceTranscriptMessage | SourceTranscriptIngestItemResult,
) -> tuple[str, str, str]:
    return (
        item.source_host,
        item.source_session_id,
        item.source_message_id,
    )


def _validated_ingest_items(
    response: object,
    messages: list[SourceTranscriptMessage],
) -> list[SourceTranscriptIngestItemResult]:
    """Validate one hosted result before it can authorize cursor advancement."""
    try:
        result = IngestSourceTranscriptResult.model_validate(response)
    except ValidationError as exc:
        raise RuntimeError("hosted ingest returned an invalid response") from exc

    expected = [_source_key(message) for message in messages]
    actual = [_source_key(item) for item in result.items]
    if Counter(actual) != Counter(expected):
        raise RuntimeError(
            "hosted ingest response did not match the submitted source messages"
        )
    return result.items


def _argv_status_path(argv: list[str]) -> Path | None:
    for index, value in enumerate(argv):
        if value == "--status-path" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if value.startswith("--status-path="):
            return Path(value.split("=", 1)[1])
    return None


# The Claude Code SessionStart hook installed by claude_setup kills prime at
# this many seconds; a fetch deadline at or beyond it silently recreates the
# lost-block failure the deadline exists to prevent.
_SESSION_START_HOOK_KILL_SECONDS = 30.0


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not (parsed > 0 and math.isfinite(parsed)):
        raise argparse.ArgumentTypeError(
            "must be a positive, finite number of seconds"
        )
    return parsed


def _prime_status_path(path: Path | None) -> Path | None:
    # Prime records land in a sibling file, never the ingest status file: an
    # async Stop ingest overwriting a killed prime's stale "started" marker
    # would destroy the very evidence the marker exists to preserve, and a
    # prime write would likewise erase ingest counts an operator needs.
    if path is None:
        return None
    return path.with_name(f"{path.stem}-prime{path.suffix}")


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
    ingest.add_argument("--timeout-seconds", type=float, default=30.0)
    ingest.add_argument("--forbidden-value", action="append", default=[])
    ingest.add_argument("--status-path", type=Path)

    prime = subparsers.add_parser("prime")
    prime.add_argument("--hook-input", type=Path)
    prime.add_argument("--config", type=Path, required=True)
    prime.add_argument("--base-url")
    prime.add_argument("--api-key")
    prime.add_argument("--project-id")
    prime.add_argument("--session-id")
    prime.add_argument("--agent-id")
    prime.add_argument("--timeout-seconds", type=float, default=15.0)
    prime.add_argument(
        "--deadline-seconds",
        type=_positive_float,
        default=PRIME_DEADLINE_SECONDS,
    )
    prime.add_argument("--max-chars", type=int, default=DEFAULT_PRIME_MAX_CHARS)
    prime.add_argument("--status-path", type=Path)

    trigger_dream = subparsers.add_parser("trigger-dream")
    trigger_dream.add_argument("--config", type=Path, required=True)
    trigger_dream.add_argument("--base-url")
    trigger_dream.add_argument("--api-key")
    trigger_dream.add_argument("--project-id")
    trigger_dream.add_argument("--session-id")
    trigger_dream.add_argument("--agent-id")

    setup = subparsers.add_parser(
        "setup-claude-code",
        description=(
            "Install Claude Code recording and print the opt-in "
            "`claude mcp add` command for read-only memory search."
        ),
    )
    setup.add_argument("--home", type=Path, default=Path.home())
    setup.add_argument("--base-url", required=True)
    setup.add_argument("--token")
    setup.add_argument("--api-key")
    setup.add_argument("--project-id")
    setup.add_argument("--session-id")
    setup.add_argument("--agent-id")
    setup.add_argument(
        "--hook-command",
        dest="hook_command",
        default=None,
    )
    setup.add_argument("--prime-hook-command")

    uninstall = subparsers.add_parser(
        "uninstall-claude-code",
        description="Remove the Claude Code Vexic recorder hooks.",
    )
    uninstall.add_argument("--home", type=Path, default=Path.home())

    setup_codex = subparsers.add_parser(
        "setup-codex",
        description=(
            "Write the Vexic MCP credential file and print the opt-in "
            "`codex mcp add` command for read-only memory search."
        ),
    )
    _add_setup_credential_args(setup_codex)

    setup_mcp_client = subparsers.add_parser(
        "setup-mcp-client",
        description=(
            "Write the Vexic MCP credential file and print the launcher command "
            "to add a generic MCP client for read-only memory search."
        ),
    )
    setup_mcp_client.add_argument("name")
    _add_setup_credential_args(setup_mcp_client)

    uninstall_codex = subparsers.add_parser(
        "uninstall-codex",
        description="Delete the Codex MCP credential file and print `codex mcp remove`.",
    )
    uninstall_codex.add_argument("--home", type=Path, default=Path.home())

    uninstall_mcp_client = subparsers.add_parser(
        "uninstall-mcp-client",
        description="Delete a generic MCP credential file and print `<name> mcp remove`.",
    )
    uninstall_mcp_client.add_argument("name")
    uninstall_mcp_client.add_argument("--home", type=Path, default=Path.home())
    return parser


def _add_setup_credential_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared `--base-url`/`--token`/manual-cred flags for setup commands."""
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token")
    parser.add_argument("--api-key")
    parser.add_argument("--project-id")
    parser.add_argument("--session-id")
    parser.add_argument("--agent-id")


def _load_config(path: Path) -> _RecorderIngestConfigFile:
    try:
        return _RecorderIngestConfigFile.model_validate_json(
            path.read_text(encoding="utf-8")
        )
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


def _read_hook_input_bytes(path: Path | None) -> bytes:
    # Read raw UTF-8 bytes and let pydantic-core decode them. Reading via
    # sys.stdin.read() decodes with the locale codec (cp1252 + surrogateescape
    # on Windows), which turns any byte the codec cannot map into a lone
    # surrogate and makes model_validate_json fail with string_unicode.
    if path is not None:
        return path.read_bytes()
    return sys.stdin.buffer.read()


def _read_hook_payload(path: Path | None) -> _ClaudeHookPayload:
    raw = _read_hook_input_bytes(path)
    try:
        return _ClaudeHookPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid hook input: {exc}") from exc


def _read_session_start_payload(path: Path | None) -> _ClaudeSessionStartHookPayload:
    raw = _read_hook_input_bytes(path)
    try:
        return _ClaudeSessionStartHookPayload.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid hook input: {exc}") from exc


def _cursor_dir(args: argparse.Namespace) -> Path | None:
    """Recorder-local cursor directory, next to the recorder config.

    Without `--config` there is no recorder-local state directory to own, so the
    run stays on the full-reread path. The cursor is only ever an optimization.
    """
    config = getattr(args, "config", None)
    if config is None:
        return None
    return Path(config).parent / "cursors"


def _try_write_cursor(
    cursor_dir: Path | None,
    transcript: Path,
    cursor: TranscriptCursor | None,
) -> None:
    """Persist the cursor after a successful ingest; never fail the run for it.

    A cursor that cannot be written just means the next run rereads the whole
    transcript and the ledger dedupes it, which is exactly the fallback path.
    """
    if cursor_dir is None or cursor is None:
        return
    try:
        write_cursor(cursor_dir, transcript, cursor)
    except Exception as exc:
        print(
            f"warning: recorder cursor write failed: {type(exc).__name__}",
            file=sys.stderr,
        )


def _ingest(args: argparse.Namespace) -> int:
    payload = _read_hook_payload(args.hook_input)
    transcript_path = payload.transcript_path
    source_session_id = payload.session_id
    args.transcript_path = transcript_path
    args.source_session_id = source_session_id

    transcript = Path(transcript_path)
    cursor_dir = _cursor_dir(args)
    scan = scan_claude_code_transcript(
        transcript,
        cursor=read_cursor(cursor_dir, transcript) if cursor_dir is not None else None,
        source_session_id=source_session_id,
    )
    messages = scan.messages
    ignored = scan.ignored

    config = HostedIngestConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        project_id=args.project_id,
        session_id=args.session_id,
        agent_id=args.agent_id,
        timeout_seconds=args.timeout_seconds,
    )
    items: list[SourceTranscriptIngestItemResult] = []
    batches = list(_iter_hosted_message_batches(messages))
    for batch in batches:
        result = post_source_messages(
            config,
            messages=batch,
            forbidden_values=tuple(args.forbidden_value),
        )
        items.extend(_validated_ingest_items(result, batch))
    inserted = sum(item.status == "inserted" for item in items)
    skipped = sum(item.status == "skipped" for item in items)
    rejected = sum(item.status == "rejected" for item in items)

    # Only after every batch posted: a cursor written ahead of a failed POST
    # would skip those rows forever, and the cursor must never decide ingest.
    _try_write_cursor(cursor_dir, transcript, scan.cursor)

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


def _trigger_dream(args: argparse.Namespace) -> int:
    # Fail-open by construction: this subcommand always exits 0. It runs
    # detached from `prime` (see _spawn_trigger_dream) and has no stdout/
    # stderr consumer other than an operator tailing logs, so any failure
    # here must never surface as a nonzero exit or block anything.
    try:
        _apply_ingest_config(args)
        config = HostedPrimeConfig(
            base_url=args.base_url,
            api_key=args.api_key,
            project_id=args.project_id,
            session_id=args.session_id,
            agent_id=args.agent_id,
        )
        post_trigger_dream_phase(config)
    except Exception as exc:
        print(f"warning: {exc}", file=sys.stderr)
    return 0


def _spawn_trigger_dream(config_path: Path) -> None:
    # Detached, fire-and-forget: prime's SessionStart hook must not gain any
    # serial latency waiting on this. All three stdio streams are DEVNULL --
    # an inherited stdout pipe would keep the hook's own stdout open until
    # this child exits, silently defeating "zero added latency". Credentials
    # travel via --config only, never argv, to avoid `ps` exposure.
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vexic.cli",
                "recorder",
                "trigger-dream",
                "--config",
                str(config_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        print(f"warning: trigger-dream spawn failed: {exc}", file=sys.stderr)


def _prime(args: argparse.Namespace) -> int:
    started = time.monotonic()
    status_session_id: str | None = None
    try:
        payload = _read_session_start_payload(args.hook_input)
        if payload.source not in {"startup", "clear"}:
            return 0
        _apply_ingest_config(args)
        status_session_id = args.session_id
        # Attempt marker before any hosted read: the SessionStart hook kills
        # this process at its timeout and no in-process handler can run, so a
        # stale "started" record is the only durable evidence of a kill.
        _try_write_status(
            _prime_status_path(args.status_path),
            RecorderStatus(
                ok=False,
                operation="prime",
                source_session_id=status_session_id,
                transcript_path=None,
                phase="started",
            ),
        )
        if args.deadline_seconds >= _SESSION_START_HOOK_KILL_SECONDS - 5:
            print(
                "warning: --deadline-seconds "
                f"{args.deadline_seconds:g} leaves under 5s of margin before "
                f"the SessionStart hook kill ({_SESSION_START_HOOK_KILL_SECONDS:g}s); "
                "a kill discards the entire priming block",
                file=sys.stderr,
            )
        result = fetch_prime_context(
            HostedPrimeConfig(
                base_url=args.base_url,
                api_key=args.api_key,
                project_id=args.project_id,
                session_id=args.session_id,
                agent_id=args.agent_id,
                timeout_seconds=args.timeout_seconds,
            ),
            max_chars=args.max_chars,
            deadline_seconds=args.deadline_seconds,
        )
        # stdout first: the hook consumes it only on a clean pre-timeout
        # exit, so nothing that can stall (status I/O, subprocess spawn) may
        # sit between a successful fetch and the print.
        if result.context:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": result.context,
                        }
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        # The harness discards even flushed stdout unless the process exits
        # cleanly before the hook kill, so the remaining work (status write,
        # dream spawn) runs in a daemon thread joined against the leftover
        # hook budget: a stall there is abandoned, never waited into the
        # kill window. Spawn stays after the reads so prime's own dream
        # trigger cannot compete with them for hosted capacity (ADR 0025 D4
        # follow-up).
        def _finish() -> None:
            _try_write_status(
                _prime_status_path(args.status_path),
                RecorderStatus(
                    ok=True,
                    operation="prime",
                    source_session_id=status_session_id,
                    transcript_path=None,
                    phase="finished",
                    legs=result.legs,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ),
            )
            _spawn_trigger_dream(args.config)

        finisher = threading.Thread(target=_finish, daemon=True)
        finisher.start()
        finisher.join(
            max(
                0.0,
                (_SESSION_START_HOOK_KILL_SECONDS - 5.0)
                - (time.monotonic() - started),
            )
        )
    except Exception as exc:
        # SessionStart priming is fail-open: stderr is the operator signal,
        # stdout stays empty so Claude Code receives no unsafe context.
        print(f"warning: {exc}", file=sys.stderr)
        _try_write_status(
            _prime_status_path(getattr(args, "status_path", None)),
            RecorderStatus(
                ok=False,
                operation="prime",
                source_session_id=status_session_id,
                transcript_path=None,
                error=f"{type(exc).__name__}: {exc}",
                phase="finished",
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
        )
        return 0
    return 0


def _resolve_setup_credentials(
    args: argparse.Namespace,
) -> tuple[str, str, str, str | None]:
    """Resolve (api_key, project_id, session_id, agent_id) for a `setup <client>`.

    Shared by every setup command: either exchange a single-use `--token` or
    accept manual credentials, the two being mutually exclusive.
    """
    if args.token is not None:
        if not args.token.strip():
            raise ValueError(
                "--token must not be blank; paste the console setup token or "
                "omit --token to use manual credentials"
            )
        conflicting = [
            option
            for option, value in (
                ("--api-key", args.api_key),
                ("--project-id", args.project_id),
                ("--session-id", args.session_id),
                ("--agent-id", args.agent_id),
            )
            if value
        ]
        if conflicting:
            raise ValueError(
                "--token and manual credentials are mutually exclusive; "
                f"drop {conflicting[0]} or omit --token"
            )
        exchange = exchange_setup_token(
            SetupExchangeConfig(base_url=args.base_url),
            token=args.token,
        )
        return (
            exchange.api_key,
            exchange.project_id,
            exchange.session_id,
            exchange.agent_id,
        )

    missing = [
        option
        for option, value in (
            ("--api-key", args.api_key),
            ("--project-id", args.project_id),
            ("--session-id", args.session_id),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise ValueError(
            f"missing required setup option: {missing[0]} "
            "(or pass --token to exchange a console setup token)"
        )
    return (args.api_key, args.project_id, args.session_id, args.agent_id)


def _validate_client_name(name: str) -> str:
    if not name or not name.strip() or not _CLIENT_NAME_RE.fullmatch(name):
        raise ValueError(
            "mcp-client name must be a safe filename component "
            "(letters, digits, '.', '_', '-'; no path separators or traversal)"
        )
    return name


def _setup_claude_code(args: argparse.Namespace) -> int:
    api_key, project_id, session_id, agent_id = _resolve_setup_credentials(args)

    result = install_claude_code_setup(
        home=args.home,
        base_url=args.base_url,
        api_key=api_key,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
        command=args.hook_command or default_recorder_hook_command(),
        prime_command=args.prime_hook_command,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "settings_path": str(result.settings_path),
                "config_path": str(result.config_path),
                "status_path": str(result.status_path),
                "connect_command": result.connect_command,
                "hook_command": result.command,
            },
            sort_keys=True,
        )
    )
    # Read-only memory search is opt-in (ADR 0027): the recorder is installed,
    # but memory search stays off until the user runs the printed command.
    print(
        "Vexic recorder installed. To enable read-only memory search (opt-in), run:\n"
        f"  {result.connect_command}\n"
        f"It reads credentials from {result.config_path}.",
        file=sys.stderr,
    )
    return 0


def _uninstall_claude_code(args: argparse.Namespace) -> int:
    removed = uninstall_claude_code_setup(home=args.home)
    print(json.dumps({"ok": True, "removed": removed}, sort_keys=True))
    return 0


def _print_connect_result(creds_path: Path, command: str) -> None:
    print(
        json.dumps(
            {
                "ok": True,
                "creds_path": str(creds_path),
                "connect_command": command,
            },
            sort_keys=True,
        )
    )


def _setup_codex(args: argparse.Namespace) -> int:
    api_key, project_id, session_id, agent_id = _resolve_setup_credentials(args)
    result = install_codex_connect(
        home=args.home,
        base_url=args.base_url,
        api_key=api_key,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
    )
    _print_connect_result(result.creds_path, result.command)
    # Read-only memory search is opt-in (ADR 0027): the creds file is written,
    # but memory search stays off until the user runs the printed command.
    print(
        "Vexic credentials written. To enable read-only memory search (opt-in), run:\n"
        f"  {result.command}\n"
        f"It reads credentials from {result.creds_path}.",
        file=sys.stderr,
    )
    return 0


def _setup_mcp_client(args: argparse.Namespace) -> int:
    name = _validate_client_name(args.name)
    api_key, project_id, session_id, agent_id = _resolve_setup_credentials(args)
    result = install_generic_connect(
        home=args.home,
        name=name,
        base_url=args.base_url,
        api_key=api_key,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
    )
    _print_connect_result(result.creds_path, result.command)
    lines = ["Vexic credentials written. To enable read-only memory search (opt-in):"]
    if result.instructions:
        lines.append(f"  {result.instructions}")
    lines.append(f"  Launcher command: {result.command}")
    lines.append(f"It reads credentials from {result.creds_path}.")
    print("\n".join(lines), file=sys.stderr)
    return 0


def _uninstall_connect(home: Path, creds_name: str, client_binary: str) -> int:
    creds_path = home / ".vexic" / creds_name
    removed = creds_path.exists()
    creds_path.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "removed": removed}, sort_keys=True))
    print(f"{client_binary} mcp remove vexic", file=sys.stderr)
    return 0


def _uninstall_codex(args: argparse.Namespace) -> int:
    return _uninstall_connect(args.home, "codex-mcp.json", "codex")


def _uninstall_mcp_client(args: argparse.Namespace) -> int:
    name = _validate_client_name(args.name)
    return _uninstall_connect(args.home, f"{name}-mcp.json", name)


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
        if args.command == "prime":
            return _prime(args)
        if args.command == "trigger-dream":
            return _trigger_dream(args)
        if args.command == "setup-claude-code":
            return _setup_claude_code(args)
        if args.command == "uninstall-claude-code":
            return _uninstall_claude_code(args)
        if args.command == "setup-codex":
            return _setup_codex(args)
        if args.command == "setup-mcp-client":
            return _setup_mcp_client(args)
        if args.command == "uninstall-codex":
            return _uninstall_codex(args)
        if args.command == "uninstall-mcp-client":
            return _uninstall_mcp_client(args)
        raise ValueError(f"unknown command: {args.command}")
    except HostedIngestTransportError as exc:
        # Transient hosted transport fault (5xx / connectivity): fail open so a
        # blip does not derail the conversation. The blocking Stop-hook exit 2
        # feeds stderr to the model and prevents the stop; this returns a
        # non-blocking exit instead and records the failure in the status file.
        # Exit 1 (not 0): ingest has a status consumer, so a nonzero code marks
        # the run as degraded, unlike the always-exit-0 fire-and-forget
        # trigger-dream subcommand.
        _try_write_status(
            getattr(args, "status_path", None),
            RecorderStatus(
                ok=False,
                operation=args.command,
                source_session_id=getattr(args, "source_session_id", None),
                transcript_path=getattr(args, "transcript_path", None),
                error=str(exc),
            ),
        )
        print(f"warning: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        _try_write_status(
            getattr(args, "status_path", None),
            RecorderStatus(
                ok=False,
                operation=args.command,
                source_session_id=getattr(args, "source_session_id", None),
                transcript_path=getattr(args, "transcript_path", None),
                error="argument parsing failed"
                if isinstance(exc, MissingIngestOption)
                else str(exc),
            ),
        )
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
