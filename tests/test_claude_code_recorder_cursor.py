"""Recorder-local transcript cursor.

The Stop hook re-reads the Claude Code JSONL transcript on every invocation.
A recorder-local cursor lets a run resume from the last processed row instead
of re-POSTing the whole session. Correctness must never depend on the cursor:
the hosted source ledger stays the duplicate guard, and every stale, missing,
corrupt, truncated, or rotated cursor falls back to a full reread.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    SearchTranscriptRequest,
    SourceTranscriptMessage,
    TrustBoundary,
)
from vexic.hosted import HostedMemoryService
from vexic.hosted_http import create_app
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.recorders.cli import main as recorder_main
from vexic.recorders.hosted_ingest import HostedIngestTransportError
from vexic.recorders.transcript_cursor import (
    TranscriptCursor,
    cursor_path,
    read_cursor,
    write_cursor,
)


def _user_row(uuid: str, text: str, *, session_id: str = "claude-session") -> str:
    return json.dumps(
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": uuid,
            "message": {"role": "user", "content": text},
        }
    )


def _write_transcript(
    path: Path, rows: list[str], *, trailing_newline: bool = True
) -> None:
    text = "\n".join(rows)
    if trailing_newline and rows:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _ingest_item(
    message: SourceTranscriptMessage,
    *,
    status: str = "inserted",
) -> dict[str, object]:
    return {
        "source_host": message.source_host,
        "source_session_id": message.source_session_id,
        "source_message_id": message.source_message_id,
        "status": status,
    }


class _RecorderHarness:
    """Drives `vexic recorder ingest` against a temp home and records posts."""

    def __init__(
        self, root: Path, *, source_session_id: str | None = "claude-session"
    ) -> None:
        self.root = root
        self.transcript = root / "claude-session.jsonl"
        self.config_path = root / "vexic" / "claude-code-recorder.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path = root / "vexic" / "claude-code-recorder-status.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "base_url": "https://api.example.test",
                    "api_key": "vx_secret",
                    "project_id": "project-a",
                    "session_id": "vexic-session",
                    "status_path": str(self.status_path),
                }
            ),
            encoding="utf-8",
        )
        self.hook_path = root / "hook.json"
        hook: dict[str, str] = {
            "hook_event_name": "Stop",
            "transcript_path": str(self.transcript),
        }
        if source_session_id is not None:
            hook["session_id"] = source_session_id
        self.hook_path.write_text(json.dumps(hook), encoding="utf-8")
        self.posted: list[list[str]] = []

    @property
    def cursor_dir(self) -> Path:
        return self.config_path.parent / "cursors"

    def cursor_files(self) -> list[Path]:
        if not self.cursor_dir.exists():
            return []
        return sorted(self.cursor_dir.iterdir())

    def run(
        self,
        *,
        post_error: Exception | None = None,
        response_factory: Callable[[list[SourceTranscriptMessage]], object]
        | None = None,
    ) -> int:
        def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
            self.posted.append([message.source_message_id for message in messages])
            if post_error is not None:
                raise post_error
            if response_factory is not None:
                return response_factory(messages)
            return {"items": [_ingest_item(message) for message in messages]}

        with (
            patch("vexic.recorders.cli.post_source_messages", fake_post),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return recorder_main(
                [
                    "ingest",
                    "--config",
                    str(self.config_path),
                    "--hook-input",
                    str(self.hook_path),
                ]
            )

    def posted_ids(self) -> list[str]:
        return [message_id for batch in self.posted for message_id in batch]


class RecorderTranscriptCursorTests(unittest.TestCase):
    def test_second_run_posts_only_rows_appended_since_the_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

            with harness.transcript.open("a", encoding="utf-8") as handle:
                handle.write(_user_row("uuid-3", "and juniper") + "\n")
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-3"])

    def test_unchanged_transcript_posts_no_rows_on_the_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript, [_user_row("uuid-1", "remember cedar")]
            )

            self.assertEqual(harness.run(), 0)
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), [])

    def test_removed_cursor_falls_back_to_a_full_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )
            self.assertEqual(harness.run(), 0)

            for path in harness.cursor_files():
                path.unlink()
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

    def test_corrupt_cursor_falls_back_to_a_full_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )
            self.assertEqual(harness.run(), 0)

            cursor_files = harness.cursor_files()
            self.assertEqual(len(cursor_files), 1)
            cursor_files[0].write_text("{not json", encoding="utf-8")
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

    def test_cursor_with_impossible_offsets_falls_back_to_a_full_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )
            self.assertEqual(harness.run(), 0)

            cursor_files = harness.cursor_files()
            self.assertEqual(len(cursor_files), 1)
            cursor_files[0].write_text(
                json.dumps(
                    {
                        "source_session_id": "claude-session",
                        "byte_offset": 4096,
                        "last_line_offset": -5,
                        "last_line_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

    def test_truncated_transcript_triggers_a_full_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                    _user_row("uuid-3", "and juniper"),
                ],
            )
            self.assertEqual(harness.run(), 0)

            # File is now shorter than the recorded cursor offset.
            _write_transcript(harness.transcript, [_user_row("uuid-4", "restarted")])
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-4"])

    def test_rotated_transcript_of_equal_length_triggers_a_full_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )
            self.assertEqual(harness.run(), 0)
            original_size = harness.transcript.stat().st_size

            # Same path, same byte length, different rows: the cursor offset is
            # still inside the file but points at a row that no longer matches.
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-3", "remember cedar"),
                    _user_row("uuid-4", "and orchid"),
                ],
            )
            self.assertEqual(harness.transcript.stat().st_size, original_size)
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-3", "uuid-4"])

    def test_same_length_rewrite_self_heals_on_the_following_run(self) -> None:
        # Run 2 detects the same-length rewrite via the prefix digest and does a
        # correct full reread, but its corrected cursor has the same byte_offset
        # as the stale one already on disk. If that write were skipped as a
        # "regression", the stale prefix hash would stick around forever and
        # every following run would keep failing verification and re-reading --
        # run 3, with the transcript untouched since run 2, must resume cleanly
        # and post nothing.
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )
            self.assertEqual(harness.run(), 0)
            original_size = harness.transcript.stat().st_size

            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-3", "remember cedar"),
                    _user_row("uuid-4", "and orchid"),
                ],
            )
            self.assertEqual(harness.transcript.stat().st_size, original_size)
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-3", "uuid-4"])

            harness.posted.clear()
            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), [])

    def test_same_length_rewrite_before_unchanged_final_line_triggers_full_reread(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            original = [
                _user_row("uuid-1", "remember cedar"),
                _user_row("uuid-2", "and orchid"),
            ]
            _write_transcript(harness.transcript, original)
            self.assertEqual(harness.run(), 0)
            original_size = harness.transcript.stat().st_size

            # The last consumed line and total byte length are unchanged. Only a
            # digest of the whole consumed prefix can detect this earlier rewrite.
            replacement = [
                _user_row("uuid-3", "remember cedar"),
                original[-1],
            ]
            _write_transcript(harness.transcript, replacement)
            self.assertEqual(harness.transcript.stat().st_size, original_size)
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-3", "uuid-2"])

    def test_cursor_without_prefix_digest_forces_a_full_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )
            self.assertEqual(harness.run(), 0)
            (cursor_file,) = harness.cursor_files()
            old_cursor = json.loads(cursor_file.read_text(encoding="utf-8"))
            old_cursor.pop("prefix_sha256", None)
            cursor_file.write_text(json.dumps(old_cursor), encoding="utf-8")
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

    def test_new_source_session_at_the_same_path_triggers_a_full_reread(self) -> None:
        # The cursor is keyed by transcript path *and* source session. A hook that
        # reports a different session for the same path cannot be resumed, even
        # when the bytes on disk still fingerprint clean.
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            rows = [
                _user_row("uuid-1", "remember cedar"),
                _user_row("uuid-2", "and orchid"),
            ]
            _write_transcript(harness.transcript, rows)
            self.assertEqual(harness.run(), 0)

            replacement = _RecorderHarness(
                Path(temp), source_session_id="claude-session-2"
            )
            self.assertEqual(replacement.transcript, harness.transcript)
            self.assertEqual(replacement.cursor_dir, harness.cursor_dir)

            self.assertEqual(replacement.run(), 0)
            self.assertEqual(replacement.posted_ids(), ["uuid-1", "uuid-2"])

    def test_hook_without_a_source_session_triggers_a_full_reread(self) -> None:
        # A hook payload that omits `session_id` leaves the run unable to check
        # the cursor's session against the session on disk. An unchecked cursor
        # cannot be trusted -- the transcript at this path may belong to a new
        # session whose rows the cursor would seek straight past -- so the run
        # rereads the whole file and lets the hosted ledger dedupe.
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )
            self.assertEqual(harness.run(), 0)
            self.assertEqual(len(harness.cursor_files()), 1)

            # Same path, transcript untouched: the cursor still fingerprints
            # clean, so only the unknown session forces the reread.
            sessionless = _RecorderHarness(Path(temp), source_session_id=None)
            self.assertEqual(sessionless.transcript, harness.transcript)
            self.assertEqual(sessionless.cursor_dir, harness.cursor_dir)

            self.assertEqual(sessionless.run(), 0)
            self.assertEqual(sessionless.posted_ids(), ["uuid-1", "uuid-2"])

    def test_failed_post_does_not_advance_the_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )

            code = harness.run(
                post_error=RuntimeError("hosted ingest failed: HTTP 503")
            )
            self.assertEqual(code, 2)
            self.assertEqual(harness.cursor_files(), [])
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

    def test_transport_error_post_does_not_advance_the_cursor(self) -> None:
        # The fail-open twin of the RuntimeError/exit-2 pin above: an exhausted
        # transient fault degrades to exit 1, the cursor stays unwritten, and
        # the next run re-posts every row (the hosted ledger dedups).
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
            )

            code = harness.run(
                post_error=HostedIngestTransportError(
                    "hosted ingest failed: HTTP 503"
                )
            )
            self.assertEqual(code, 1)
            self.assertEqual(harness.cursor_files(), [])
            status = json.loads(harness.status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

    def test_duplicate_source_keys_with_matching_results_advance_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-1", "remember cedar again"),
                ],
            )

            def matching_results(
                messages: list[SourceTranscriptMessage],
            ) -> object:
                self.assertEqual(len(messages), 2)
                return {
                    "items": [
                        _ingest_item(messages[0], status="inserted"),
                        _ingest_item(messages[1], status="skipped"),
                    ]
                }

            self.assertEqual(harness.run(response_factory=matching_results), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-1"])
            status = json.loads(harness.status_path.read_text(encoding="utf-8"))
            self.assertEqual((status["inserted"], status["skipped"]), (1, 1))
            (cursor_file,) = harness.cursor_files()
            cursor = json.loads(cursor_file.read_text(encoding="utf-8"))
            self.assertEqual(cursor["byte_offset"], harness.transcript.stat().st_size)

            harness.posted.clear()
            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), [])

    def test_invalid_hosted_result_does_not_advance_an_existing_cursor(self) -> None:
        cases: dict[
            str,
            Callable[[list[SourceTranscriptMessage]], object],
        ] = {
            "missing_items": lambda _messages: {},
            "malformed_item": lambda messages: {
                "items": [_ingest_item(messages[0], status="unknown")]
            },
            "truncated_items": lambda _messages: {"items": []},
            "duplicate_items": lambda messages: {
                "items": [_ingest_item(messages[0]), _ingest_item(messages[0])]
            },
            "mismatched_item": lambda messages: {
                "items": [
                    {
                        **_ingest_item(messages[0]),
                        "source_message_id": "different-source-message",
                    }
                ]
            },
        }
        for name, response_factory in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                harness = _RecorderHarness(Path(temp))
                _write_transcript(
                    harness.transcript,
                    [_user_row("uuid-1", "remember cedar")],
                )
                self.assertEqual(harness.run(), 0)
                (cursor_file,) = harness.cursor_files()
                cursor_before = cursor_file.read_bytes()

                with harness.transcript.open("a", encoding="utf-8") as handle:
                    handle.write(_user_row("uuid-2", "and orchid") + "\n")
                harness.posted.clear()

                self.assertEqual(harness.run(response_factory=response_factory), 2)
                self.assertEqual(cursor_file.read_bytes(), cursor_before)

                self.assertEqual(harness.run(), 0)
                self.assertEqual(harness.posted_ids(), ["uuid-2", "uuid-2"])

    def test_unterminated_final_row_is_ingested_and_reread_until_it_is_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            harness = _RecorderHarness(Path(temp))
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember cedar"),
                    _user_row("uuid-2", "and orchid"),
                ],
                trailing_newline=False,
            )

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-1", "uuid-2"])

            # The cursor never advances past a row that is not newline-terminated,
            # so the partially flushed row is re-read (and ledger-deduped) rather
            # than skipped once the host finishes writing it.
            with harness.transcript.open("a", encoding="utf-8") as handle:
                handle.write("\n" + _user_row("uuid-3", "and juniper") + "\n")
            harness.posted.clear()

            self.assertEqual(harness.run(), 0)
            self.assertEqual(harness.posted_ids(), ["uuid-2", "uuid-3"])


class RecorderCursorLedgerDedupTests(unittest.TestCase):
    """The hosted source ledger, never the cursor, is the duplicate guard."""

    def test_full_reread_after_cursor_loss_is_deduped_by_the_hosted_ledger(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = HostedTenantCatalog(root)
            keys = HostedApiKeyStore(root)
            catalog.provision_tenant("tenant-a", project_ids={"project-a"})
            api_key = keys.create_key(
                tenant_id="tenant-a",
                principal_id="agent-a",
                capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
                project_ids={"project-a"},
            ).raw_key
            client = TestClient(
                create_app(HostedMemoryService(catalog, keys, telemetry=catalog))
            )

            harness = _RecorderHarness(root)
            harness.config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://testserver",
                        "api_key": api_key,
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "status_path": str(harness.status_path),
                    }
                ),
                encoding="utf-8",
            )
            _write_transcript(
                harness.transcript,
                [
                    _user_row("uuid-1", "remember hosted-orchid"),
                    _user_row("uuid-2", "and hosted-juniper"),
                ],
            )

            class _Response:
                def __init__(self, content: bytes) -> None:
                    self._body = io.BytesIO(content)

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self, size: int = -1) -> bytes:
                    return self._body.read(size)

            def fake_urlopen(request, timeout):
                response = client.request(
                    request.get_method(),
                    urlsplit(request.full_url).path,
                    headers=dict(request.header_items()),
                    content=request.data,
                )
                response.raise_for_status()
                return _Response(response.content)

            def run_hook() -> dict:
                with (
                    patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = recorder_main(
                        [
                            "ingest",
                            "--config",
                            str(harness.config_path),
                            "--hook-input",
                            str(harness.hook_path),
                        ]
                    )
                self.assertEqual(code, 0)
                return json.loads(harness.status_path.read_text(encoding="utf-8"))

            first = run_hook()

            # Lose the cursor: the run must reread the whole transcript and the
            # ledger must skip -- not re-insert -- every already-ingested row.
            for path in harness.cursor_files():
                path.unlink()
            second = run_hook()

            search_response = client.post(
                "/v1/search_transcript",
                headers={"Authorization": f"Bearer {api_key}"},
                json=SearchTranscriptRequest(
                    scope=MemoryScope(
                        tenant_id="tenant-a",
                        project_id="project-a",
                        session_id="session-a",
                        principal=Principal(
                            principal_id="agent-a",
                            principal_type=PrincipalType.HUMAN,
                        ),
                        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
                        capabilities={MemoryCapability.SEARCH},
                    ),
                    query="hosted-orchid",
                ).model_dump(mode="json"),
            )

            self.assertEqual((first["inserted"], first["skipped"]), (2, 0))
            self.assertEqual((second["inserted"], second["skipped"]), (0, 2))
            self.assertEqual(search_response.status_code, 200)
            # OR-semantics recall (ADR 0036) also surfaces the hosted-juniper
            # row on the shared "hosted" token; strict list equality still
            # proves the ledger deduped -- neither body appears twice.
            self.assertEqual(
                [hit["body"] for hit in search_response.json()["hits"]],
                ["User: remember hosted-orchid", "User: and hosted-juniper"],
            )


def _cursor(byte_offset: int, *, prefix_sha256: str = "0" * 64) -> TranscriptCursor:
    return TranscriptCursor(
        source_session_id="claude-session",
        byte_offset=byte_offset,
        prefix_sha256=prefix_sha256,
        last_line_offset=0,
        last_line_sha256="0" * 64,
    )


class WriteCursorMonotonicTests(unittest.TestCase):
    """`write_cursor` must not let a late-finishing older run regress a cursor
    a newer overlapping run already advanced."""

    def test_older_byte_offset_does_not_overwrite_a_newer_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cursor_dir = Path(temp)
            transcript = cursor_dir / "session.jsonl"

            write_cursor(cursor_dir, transcript, _cursor(200))
            write_cursor(cursor_dir, transcript, _cursor(100))

            self.assertEqual(
                read_cursor(cursor_dir, transcript).byte_offset,  # type: ignore[union-attr]
                200,
            )

    def test_newer_byte_offset_replaces_the_existing_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cursor_dir = Path(temp)
            transcript = cursor_dir / "session.jsonl"

            write_cursor(cursor_dir, transcript, _cursor(100))
            write_cursor(cursor_dir, transcript, _cursor(200))

            self.assertEqual(
                read_cursor(cursor_dir, transcript).byte_offset,  # type: ignore[union-attr]
                200,
            )

    def test_equal_byte_offset_identical_cursor_skips_the_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cursor_dir = Path(temp)
            transcript = cursor_dir / "session.jsonl"

            write_cursor(cursor_dir, transcript, _cursor(100))
            path = cursor_path(cursor_dir, transcript)
            before = path.read_bytes()

            write_cursor(cursor_dir, transcript, _cursor(100))

            self.assertEqual(path.read_bytes(), before)

    def test_equal_byte_offset_different_hash_still_writes(self) -> None:
        # A same-length transcript rewrite produces a corrected cursor at the
        # same byte_offset but a different prefix digest. Skipping this write
        # would pin the stale digest in place and force every following run
        # to keep failing verification and fully rereading.
        with tempfile.TemporaryDirectory() as temp:
            cursor_dir = Path(temp)
            transcript = cursor_dir / "session.jsonl"

            write_cursor(cursor_dir, transcript, _cursor(100, prefix_sha256="a" * 64))
            write_cursor(cursor_dir, transcript, _cursor(100, prefix_sha256="b" * 64))

            self.assertEqual(
                read_cursor(cursor_dir, transcript).prefix_sha256,  # type: ignore[union-attr]
                "b" * 64,
            )

    def test_corrupt_existing_cursor_does_not_block_the_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cursor_dir = Path(temp)
            transcript = cursor_dir / "session.jsonl"
            path = cursor_path(cursor_dir, transcript)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xfenot json garbage")

            write_cursor(cursor_dir, transcript, _cursor(100))

            self.assertEqual(
                read_cursor(cursor_dir, transcript).byte_offset,  # type: ignore[union-attr]
                100,
            )


if __name__ == "__main__":
    unittest.main()
