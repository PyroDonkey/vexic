"""Concurrency sweep against the deployed hosted ingest endpoint.

Drives `POST /v1/ingest_source_transcript` at several concurrency levels and
reports client-observed latency per level. Pair the numbers here with the
per-request phase timings the server records on `hosted_usage_events`
(`auth_ms`, `bind_ms`, `delegate_ms`, `total_ms`): the gap between what the
client waits and what the server reports spending IS the queueing time, and
no in-process probe can observe it.

Read the sweep like this:

    p50/p95 flat as concurrency rises
        -> requests are not queueing behind each other

    p50 growing ~linearly with concurrency while server-side `total_ms`
    stays flat
        -> requests ARE queueing; the server is servicing them one at a time

This posts REAL messages through the REAL client (`post_source_messages`), so
it exercises the same path the Claude Code Stop hook uses. Tier 1 is
append-only, so send them under a throwaway `--session-id` and purge that
scope afterwards.

Secrets (`api_key`) are read HERE, in the CLI/adapter layer, and are never
printed. `src/vexic` never reads them from the environment.

Usage:
    uv run python scripts/hosted_ingest_latency_sweep.py \\
        --config ~/.vexic/claude-code-recorder.json \\
        --session-id latency-probe-01 \\
        --concurrency 1,2,4 \\
        --trials 8 \\
        --allow-live
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import SourceTranscriptMessage
from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
from vexic.storage.transcript import single_message_adapter

# A real Stop-hook batch can approach HOSTED_WRITE_MAX_CHARS (250_000) across
# HOSTED_WRITE_MAX_MESSAGES (100), so ~2.5 KB/message is representative.
# Tiny synthetic bodies would understate per-request work by orders of
# magnitude and make the sweep measure the wrong thing.
DEFAULT_BODY_BYTES = 2500
DEFAULT_MESSAGES_PER_REQUEST = 100
SOURCE_HOST = "vexic-latency-sweep"


@dataclass(frozen=True)
class _Attempt:
    concurrency: int
    trial: int
    wall_ms: int
    ok: bool
    error_type: str | None


def _load_config(path: Path, *, session_id: str, timeout_seconds: float) -> HostedIngestConfig:
    """Read the recorder credentials file. Never logs or returns the raw key
    anywhere it could be printed."""
    raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    missing = [key for key in ("base_url", "api_key", "project_id") if not raw.get(key)]
    if missing:
        raise SystemExit(f"config {path} is missing required fields: {', '.join(missing)}")
    return HostedIngestConfig(
        base_url=str(raw["base_url"]),
        api_key=str(raw["api_key"]),
        project_id=str(raw["project_id"]),
        session_id=session_id,
        agent_id=raw.get("agent_id"),
        timeout_seconds=timeout_seconds,
    )


def _build_batch(
    *,
    session_id: str,
    label: str,
    count: int,
    body_bytes: int,
) -> list[SourceTranscriptMessage]:
    """Build `count` messages with UNIQUE source triples.

    Uniqueness is load-bearing: a reused triple takes the dedup read path
    instead of the insert path, and the sweep would then measure reads while
    claiming to measure ingest.
    """
    messages: list[SourceTranscriptMessage] = []
    for index in range(count):
        # Random filler, not a repeated character: a compressible body would
        # understate both request bytes on the wire and FTS tokenization cost.
        filler = secrets.token_hex(max(1, body_bytes // 2))
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content=f"latency sweep {label} {filler}")])
        ).decode("utf-8")
        messages.append(
            SourceTranscriptMessage(
                source_host=SOURCE_HOST,
                source_session_id=session_id,
                source_message_id=f"{label}-{index}",
                message_json=message_json,
            )
        )
    return messages


def _one_request(
    config: HostedIngestConfig,
    *,
    session_id: str,
    label: str,
    messages_per_request: int,
    body_bytes: int,
    concurrency: int,
    trial: int,
) -> _Attempt:
    batch = _build_batch(
        session_id=session_id,
        label=label,
        count=messages_per_request,
        body_bytes=body_bytes,
    )
    started = time.monotonic()
    try:
        post_source_messages(config, messages=batch, forbidden_values=())
    except Exception as exc:  # noqa: BLE001 - a failed sample is data, not a crash
        return _Attempt(
            concurrency=concurrency,
            trial=trial,
            wall_ms=int((time.monotonic() - started) * 1000),
            ok=False,
            error_type=type(exc).__name__,
        )
    return _Attempt(
        concurrency=concurrency,
        trial=trial,
        wall_ms=int((time.monotonic() - started) * 1000),
        ok=True,
        error_type=None,
    )


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile. Small trial counts make interpolation a
    false precision, so the reported number is always an observed sample."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def _run_level(
    config: HostedIngestConfig,
    *,
    session_id: str,
    run_id: str,
    concurrency: int,
    trials: int,
    messages_per_request: int,
    body_bytes: int,
) -> dict[str, object]:
    attempts: list[_Attempt] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        # `trials` requests, `concurrency` of them in flight at a time.
        futures = [
            pool.submit(
                _one_request,
                config,
                session_id=session_id,
                label=f"{run_id}-c{concurrency}-t{trial}",
                messages_per_request=messages_per_request,
                body_bytes=body_bytes,
                concurrency=concurrency,
                trial=trial,
            )
            for trial in range(trials)
        ]
        for future in futures:
            attempts.append(future.result())

    ok_ms = [attempt.wall_ms for attempt in attempts if attempt.ok]
    failures = [attempt for attempt in attempts if not attempt.ok]
    return {
        "concurrency": concurrency,
        "trials": trials,
        "ok_count": len(ok_ms),
        "failure_count": len(failures),
        "failure_types": sorted({attempt.error_type or "?" for attempt in failures}),
        "p50_ms": _percentile(ok_ms, 0.50),
        "p95_ms": _percentile(ok_ms, 0.95),
        "max_ms": max(ok_ms) if ok_ms else 0,
        "mean_ms": int(statistics.fmean(ok_ms)) if ok_ms else 0,
        "wall_ms": sorted(ok_ms),
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("~/.vexic/claude-code-recorder.json"),
        help="Recorder credentials file (base_url, api_key, project_id).",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Throwaway session scope to write into. Defaults to a random "
        "latency-probe-<hex>. Purge this scope when finished.",
    )
    parser.add_argument("--concurrency", default="1,2,4", help="Comma-separated levels.")
    parser.add_argument("--trials", type=int, default=8, help="Requests per level.")
    parser.add_argument("--messages", type=int, default=DEFAULT_MESSAGES_PER_REQUEST)
    parser.add_argument("--body-bytes", type=int, default=DEFAULT_BODY_BYTES)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Per-attempt socket timeout. Deliberately well above the 10s the "
        "Stop hook uses, so a slow request is measured rather than retried.",
    )
    parser.add_argument("--output", type=Path, default=Path("ingest_latency_sweep.json"))
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Required. This posts real messages to the deployed service.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not args.allow_live:
        print(
            "Refusing to run without --allow-live: this posts real messages to "
            "the deployed hosted service.",
            file=sys.stderr,
        )
        return 2
    if args.trials < 1:
        raise SystemExit("--trials must be at least 1")
    try:
        levels = [int(part) for part in str(args.concurrency).split(",") if part.strip()]
    except ValueError as exc:
        raise SystemExit(f"invalid --concurrency: {args.concurrency}") from exc
    if not levels or any(level < 1 for level in levels):
        raise SystemExit("--concurrency must be positive integers")

    run_id = secrets.token_hex(4)
    session_id = args.session_id or f"latency-probe-{run_id}"
    config = _load_config(
        args.config, session_id=session_id, timeout_seconds=args.timeout_seconds
    )

    print(f"session_id={session_id} (purge this scope when finished)", file=sys.stderr)
    results = []
    for level in levels:
        print(f"concurrency={level} trials={args.trials} ...", file=sys.stderr)
        results.append(
            _run_level(
                config,
                session_id=session_id,
                run_id=run_id,
                concurrency=level,
                trials=args.trials,
                messages_per_request=args.messages,
                body_bytes=args.body_bytes,
            )
        )

    report = {
        "session_id": session_id,
        "messages_per_request": args.messages,
        "body_bytes_per_message": args.body_bytes,
        "timeout_seconds": args.timeout_seconds,
        "levels": results,
    }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
