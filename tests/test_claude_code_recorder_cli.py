import contextlib
import io
import json
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError
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
from vexic.hosted import HOSTED_WRITE_MAX_CHARS, HostedMemoryService
from vexic.hosted_http import create_app
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.recorders.claude_code import TranscriptScan
from vexic.recorders.cli import main as recorder_main
from vexic.recorders.claude_setup import (
    install_claude_code_setup,
    uninstall_claude_code_setup,
)
from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
from vexic.recorders import hosted_prime
from vexic.recorders.hosted_prime import (
    HostedPrimeConfig,
    build_prime_context,
    fetch_prime_context,
)
from vexic.recorders.status import RecorderStatus, write_status


def _ingest_result(
    messages: list[SourceTranscriptMessage],
    statuses: list[str] | None = None,
) -> dict[str, object]:
    resolved_statuses = statuses or ["inserted"] * len(messages)
    if len(resolved_statuses) != len(messages):
        raise AssertionError("test result statuses must match messages")
    return {
        "items": [
            {
                "source_host": message.source_host,
                "source_session_id": message.source_session_id,
                "source_message_id": message.source_message_id,
                "status": status,
            }
            for message, status in zip(messages, resolved_statuses, strict=True)
        ]
    }


class ClaudeCodeRecorderCliTests(unittest.TestCase):
    def test_post_source_messages_sends_scope_headers_without_agent_id(self) -> None:
        calls = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return _Response()

        config = HostedIngestConfig(
            base_url="https://api.example.test/",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
            timeout_seconds=7.0,
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen):
            result = post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(result, {"items": []})
        request, timeout = calls[0]
        self.assertEqual(timeout, 7.0)
        self.assertEqual(request.full_url, "https://api.example.test/v1/ingest_source_transcript")
        self.assertEqual(request.get_header("Authorization"), "Bearer vx_secret")
        self.assertEqual(request.get_header("X-vexic-project-id"), "project-a")
        self.assertEqual(request.get_header("X-vexic-session-id"), "session-a")
        self.assertIsNone(request.get_header("X-vexic-agent-id"))
        body = json.loads(request.data.decode())
        self.assertEqual(body, {"messages": [], "redaction": {"forbidden_values": []}})

    def test_post_source_messages_includes_agent_id_when_configured(self) -> None:
        calls = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        def fake_urlopen(request, timeout):
            calls.append(request)
            return _Response()

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id="agent-a",
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen):
            post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(calls[0].get_header("X-vexic-agent-id"), "agent-a")

    def test_post_source_messages_raises_sanitized_http_error(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        error = HTTPError(
            url="https://api.example.test/v1/ingest_source_transcript",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=None,
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "hosted ingest failed: HTTP 403"):
                post_source_messages(config, messages=[], forbidden_values=())

    def test_post_source_messages_4xx_error_includes_server_error_detail(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        body = io.BytesIO(
            json.dumps(
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "limit must be between 1 and 20.",
                    }
                }
            ).encode("utf-8")
        )
        error = HTTPError(
            url="https://api.example.test/v1/ingest_source_transcript",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=body,
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError,
                r"hosted ingest failed: HTTP 400 \(invalid_request: "
                r"limit must be between 1 and 20\.\)",
            ):
                post_source_messages(config, messages=[], forbidden_values=())

    def test_post_source_messages_5xx_error_surfaces_code_only(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        body = io.BytesIO(
            json.dumps(
                {
                    "error": {
                        "code": "storage_unavailable",
                        "message": "SQLITE_BUSY: backend storage detail",
                    }
                }
            ).encode("utf-8")
        )
        error = HTTPError(
            url="https://api.example.test/v1/ingest_source_transcript",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=body,
        )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", side_effect=error),
            patch("vexic.recorders.hosted_ingest.time.sleep"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"hosted ingest failed: HTTP 503 \(storage_unavailable\)$",
            ):
                post_source_messages(config, messages=[], forbidden_values=())

    def test_post_source_messages_http_error_without_json_body_stays_bare(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        error = HTTPError(
            url="https://api.example.test/v1/ingest_source_transcript",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(b"<html>not json</html>"),
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, r"hosted ingest failed: HTTP 400$"):
                post_source_messages(config, messages=[], forbidden_values=())

    def test_post_source_messages_oversized_error_body_is_not_fully_read(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        huge = b'{"error": {"code": "' + b"a" * (10 * 1024 * 1024) + b'"}}'
        read_sizes: list[int | None] = []

        class _TrackingBody(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                read_sizes.append(size)
                return super().read(size)

        error = HTTPError(
            url="https://api.example.test/v1/ingest_source_transcript",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=_TrackingBody(huge),
        )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", side_effect=error),
            patch("vexic.recorders.hosted_ingest.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, r"hosted ingest failed: HTTP 502$"):
                post_source_messages(config, messages=[], forbidden_values=())

        self.assertTrue(read_sizes)
        for size in read_sizes:
            self.assertIsNotNone(size)
            self.assertGreaterEqual(size, 0)
            self.assertLessEqual(size, 1024 * 1024)

    def test_post_source_messages_rejects_forbidden_value_before_egress(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        message = SourceTranscriptMessage(
            source_host="claude-code",
            source_session_id="claude-session",
            source_message_id="uuid-1",
            message_json="User: cedar-secret",
        )

        with patch("vexic.recorders.hosted_ingest.urlopen") as urlopen_mock:
            with self.assertRaisesRegex(ValueError, "forbidden secret value"):
                post_source_messages(
                    config,
                    messages=[message],
                    forbidden_values=("cedar-secret",),
                )

        urlopen_mock.assert_not_called()

    def test_post_source_messages_rejects_non_http_base_url(self) -> None:
        config = HostedIngestConfig(
            base_url="file:///tmp/vexic",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        with patch("vexic.recorders.hosted_ingest.urlopen") as urlopen_mock:
            with self.assertRaisesRegex(ValueError, "base_url.*http"):
                post_source_messages(config, messages=[], forbidden_values=())

        urlopen_mock.assert_not_called()

    def test_post_source_messages_retries_transient_5xx_then_succeeds(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError(
                    url="https://api.example.test/v1/ingest_source_transcript",
                    code=503,
                    msg="Service Unavailable",
                    hdrs={},
                    fp=None,
                )
            return _Response()

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            result = post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(result, {"items": []})
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(0.5)

    def test_post_source_messages_retries_urlerror_then_succeeds(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise URLError(TimeoutError("timed out"))
            return _Response()

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            result = post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(result, {"items": []})
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(0.5)

    def test_post_source_messages_does_not_retry_4xx(self) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=None,
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
        ):
            with self.assertRaises(RuntimeError) as caught:
                post_source_messages(config, messages=[], forbidden_values=())

        self.assertNotIsInstance(caught.exception, HostedIngestTransportError)
        self.assertRegex(str(caught.exception), "hosted ingest failed: HTTP 403")
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_post_source_messages_retries_429_then_succeeds(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError(
                    url="https://api.example.test/v1/ingest_source_transcript",
                    code=429,
                    msg="Too Many Requests",
                    hdrs={},
                    fp=None,
                )
            return _Response()

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            result = post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(result, {"items": []})
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(0.5)

    def test_post_source_messages_429_honors_retry_after_seconds(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError(
                    url="https://api.example.test/v1/ingest_source_transcript",
                    code=429,
                    msg="Too Many Requests",
                    hdrs={"Retry-After": "7"},
                    fp=None,
                )
            return _Response()

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform") as uniform_mock,
        ):
            result = post_source_messages(config, messages=[], forbidden_values=())

        # The server-directed wait replaces the jittered backoff outright:
        # the server value already staggers clients.
        self.assertEqual(result, {"items": []})
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(7.0)
        uniform_mock.assert_not_called()

    def test_post_source_messages_bad_retry_after_falls_back_to_backoff(self) -> None:
        # Malformed ("soon"), zero, and negative Retry-After values all fall
        # back to the jittered backoff. int("-1") parses, and an honored
        # negative would reach time.sleep(-1) -> ValueError -> loud exit 2,
        # exactly the failure mode the 429 allowlist removes.
        for raw in ("soon", "0", "-1"):
            with self.subTest(retry_after=raw):
                config = HostedIngestConfig(
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                )

                class _Response:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_exc):
                        return False

                    def read(self) -> bytes:
                        return b'{"items":[]}'

                calls: list[int] = []

                def fake_urlopen(request, timeout):
                    calls.append(1)
                    if len(calls) == 1:
                        raise HTTPError(
                            url="https://api.example.test/v1/ingest_source_transcript",
                            code=429,
                            msg="Too Many Requests",
                            hdrs={"Retry-After": raw},
                            fp=None,
                        )
                    return _Response()

                with (
                    patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
                    patch(
                        "vexic.recorders.hosted_ingest.time.sleep"
                    ) as sleep_mock,
                    patch(
                        "vexic.recorders.hosted_ingest.random.uniform",
                        return_value=1.0,
                    ),
                ):
                    result = post_source_messages(
                        config, messages=[], forbidden_values=()
                    )

                self.assertEqual(result, {"items": []})
                sleep_mock.assert_called_once_with(0.5)

    def test_post_source_messages_retry_after_capped(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        def make_urlopen(calls: list[int]):
            def fake_urlopen(request, timeout):
                calls.append(1)
                if len(calls) == 1:
                    raise HTTPError(
                        url="https://api.example.test/v1/ingest_source_transcript",
                        code=429,
                        msg="Too Many Requests",
                        hdrs={"Retry-After": "120"},
                        fp=None,
                    )
                return _Response()

            return fake_urlopen

        # Without a budget the defensive 30s constant caps the wait.
        calls: list[int] = []
        with (
            patch("vexic.recorders.hosted_ingest.urlopen", make_urlopen(calls)),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
        ):
            post_source_messages(config, messages=[], forbidden_values=())
        sleep_mock.assert_called_once_with(30.0)

        # With a budget too small for the server-directed wait, the fault
        # surfaces immediately instead of sleeping into certain exhaustion.
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        calls = []
        with (
            patch("vexic.recorders.hosted_ingest.urlopen", make_urlopen(calls)),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch(
                "vexic.recorders.hosted_ingest.time.monotonic",
                # started, attempt-1 pre-check, retry decision (5s left,
                # which cannot fit the 30s-capped Retry-After wait),
                # body-read guard.
                side_effect=[0.0, 0.0, 5.0, 5.0],
            ),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(
                    config,
                    messages=[],
                    forbidden_values=(),
                    budget_seconds=10.0,
                )
        self.assertRegex(str(caught.exception), "hosted ingest failed: HTTP 429")
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_post_source_messages_408_exhausts_to_transport_error(self) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=408,
                msg="Request Timeout",
                hdrs={},
                fp=None,
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(config, messages=[], forbidden_values=())

        self.assertRegex(str(caught.exception), "hosted ingest failed: HTTP 408")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep_mock.call_args_list, [call(0.5), call(1.0)])

    def test_post_source_messages_does_not_retry_unlisted_4xx(self) -> None:
        # 413 is deliberately NOT in the retry allowlist: an oversized payload
        # signals a config/batching bug, not transience, so it stays loud.
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=413,
                msg="Payload Too Large",
                hdrs={},
                fp=None,
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
        ):
            with self.assertRaises(RuntimeError) as caught:
                post_source_messages(config, messages=[], forbidden_values=())

        self.assertNotIsInstance(caught.exception, HostedIngestTransportError)
        self.assertRegex(str(caught.exception), "hosted ingest failed: HTTP 413")
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_post_source_messages_exhausted_retries_raise_transport_error(self) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=503,
                msg="Service Unavailable",
                hdrs={},
                fp=None,
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(config, messages=[], forbidden_values=())

        self.assertRegex(str(caught.exception), "hosted ingest failed: HTTP 503")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep_mock.call_args_list, [call(0.5), call(1.0)])

    def test_post_source_messages_backoff_is_jittered(self) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        def fake_urlopen(request, timeout):
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=503,
                msg="Service Unavailable",
                hdrs={},
                fp=None,
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch(
                "vexic.recorders.hosted_ingest.random.uniform", return_value=1.5
            ) as uniform_mock,
        ):
            with self.assertRaises(HostedIngestTransportError):
                post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(sleep_mock.call_args_list, [call(0.75), call(1.5)])
        for args in uniform_mock.call_args_list:
            self.assertEqual(args, call(0.5, 1.5))

    def test_post_source_messages_budget_exhausted_before_first_attempt(self) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen") as urlopen_mock,
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch(
                "vexic.recorders.hosted_ingest.time.monotonic",
                side_effect=[0.0, 12.0],
            ),
        ):
            with self.assertRaisesRegex(HostedIngestTransportError, "budget"):
                post_source_messages(
                    config,
                    messages=[],
                    forbidden_values=(),
                    budget_seconds=10.0,
                )

        urlopen_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_post_source_messages_budget_exhausted_before_retry(self) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=503,
                msg="Service Unavailable",
                hdrs={},
                fp=None,
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch(
                "vexic.recorders.hosted_ingest.time.monotonic",
                # started, attempt-1 pre-check, attempt-1 retry decision (the
                # budget is spent while the first attempt is in flight, so no
                # second attempt and no sleep may happen), body-read guard.
                side_effect=[0.0, 5.0, 12.0, 12.0],
            ),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(
                    config,
                    messages=[],
                    forbidden_values=(),
                    budget_seconds=10.0,
                )

        self.assertRegex(str(caught.exception), "hosted ingest failed: HTTP 503")
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_post_source_messages_skips_error_body_read_when_budget_spent(self) -> None:
        # The exhaustion raise normally reads the error body for detail, but a
        # dripping body can block up to a socket timeout; with the budget
        # already spent that delay would push the fail-open exit toward the
        # Stop hook kill, so the read is skipped and the bare code surfaces.
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        read_sizes: list[int | None] = []

        class _TrackingBody(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                read_sizes.append(size)
                return super().read(size)

        def fake_urlopen(request, timeout):
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=503,
                msg="Service Unavailable",
                hdrs={},
                fp=_TrackingBody(b'{"error": {"code": "storage_unavailable"}}'),
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
            patch(
                "vexic.recorders.hosted_ingest.time.monotonic",
                # started, attempt-1 pre-check, retry decision (budget spent),
                # the body-read guard's own check.
                side_effect=[0.0, 5.0, 12.0, 12.0],
            ),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(
                    config,
                    messages=[],
                    forbidden_values=(),
                    budget_seconds=10.0,
                )

        self.assertEqual(str(caught.exception), "hosted ingest failed: HTTP 503")
        self.assertEqual(read_sizes, [])
        sleep_mock.assert_not_called()

    def test_post_source_messages_skips_sleep_that_cannot_fit_the_budget(self) -> None:
        # Sleeping into a guaranteed budget failure would only delay the
        # fail-open exit: when the backoff cannot fit the remaining budget the
        # underlying fault surfaces immediately, keeping its HTTP code.
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            raise HTTPError(
                url="https://api.example.test/v1/ingest_source_transcript",
                code=503,
                msg="Service Unavailable",
                hdrs={},
                fp=None,
            )

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
            patch(
                "vexic.recorders.hosted_ingest.time.monotonic",
                # started, attempt-1 pre-check, attempt-1 retry decision
                # (0.25s of budget is left there, which cannot fit the 0.5s
                # backoff), body-read guard.
                side_effect=[0.0, 0.0, 9.75, 9.75],
            ),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(
                    config,
                    messages=[],
                    forbidden_values=(),
                    budget_seconds=10.0,
                )

        self.assertRegex(str(caught.exception), "hosted ingest failed: HTTP 503")
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_post_source_messages_response_read_bounded_by_budget(self) -> None:
        # A socket timeout bounds each recv, not the whole body: a proxy that
        # drips bytes forever must not stretch the read past the retry budget
        # and into the Stop hook kill.
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _DripResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, size=-1) -> bytes:
                return b"{"  # never finishes

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            return _DripResponse()

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
            patch(
                "vexic.recorders.hosted_ingest.time.monotonic",
                # started, attempt-1 pre-check, first chunk check (budget
                # fine), second chunk check (budget spent mid-read), retry
                # decision (spent, so no second attempt).
                side_effect=[0.0, 0.0, 1.0, 12.0, 13.0],
            ),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(
                    config,
                    messages=[],
                    forbidden_values=(),
                    budget_seconds=10.0,
                )

        self.assertEqual(str(caught.exception), "hosted ingest failed: TimeoutError")
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_post_source_messages_urlopen_timeout_capped_by_remaining_budget(
        self,
    ) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __init__(self) -> None:
                self._body = io.BytesIO(b'{"items":[]}')

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, size: int = -1) -> bytes:
                return self._body.read(size)

        timeouts: list[float] = []

        def fake_urlopen(request, timeout):
            timeouts.append(timeout)
            return _Response()

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch(
                "vexic.recorders.hosted_ingest.time.monotonic",
                # started, attempt-1 pre-check (2s of budget left, so the
                # 30s per-request timeout must shrink to the remaining 2s),
                # then one budget check per body chunk read.
                side_effect=[0.0, 8.0, 8.5, 8.5],
            ),
        ):
            result = post_source_messages(
                config,
                messages=[],
                forbidden_values=(),
                budget_seconds=10.0,
            )

        self.assertEqual(result, {"items": []})
        self.assertEqual(timeouts, [2.0])

    def test_post_source_messages_retries_response_read_timeout_then_succeeds(
        self,
    ) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __init__(self, *, fail: bool) -> None:
                self._fail = fail

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                if self._fail:
                    raise TimeoutError("read timed out")
                return b'{"items":[]}'

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            return _Response(fail=len(calls) == 1)

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            result = post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(result, {"items": []})
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(0.5)

    def test_post_source_messages_retries_incomplete_read_then_succeeds(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __init__(self, *, fail: bool) -> None:
                self._fail = fail

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                if self._fail:
                    raise IncompleteRead(partial=b"{")
                return b'{"items":[]}'

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            return _Response(fail=len(calls) == 1)

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            result = post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(result, {"items": []})
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once_with(0.5)

    def test_post_source_messages_invalid_json_body_exhausts_to_transport_error(
        self,
    ) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                # A proxy-truncated 200 body: parse fails on every attempt.
                return b"<html>truncated"

        calls: list[int] = []

        def fake_urlopen(request, timeout):
            calls.append(1)
            return _Response()

        with (
            patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
            patch("vexic.recorders.hosted_ingest.time.sleep") as sleep_mock,
            patch("vexic.recorders.hosted_ingest.random.uniform", return_value=1.0),
        ):
            with self.assertRaises(HostedIngestTransportError) as caught:
                post_source_messages(config, messages=[], forbidden_values=())

        # The parse failure surfaces only the exception type name, never the
        # response body, so a misbehaving proxy body cannot leak into status.
        self.assertEqual(str(caught.exception), "hosted ingest failed: JSONDecodeError")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep_mock.call_args_list, [call(0.5), call(1.0)])

    def test_write_status_stamps_written_at_and_pid(self) -> None:
        from datetime import datetime

        with tempfile.TemporaryDirectory() as temp:
            status_path = Path(temp) / "status.json"
            write_status(
                status_path,
                RecorderStatus(
                    ok=True,
                    operation="ingest",
                    source_session_id="session-1",
                    transcript_path=None,
                ),
            )

            payload = json.loads(status_path.read_text(encoding="utf-8"))
            written_at = datetime.fromisoformat(payload["written_at"])
            self.assertIsNotNone(written_at.tzinfo)
            self.assertEqual(payload["pid"], os.getpid())

    def test_write_status_preserves_explicit_written_at_and_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status_path = Path(temp) / "status.json"
            write_status(
                status_path,
                RecorderStatus(
                    ok=True,
                    operation="ingest",
                    source_session_id="session-1",
                    transcript_path=None,
                    written_at="2026-07-18T00:00:00+00:00",
                    pid=12345,
                ),
            )

            payload = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["written_at"], "2026-07-18T00:00:00+00:00")
            self.assertEqual(payload["pid"], 12345)

    def test_write_status_does_not_leak_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status_path = Path(temp) / "status.json"
            write_status(
                status_path,
                RecorderStatus(
                    ok=False,
                    operation="ingest",
                    source_session_id="session-1",
                    transcript_path="C:/tmp/session.jsonl",
                    inserted=1,
                    skipped=2,
                    rejected=3,
                    ignored=4,
                    error="hosted ingest failed: HTTP 403",
                ),
            )
            payload = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "ingest")
        self.assertEqual(payload["inserted"], 1)
        self.assertEqual(payload["skipped"], 2)
        self.assertEqual(payload["rejected"], 3)
        self.assertEqual(payload["ignored"], 4)
        self.assertNotIn("vx_secret", json.dumps(payload))

    def test_write_status_is_atomic_via_temp_file_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status_dir = Path(temp)
            status_path = status_dir / "status.json"
            captured: list[tuple[str, str]] = []
            real_replace = os.replace

            def spy_replace(src, dst):
                captured.append((str(src), str(dst)))
                return real_replace(src, dst)

            with patch("vexic.recorders.status.os.replace", spy_replace):
                write_status(
                    status_path,
                    RecorderStatus(
                        ok=True,
                        operation="ingest",
                        source_session_id="session-1",
                        transcript_path="/tmp/session.jsonl",
                    ),
                )

            # The final write lands via a temp file renamed onto the target, so
            # an overlapping async run can never observe a half-written status.
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0][1], str(status_path))
            self.assertNotEqual(captured[0][0], str(status_path))
            self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["ok"])
            leftovers = [p.name for p in status_dir.iterdir() if p.name != "status.json"]
            self.assertEqual(leftovers, [])

    def test_write_status_failed_replace_leaves_target_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status_dir = Path(temp)
            status_path = status_dir / "status.json"
            status_path.write_text('{"ok": true, "prior": "value"}\n', encoding="utf-8")

            with patch(
                "vexic.recorders.status.os.replace",
                side_effect=OSError("rename failed"),
            ):
                with self.assertRaises(OSError):
                    write_status(
                        status_path,
                        RecorderStatus(
                            ok=False,
                            operation="ingest",
                            source_session_id="session-1",
                            transcript_path="/tmp/session.jsonl",
                        ),
                    )

            # A failed atomic replace must not corrupt the prior status file or
            # strand a temp file in the directory.
            self.assertEqual(
                status_path.read_text(encoding="utf-8"), '{"ok": true, "prior": "value"}\n'
            )
            leftovers = [p.name for p in status_dir.iterdir() if p.name != "status.json"]
            self.assertEqual(leftovers, [])


class ClaudeCodeRecorderIngestCommandTests(unittest.TestCase):
    def test_ingest_from_hook_payload_posts_clean_rows_and_writes_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "claude-session",
                                "uuid": "uuid-1",
                                "message": {"role": "user", "content": "remember cedar"},
                            }
                        ),
                        json.dumps({"type": "summary", "summary": "ignore cedar"}),
                    ]
                ),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "claude-session",
                        "transcript_path": str(transcript),
                    }
                ),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            calls = []

            def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
                calls.append((config, messages, forbidden_values))
                return {
                    "items": [
                        {
                            "source_host": "claude-code",
                            "source_session_id": "claude-session",
                            "source_message_id": "uuid-1",
                            "status": "inserted",
                        }
                    ]
                }

            with patch("vexic.recorders.cli.post_source_messages", fake_post):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--agent-id",
                        "agent-a",
                        "--status-path",
                        str(status_path),
                    ]
                )

            self.assertEqual(code, 0)
            config, messages, forbidden_values = calls[0]
            self.assertEqual(config.session_id, "vexic-session")
            self.assertEqual(config.agent_id, "agent-a")
            self.assertEqual(forbidden_values, ())
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].source_message_id, "uuid-1")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(status["ok"])
            self.assertEqual(status["inserted"], 1)
            self.assertEqual(status["ignored"], 1)

    def test_ingest_reads_stdin_as_utf8_bytes_not_locale_decoded(self) -> None:
        # Regression: on Windows under `uv run`, sys.stdin decodes as
        # cp1252 + surrogateescape, so any payload char whose UTF-8 encoding
        # contains a cp1252-undefined byte (e.g. U+1041 -> E1 81 81) becomes a
        # lone surrogate that model_validate_json rejects with string_unicode.
        # The hook must read raw stdin bytes and let pydantic decode UTF-8.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-session",
                        "uuid": "uuid-1",
                        "message": {"role": "user", "content": "remember cedar"},
                    }
                ),
                encoding="utf-8",
            )
            # ensure_ascii=False so the U+1041 char is emitted as raw UTF-8
            # bytes (E1 81 81) on the wire, not an ASCII \u escape — that is
            # what triggers the cp1252+surrogateescape mis-decode.
            payload_bytes = json.dumps(
                {
                    "session_id": "claude-session၁",
                    "transcript_path": str(transcript),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.assertIn(b"\xe1\x81\x81", payload_bytes)

            class _WindowsLikeStdin:
                def __init__(self, data: bytes) -> None:
                    self.buffer = io.BytesIO(data)
                    self._data = data

                def read(self) -> str:
                    # Mirror real uv-run Windows stdin text decoding.
                    return self._data.decode("cp1252", "surrogateescape")

            def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
                return _ingest_result(messages)

            with (
                patch("vexic.recorders.cli.sys.stdin", _WindowsLikeStdin(payload_bytes)),
                patch("vexic.recorders.cli.post_source_messages", fake_post),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                    ]
                )

            self.assertEqual(code, 0)

    def test_ingest_batches_hosted_posts_at_one_hundred_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            rows = [
                {
                    "type": "user",
                    "sessionId": "claude-session",
                    "uuid": f"uuid-{index}",
                    "message": {"role": "user", "content": f"remember cedar {index}"},
                }
                for index in range(205)
            ]
            rows.append({"type": "summary", "summary": "ignore cedar"})
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps({"session_id": "claude-session", "transcript_path": str(transcript)}),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            calls = []

            def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
                calls.append(messages)
                if len(calls) == 1:
                    return _ingest_result(messages)
                if len(calls) == 2:
                    return _ingest_result(
                        messages,
                        ["skipped"] * (len(messages) - 1) + ["rejected"],
                    )
                return _ingest_result(messages)

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.cli.post_source_messages", fake_post),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--status-path",
                        str(status_path),
                    ]
                )

            output = json.loads(stdout.getvalue())
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual([len(batch) for batch in calls], [100, 100, 5])
            self.assertEqual(output["inserted"], 105)
            self.assertEqual(output["skipped"], 99)
            self.assertEqual(output["rejected"], 1)
            self.assertEqual(output["ignored"], 1)
            self.assertEqual(status["inserted"], 105)
            self.assertEqual(status["skipped"], 99)
            self.assertEqual(status["rejected"], 1)
            self.assertEqual(status["ignored"], 1)

    def test_ingest_deadline_expiry_fails_open_between_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            rows = [
                {
                    "type": "user",
                    "sessionId": "claude-session",
                    "uuid": f"uuid-{index}",
                    "message": {"role": "user", "content": f"remember cedar {index}"},
                }
                for index in range(101)
            ]
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "session_id": "claude-session",
                        "transcript_path": str(transcript),
                    }
                ),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            calls = []
            budgets: list[float | None] = []

            def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
                calls.append(messages)
                budgets.append(budget_seconds)
                return _ingest_result(messages)

            argv = [
                "ingest",
                "--hook-input",
                str(hook_payload),
                "--base-url",
                "https://api.example.test",
                "--api-key",
                "vx_secret",
                "--project-id",
                "project-a",
                "--session-id",
                "vexic-session",
                "--status-path",
                str(status_path),
            ]

            stderr = io.StringIO()
            with (
                patch("vexic.recorders.cli.post_source_messages", fake_post),
                patch(
                    "vexic.recorders.cli.time.monotonic",
                    # started, batch-1 deadline check, batch-2 deadline check:
                    # the 100s default is spent while batch 1 posts, so batch 2
                    # must never be attempted.
                    side_effect=[0.0, 0.0, 150.0],
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(argv)

            self.assertEqual(code, 1)
            self.assertEqual([len(batch) for batch in calls], [100])
            # The full remaining deadline flows into the batch as its retry
            # budget.
            self.assertEqual(budgets, [100.0])
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertIn("deadline", status["error"])
            self.assertIn("warning:", stderr.getvalue())

            # A rerun with a live clock re-posts every row; the hosted ledger
            # dedups the 100 rows batch 1 already delivered.
            calls.clear()
            with (
                patch("vexic.recorders.cli.post_source_messages", fake_post),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(recorder_main(argv), 0)
            self.assertEqual([len(batch) for batch in calls], [100, 1])

    def test_ingest_deadline_flag_rejects_non_positive_values(self) -> None:
        # recorder_main converts the argparse SystemExit into a return code.
        for value in ("0", "-1"):
            with self.subTest(deadline=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = recorder_main(
                        [
                            "ingest",
                            "--deadline-seconds",
                            value,
                        ]
                    )
                self.assertEqual(code, 2)

    def test_ingest_deadline_flag_warns_when_at_or_above_hook_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "session_id": "claude-session",
                        "transcript_path": str(transcript),
                    }
                ),
                encoding="utf-8",
            )

            def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
                return _ingest_result(messages)

            stderr = io.StringIO()
            with (
                patch("vexic.recorders.cli.post_source_messages", fake_post),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--deadline-seconds",
                        "116",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("margin", stderr.getvalue())
            self.assertIn("Stop hook kill", stderr.getvalue())

    def test_ingest_batches_hosted_posts_before_payload_char_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "session_id": "claude-session",
                        "transcript_path": str(root / "session.jsonl"),
                    }
                ),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            messages = [
                SourceTranscriptMessage(
                    source_host="claude-code",
                    source_session_id="claude-session",
                    source_message_id="uuid-1",
                    message_json="a" * (HOSTED_WRITE_MAX_CHARS - 10),
                ),
                SourceTranscriptMessage(
                    source_host="claude-code",
                    source_session_id="claude-session",
                    source_message_id="uuid-2",
                    message_json="b" * 20,
                ),
                SourceTranscriptMessage(
                    source_host="claude-code",
                    source_session_id="claude-session",
                    source_message_id="uuid-3",
                    message_json="c" * 10,
                ),
            ]
            calls = []

            def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
                calls.append(messages)
                return _ingest_result(messages)

            with (
                patch(
                    "vexic.recorders.cli.scan_claude_code_transcript",
                    return_value=TranscriptScan(
                        messages=messages, ignored=0, cursor=None, resumed=False
                    ),
                ),
                patch("vexic.recorders.cli.post_source_messages", fake_post),
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--status-path",
                        str(status_path),
                    ]
                )

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual([len(batch) for batch in calls], [1, 2])
            self.assertTrue(
                all(
                    sum(len(message.message_json) for message in batch)
                    <= HOSTED_WRITE_MAX_CHARS
                    for batch in calls
                )
            )
            self.assertEqual(status["inserted"], 3)

    def test_ingest_rejects_late_oversized_message_before_any_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "session_id": "claude-session",
                        "transcript_path": str(root / "session.jsonl"),
                    }
                ),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            messages = [
                SourceTranscriptMessage(
                    source_host="claude-code",
                    source_session_id="claude-session",
                    source_message_id=f"uuid-{index}",
                    message_json="x",
                )
                for index in range(101)
            ]
            messages.append(
                SourceTranscriptMessage(
                    source_host="claude-code",
                    source_session_id="claude-session",
                    source_message_id="uuid-oversize",
                    message_json="x" * (HOSTED_WRITE_MAX_CHARS + 1),
                )
            )

            with (
                patch(
                    "vexic.recorders.cli.scan_claude_code_transcript",
                    return_value=TranscriptScan(
                        messages=messages, ignored=0, cursor=None, resumed=False
                    ),
                ),
                patch("vexic.recorders.cli.post_source_messages") as post_source_messages_mock,
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--status-path",
                        str(status_path),
                    ]
                )

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 2)
            self.assertFalse(status["ok"])
            self.assertIn("exceeds hosted ingest payload cap", status["error"])
            post_source_messages_mock.assert_not_called()


class ClaudeCodeRecorderHostedRoundTripTests(unittest.TestCase):
    def test_ingest_cli_posts_to_hosted_http_and_search_finds_cleaned_row(self) -> None:
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
            client = TestClient(create_app(HostedMemoryService(catalog, keys, telemetry=catalog)))
            transcript = root / "claude-session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "claude-source-session",
                                "uuid": "source-message-1",
                                "message": {
                                    "role": "user",
                                    "content": "remember hosted-orchid",
                                },
                            }
                        ),
                        json.dumps({"type": "summary", "summary": "ignore hosted-orchid"}),
                    ]
                ),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "session_id": "claude-source-session",
                        "transcript_path": str(transcript),
                    }
                ),
                encoding="utf-8",
            )
            status_path = root / "status.json"

            class _Response:
                def __init__(self, content: bytes):
                    self._body = io.BytesIO(content)

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self, size: int = -1) -> bytes:
                    return self._body.read(size)

            def fake_urlopen(request, timeout):
                target = urlsplit(request.full_url)
                path = target.path
                if target.query:
                    path = f"{path}?{target.query}"
                response = client.request(
                    request.get_method(),
                    path,
                    headers=dict(request.header_items()),
                    content=request.data,
                )
                if not 200 <= response.status_code < 300:
                    raise HTTPError(
                        request.full_url,
                        response.status_code,
                        response.reason_phrase,
                        response.headers,
                        io.BytesIO(response.content),
                    )
                return _Response(response.content)

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://testserver",
                        "--api-key",
                        api_key,
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                        "--status-path",
                        str(status_path),
                    ]
                )

            search_response = client.post(
                "/v1/search_transcript",
                headers={"Authorization": f"Bearer {api_key}"},
                json=SearchTranscriptRequest(
                    scope=MemoryScope(
                        tenant_id="tenant-a",
                        project_id="project-a",
                        session_id="session-a",
                        principal=Principal(
                            principal_id="caller-supplied",
                            principal_type=PrincipalType.HUMAN,
                        ),
                        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
                        capabilities={MemoryCapability.SEARCH},
                    ),
                    query="hosted-orchid",
                ).model_dump(mode="json"),
            )

            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(output["inserted"], 1)
            self.assertEqual(output["ignored"], 1)
            self.assertTrue(status["ok"])
            self.assertEqual(status["inserted"], 1)
            self.assertEqual(status["ignored"], 1)
            self.assertEqual(search_response.status_code, 200)
            self.assertEqual(
                [hit["body"] for hit in search_response.json()["hits"]],
                ["User: remember hosted-orchid"],
            )


class ClaudeCodeSetupTests(unittest.TestCase):
    def test_setup_merges_user_settings_without_raw_secret_in_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "echo existing",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id="agent-a",
                command="python -m vexic.cli recorder ingest",
            )

            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            stop_groups = settings["hooks"]["Stop"]
            commands = [
                hook["command"]
                for group in stop_groups
                for hook in group["hooks"]
            ]
            self.assertIn("echo existing", commands)
            vexic_commands = [command for command in commands if "vexic" in command]
            self.assertEqual(len(vexic_commands), 1)
            self.assertNotIn("vx_secret", vexic_commands[0])
            self.assertIn(str(result.config_path).replace("\\", "/"), vexic_commands[0])
            config = json.loads(result.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["api_key"], "vx_secret")
            self.assertEqual(config["agent_id"], "agent-a")
            # The opt-in connect command names the launcher + creds path only.
            self.assertTrue(result.connect_command.startswith("claude mcp add vexic -- "))
            self.assertIn(str(result.config_path).replace("\\", "/"), result.connect_command)
            self.assertNotIn("vx_secret", result.connect_command)

    def test_setup_connect_command_uses_home_relative_recorder_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)

            with patch("vexic.recorders.claude_setup.Path.home", return_value=home):
                result = install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                )

            self.assertIn(
                "~/.vexic/claude-code-recorder.json", result.connect_command
            )
            self.assertNotIn(str(home), result.connect_command)
            self.assertTrue(result.config_path.exists())

    def test_setup_prints_opt_in_connect_command_and_writes_no_mcp_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
            )

            # ADR 0027: the connect step is the user running the printed command.
            self.assertTrue(result.connect_command.startswith("claude mcp add vexic -- "))
            self.assertNotIn("vx_secret", result.connect_command)
            self.assertFalse((project_root / ".mcp.json").exists())
            settings = json.loads(result.settings_path.read_text(encoding="utf-8"))
            self.assertNotIn("disabledMcpjsonServers", settings)
            self.assertNotIn("enabledMcpjsonServers", settings)

    def test_setup_connect_command_launcher_runs_outside_vexic_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "customer-project"
            project_root.mkdir()

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
            )

            # Strip the `claude mcp add vexic --` prefix to recover the launcher
            # argv and prove it starts (and reads creds) from any working dir.
            import shlex as _shlex

            parts = _shlex.split(result.connect_command)
            separator = parts.index("--")
            launcher_argv = parts[separator + 1 :]
            repo_root = Path(__file__).resolve().parents[1]
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                [str(repo_root / "src"), env.get("PYTHONPATH", "")]
            )
            completed = subprocess.run(
                launcher_argv,
                input="",
                text=True,
                cwd=project_root,
                env=env,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_setup_writes_config_owner_only_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            probe = home / "probe"
            probe.write_text("", encoding="utf-8")
            probe.chmod(0o600)
            if stat.S_IMODE(probe.stat().st_mode) != 0o600:
                self.skipTest("filesystem does not report owner-only file mode")

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
            )

            self.assertEqual(stat.S_IMODE(result.config_path.stat().st_mode), 0o600)

    def test_setup_restricts_config_acl_to_owner_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows ACL enforcement")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
            )

            # chmod mode bits are cosmetic on NT; the DACL is the real control.
            listing = subprocess.run(
                ["icacls", str(result.config_path)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            ace_lines = [line for line in listing.splitlines() if ":(" in line]
            self.assertEqual(len(ace_lines), 1, listing)
            self.assertIn("(F)", ace_lines[0])

    def test_setup_rejects_blank_base_url_before_writing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)

            with self.assertRaisesRegex(ValueError, "base_url must be nonblank"):
                install_claude_code_setup(
                    home=home,
                    base_url="   ",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                )

            self.assertFalse((home / ".vexic" / "claude-code-recorder.json").exists())
            self.assertFalse((home / ".claude" / "settings.json").exists())

    def test_setup_fails_if_config_permissions_cannot_be_hardened(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)

            with patch("pathlib.Path.chmod", side_effect=OSError("chmod denied")):
                with self.assertRaisesRegex(PermissionError, "owner-only permissions"):
                    install_claude_code_setup(
                        home=home,
                        base_url="https://api.example.test",
                        api_key="vx_secret",
                        project_id="project-a",
                        session_id="session-a",
                        agent_id=None,
                        command="python -m vexic.cli recorder ingest",
                    )

            self.assertFalse((home / ".vexic" / "claude-code-recorder.json").exists())
            self.assertFalse((home / ".claude" / "settings.json").exists())

    def test_setup_secret_write_failure_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()

            with patch("pathlib.Path.chmod", side_effect=OSError("chmod denied")):
                with self.assertRaisesRegex(PermissionError, "owner-only permissions"):
                    install_claude_code_setup(
                        home=home,
                        base_url="https://api.example.test",
                        api_key="vx_secret",
                        project_id="project-a",
                        session_id="session-a",
                        agent_id=None,
                        command="python -m vexic.cli recorder ingest",
                    )

            self.assertFalse((project_root / ".mcp.json").exists())
            self.assertFalse((home / ".vexic" / "claude-code-recorder.json").exists())
            self.assertFalse((home / ".claude" / "settings.json").exists())

    def test_setup_quotes_config_path_with_spaces_in_hook(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vexic home ") as temp:
            home = Path(temp)

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
            )

            settings = json.loads(result.settings_path.read_text(encoding="utf-8"))
            command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
            config_path = str(result.config_path).replace("\\", "/")
            self.assertIn(config_path, command)
            self.assertIn(f"--config '{config_path}'", command)
            self.assertNotIn("vx_secret", command)

    def test_setup_writes_bash_safe_windows_hook_command(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows hook command escaping only")
        with tempfile.TemporaryDirectory(prefix="vexic home ") as temp:
            home = Path(temp)

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command=(
                    "C:\\Users\\Ryan\\.local\\bin\\uv.exe run --with-editable "
                    "C:\\Users\\Ryan\\Documents\\GitHub\\Vexic "
                    "python -m vexic.cli recorder ingest"
                ),
            )

            settings = json.loads(result.settings_path.read_text(encoding="utf-8"))
            hook = settings["hooks"]["Stop"][0]["hooks"][0]
            command = hook["command"]
            self.assertIn("C:/Users/user/.local/bin/uv.exe", command)
            self.assertIn(str(result.config_path).replace("\\", "/"), command)
            self.assertNotIn("\\", command)
            self.assertTrue(hook["async"])

    def test_setup_rerun_upgrades_sync_stop_hook_to_async(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            # Pre-seed an old-style synchronous vexic Stop hook, as an install
            # made before this fix would have left behind.
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python -m vexic.cli recorder ingest",
                                            "async": False,
                                            "timeout": 120,
                                            "vexicHookId": "vexic-claude-code-recorder",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            code = vexic_main(
                [
                    "setup",
                    "claude-code",
                    "--home",
                    str(home),
                    "--base-url",
                    "https://api.example.test",
                    "--api-key",
                    "vx_secret",
                    "--project-id",
                    "project-a",
                    "--session-id",
                    "session-a",
                ]
            )
            self.assertEqual(code, 0)

            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            stop_hooks = [
                hook
                for group in settings["hooks"]["Stop"]
                for hook in group["hooks"]
                if hook.get("vexicHookId") == "vexic-claude-code-recorder"
            ]
            self.assertEqual(len(stop_hooks), 1)
            self.assertTrue(stop_hooks[0]["async"])
            self.assertEqual(stop_hooks[0]["timeout"], 120)
            self.assertNotIn("asyncRewake", stop_hooks[0])

            session_start_hooks = [
                hook
                for group in settings["hooks"]["SessionStart"]
                for hook in group["hooks"]
                if hook.get("vexicHookId") == "vexic-claude-code-recorder"
            ]
            self.assertEqual(len(session_start_hooks), 1)
            self.assertFalse(session_start_hooks[0]["async"])

    def test_setup_is_idempotent(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()
            for _ in range(2):
                code = vexic_main(
                    [
                        "setup",
                        "claude-code",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )
                self.assertEqual(code, 0)

            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for group in settings["hooks"]["Stop"]
                for hook in group["hooks"]
                if "vexic" in hook["command"]
            ]
            self.assertEqual(len(commands), 1)
            self.assertFalse((project_root / ".mcp.json").exists())

    def test_setup_installs_session_start_prime_hook_idempotently(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()

            for _ in range(2):
                code = vexic_main(
                    [
                        "setup",
                        "claude-code",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )
                self.assertEqual(code, 0)

            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            prime_hooks = [
                hook
                for group in settings["hooks"]["SessionStart"]
                for hook in group["hooks"]
                if hook.get("vexicHookId") == "vexic-claude-code-recorder"
            ]

            self.assertEqual(len(prime_hooks), 1)
            self.assertEqual(prime_hooks[0]["type"], "command")
            self.assertIn("recorder prime", prime_hooks[0]["command"])
            self.assertIn("--config", prime_hooks[0]["command"])
            self.assertNotIn("vx_secret", prime_hooks[0]["command"])

    def test_uninstall_removes_only_vexic_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
            )
            settings_path = home / ".claude" / "settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["hooks"]["Stop"].append(
                {"hooks": [{"type": "command", "command": "echo keep"}]}
            )
            settings["hooks"]["SessionStart"].append(
                {"hooks": [{"type": "command", "command": "echo keep session"}]}
            )
            settings_path.write_text(json.dumps(settings), encoding="utf-8")

            removed = uninstall_claude_code_setup(home=home)

            self.assertTrue(removed)
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for group in settings["hooks"]["Stop"]
                for hook in group["hooks"]
            ]
            self.assertEqual(commands, ["echo keep"])
            session_start_commands = [
                hook["command"]
                for group in settings["hooks"]["SessionStart"]
                for hook in group["hooks"]
            ]
            self.assertEqual(session_start_commands, ["echo keep session"])

    def test_uninstall_leaves_non_vexic_stop_data_unchanged(self) -> None:
        cases = [
            {"hooks": {}},
            {"hooks": {"Stop": "malformed"}},
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo keep"}]}]}},
        ]
        for initial_settings in cases:
            with self.subTest(initial_settings=initial_settings):
                with tempfile.TemporaryDirectory() as temp:
                    home = Path(temp)
                    settings_path = home / ".claude" / "settings.json"
                    settings_path.parent.mkdir(parents=True)
                    settings_path.write_text(json.dumps(initial_settings), encoding="utf-8")

                    removed = uninstall_claude_code_setup(home=home)

                    self.assertFalse(removed)
                    self.assertEqual(
                        json.loads(settings_path.read_text(encoding="utf-8")),
                        initial_settings,
                    )

    def test_top_level_setup_claude_code_dispatches(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()
            code = vexic_main(
                [
                    "setup",
                    "claude-code",
                    "--home",
                    str(home),
                    "--base-url",
                    "https://api.example.test",
                    "--api-key",
                    "vx_secret",
                    "--project-id",
                    "project-a",
                    "--session-id",
                    "session-a",
                ]
            )

            self.assertEqual(code, 0)
            self.assertTrue((home / ".claude" / "settings.json").exists())
            self.assertFalse((project_root / ".mcp.json").exists())

    def test_top_level_setup_uses_stable_uv_launcher_not_setup_python(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()
            setup_python = home / "uv-cache" / ".tmpdead" / "Scripts" / "python.exe"
            uv_path = home / "bin" / "uv.exe"

            stdout = io.StringIO()
            with (
                patch("sys.executable", str(setup_python)),
                patch("shutil.which", return_value=str(uv_path)),
                contextlib.redirect_stdout(stdout),
            ):
                code = vexic_main(
                    [
                        "setup",
                        "claude-code",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )

            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            hook_command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
            connect_command = json.loads(stdout.getvalue())["connect_command"]
            repo_root = str(Path(__file__).resolve().parents[1])

            self.assertEqual(code, 0)
            self.assertNotIn(str(setup_python).replace("\\", "/"), hook_command)
            self.assertIn(str(uv_path).replace("\\", "/"), hook_command)
            self.assertIn("run --with-editable", hook_command)
            self.assertIn(repo_root.replace("\\", "/"), hook_command)
            self.assertIn(str(uv_path).replace("\\", "/"), connect_command)
            self.assertIn("--with-editable", connect_command)
            self.assertIn(f"{repo_root}[local-embed]", connect_command)
            self.assertNotIn(str(setup_python).replace("\\", "/"), connect_command)
            self.assertFalse((project_root / ".mcp.json").exists())

    def test_top_level_setup_rejects_missing_uv_before_writing_setup_files(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()
            stderr = io.StringIO()

            with (
                patch("shutil.which", return_value=None),
                contextlib.redirect_stderr(stderr),
            ):
                code = vexic_main(
                    [
                        "setup",
                        "claude-code",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )

            self.assertEqual(code, 2)
            self.assertIn("uv executable was not found", stderr.getvalue())
            self.assertFalse((home / ".claude" / "settings.json").exists())
            self.assertFalse((home / ".vexic" / "claude-code-recorder.json").exists())
            self.assertFalse((project_root / ".mcp.json").exists())

    def test_setup_pip_install_hooks_use_setup_python_module_invocation(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()

            with patch("vexic.recorders.claude_setup._repo_root", return_value=None):
                code = vexic_main(
                    [
                        "setup",
                        "claude-code",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                    ]
                )

            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            stop_command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
            prime_command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            python = sys.executable.replace("\\", "/")

            self.assertEqual(code, 0)
            self.assertTrue(
                stop_command.startswith(f"{python} -m vexic.cli recorder ingest"),
                stop_command,
            )
            self.assertTrue(
                prime_command.startswith(f"{python} -m vexic.cli recorder prime"),
                prime_command,
            )
            for command in (stop_command, prime_command):
                self.assertNotIn("--with-editable", command)
                self.assertNotIn("uv run", command)

    def test_pip_install_hook_command_quotes_interpreter_path_with_spaces(self) -> None:
        import shlex

        from vexic.recorders.claude_setup import default_recorder_hook_command

        with (
            patch("vexic.recorders.claude_setup._repo_root", return_value=None),
            patch("sys.executable", "C:\\Program Files\\Python 3.12\\python.exe"),
        ):
            command = default_recorder_hook_command()

        parts = shlex.split(command)
        self.assertEqual(parts[0], "C:/Program Files/Python 3.12/python.exe")
        self.assertEqual(parts[1:], ["-m", "vexic.cli", "recorder", "ingest"])

    def test_setup_pip_install_connect_command_uses_module_invocation_without_uv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()

            with (
                patch("vexic.recorders.claude_setup._repo_root", return_value=None),
                patch("shutil.which", return_value=None),
            ):
                result = install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                )

            import shlex as _shlex

            expected_launcher = _shlex.join(
                part.replace("\\", "/")
                for part in [
                    sys.executable,
                    "-m",
                    "vexic.mcp_stdio_main",
                    "--recorder-config",
                    str(result.config_path),
                ]
            )
            self.assertEqual(
                result.connect_command,
                f"claude mcp add vexic -- {expected_launcher}",
            )
            self.assertNotIn("uv run", result.connect_command)
            self.assertNotIn("vx_secret", result.connect_command)
            self.assertFalse((project_root / ".mcp.json").exists())

    def test_uninstall_removes_pip_install_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)

            with (
                patch("vexic.recorders.claude_setup._repo_root", return_value=None),
                patch("shutil.which", return_value=None),
            ):
                install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                )

                removed = uninstall_claude_code_setup(home=home)

            self.assertTrue(removed)
            settings = json.loads(
                (home / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(settings["hooks"]["Stop"], [])
            self.assertEqual(settings["hooks"]["SessionStart"], [])

    def test_setup_rejects_non_derivable_hook_command_cleanly(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                code = vexic_main(
                    [
                        "setup",
                        "claude-code",
                        "--home",
                        str(home),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                        "--hook-command",
                        "custom-recorder",
                    ]
                )

            self.assertEqual(code, 2)
            self.assertIn("prime_command is required", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse((home / ".claude" / "settings.json").exists())

    def test_ingest_uses_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-session",
                        "uuid": "uuid-1",
                        "message": {"role": "user", "content": "remember cedar"},
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps({"session_id": "claude-session", "transcript_path": str(transcript)}),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "agent_id": "agent-a",
                        "status_path": str(status_path),
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            def fake_post(config, *, messages, forbidden_values, budget_seconds=None):
                calls.append((config, messages, forbidden_values))
                return _ingest_result(messages)

            with patch("vexic.recorders.cli.post_source_messages", fake_post):
                code = recorder_main(
                    [
                        "ingest",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                    ]
                )

            self.assertEqual(code, 0)
            config, messages, _forbidden_values = calls[0]
            self.assertEqual(config.base_url, "https://api.example.test")
            self.assertEqual(config.api_key, "vx_secret")
            self.assertEqual(config.project_id, "project-a")
            self.assertEqual(config.session_id, "session-a")
            self.assertEqual(config.agent_id, "agent-a")
            self.assertEqual(len(messages), 1)
            self.assertTrue(status_path.exists())


class ClaudeCodeRecorderIngestCommandMoreTests(unittest.TestCase):
    def _run_ingest_with_failing_post(
        self, root: Path, status_path: Path, error: Exception
    ) -> tuple[int, str]:
        transcript = root / "session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "claude-session",
                    "uuid": "uuid-1",
                    "message": {"role": "user", "content": "remember cedar"},
                }
            ),
            encoding="utf-8",
        )
        hook_payload = root / "hook.json"
        hook_payload.write_text(
            json.dumps({"session_id": "claude-session", "transcript_path": str(transcript)}),
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with (
            patch("vexic.recorders.cli.post_source_messages", side_effect=error),
            contextlib.redirect_stderr(stderr),
        ):
            code = recorder_main(
                [
                    "ingest",
                    "--hook-input",
                    str(hook_payload),
                    "--base-url",
                    "https://api.example.test",
                    "--api-key",
                    "vx_secret",
                    "--project-id",
                    "project-a",
                    "--session-id",
                    "vexic-session",
                    "--status-path",
                    str(status_path),
                ]
            )
        return code, stderr.getvalue()

    def test_ingest_transport_failure_warns_and_returns_one(self) -> None:
        from vexic.recorders.hosted_ingest import HostedIngestTransportError

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "status.json"
            code, stderr = self._run_ingest_with_failing_post(
                root,
                status_path,
                HostedIngestTransportError("hosted ingest failed: HTTP 503"),
            )

            self.assertEqual(code, 1)
            self.assertIn("warning: hosted ingest failed: HTTP 503", stderr)
            self.assertNotIn("error:", stderr)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"], "hosted ingest failed: HTTP 503")
            self.assertEqual(status["source_session_id"], "claude-session")
            self.assertEqual(status["transcript_path"], str(root / "session.jsonl"))
            self.assertNotIn("vx_secret", json.dumps(status))

    def test_ingest_auth_failure_still_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "status.json"
            code, stderr = self._run_ingest_with_failing_post(
                root,
                status_path,
                RuntimeError("hosted ingest failed: HTTP 403"),
            )

            self.assertEqual(code, 2)
            self.assertIn("error: hosted ingest failed: HTTP 403", stderr)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"], "hosted ingest failed: HTTP 403")
            self.assertEqual(status["source_session_id"], "claude-session")
            self.assertEqual(status["transcript_path"], str(root / "session.jsonl"))
            self.assertNotIn("vx_secret", json.dumps(status))

    def test_ingest_non_transport_failure_still_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "status.json"
            code, _stderr = self._run_ingest_with_failing_post(
                root,
                status_path,
                RuntimeError("something else broke"),
            )

            self.assertEqual(code, 2)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"], "something else broke")

    def test_ingest_status_write_failure_returns_two_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-session",
                        "uuid": "uuid-1",
                        "message": {"role": "user", "content": "remember cedar"},
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps({"session_id": "claude-session", "transcript_path": str(transcript)}),
                encoding="utf-8",
            )

            stderr = io.StringIO()
            with (
                patch(
                    "vexic.recorders.cli.post_source_messages",
                    side_effect=lambda _config, *, messages, forbidden_values, budget_seconds=None: (
                        _ingest_result(messages)
                    ),
                ),
                patch("vexic.recorders.cli.write_status", side_effect=OSError("disk full")),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--status-path",
                        str(root / "status.json"),
                    ]
                )

            self.assertEqual(code, 2)
            # The failure surfaced must be the status write itself, not an
            # incidental error swallowed on the way there.
            self.assertIn("status write failed: OSError", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_ingest_parse_error_writes_status_when_status_path_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps({"session_id": "claude-session", "transcript_path": "session.jsonl"}),
                encoding="utf-8",
            )
            status_path = root / "status.json"

            code = recorder_main(
                [
                    "ingest",
                    "--hook-input",
                    str(hook_payload),
                    "--base-url",
                    "https://api.example.test",
                    "--project-id",
                    "project-a",
                    "--session-id",
                    "session-a",
                    "--status-path",
                    str(status_path),
                ]
            )

            self.assertEqual(code, 2)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertEqual(status["operation"], "ingest")
            self.assertEqual(status["error"], "argument parsing failed")
            self.assertNotIn("vx_secret", json.dumps(status))

    def test_ingest_rejects_config_with_unknown_fields_before_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "session_id": "claude-session",
                        "transcript_path": str(root / "session.jsonl"),
                    }
                ),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "unexpected": "value",
                    }
                ),
                encoding="utf-8",
            )

            with patch("vexic.recorders.cli.post_source_messages") as post_source_messages_mock:
                code = recorder_main(
                    [
                        "ingest",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--status-path",
                        str(status_path),
                    ]
                )

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 2)
            self.assertIn("invalid recorder config", status["error"])
            post_source_messages_mock.assert_not_called()

    def test_ingest_rejects_malformed_hook_payload_before_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps({"session_id": 123, "transcript_path": str(root / "session.jsonl")}),
                encoding="utf-8",
            )
            status_path = root / "status.json"

            with patch("vexic.recorders.cli.post_source_messages") as post_source_messages_mock:
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--status-path",
                        str(status_path),
                    ]
                )

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 2)
            self.assertIn("invalid hook input", status["error"])
            post_source_messages_mock.assert_not_called()

    def test_top_level_recorder_dispatches_ingest(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-session",
                        "uuid": "uuid-1",
                        "message": {"role": "user", "content": "remember cedar"},
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps({"session_id": "claude-session", "transcript_path": str(transcript)}),
                encoding="utf-8",
            )

            with patch(
                "vexic.recorders.cli.post_source_messages",
                side_effect=lambda _config, *, messages, forbidden_values, budget_seconds=None: (
                    _ingest_result(messages)
                ),
            ):
                code = vexic_main(
                    [
                        "recorder",
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                    ]
                )

            self.assertEqual(code, 0)


class ClaudeCodeRecorderPrimeCommandTests(unittest.TestCase):
    def test_prime_malformed_session_start_payload_warns_without_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": 123}), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("invalid hook input", stderr.getvalue())

    def test_prime_invalid_config_warns_without_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "unexpected": "value",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.cli.fetch_prime_context") as fetch_prime_context_mock,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("invalid recorder config", stderr.getvalue())
            fetch_prime_context_mock.assert_not_called()

    def test_prime_resume_skips_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "agent_id": "agent-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "resume"}), encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")

    def test_prime_startup_emits_capped_context_from_hosted_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "agent_id": "agent-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            calls = []

            class _Response:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                calls.append((request, timeout))
                if request.full_url.endswith("/v1/fresh_context"):
                    return _Response(
                        {"summaries": [], "recent": [], "text": "", "truncated": False}
                    )
                if request.full_url.endswith("/v1/search_long_term"):
                    return _Response(
                        {
                            "facts": [
                                {
                                    "fact_id": 1,
                                    "fact_text": "Durable cedar preference",
                                    "subject": "user",
                                    "category": "preference",
                                    "importance": 5,
                                    "confidence": 0.9,
                                    "source_message_ids": [7],
                                    "editable": True,
                                    "created_at": "2026-06-29T00:00:00Z",
                                }
                            ],
                            "candidate_notes": [],
                        }
                    )
                return _Response(
                    {
                        "hits": [
                            {
                                "message_id": 3,
                                "session_id": "session-a",
                                "timestamp": "2026-06-29T00:00:01Z",
                                "body": "User: recent cedar note",
                            }
                        ]
                    }
                )

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--max-chars",
                        "600",
                    ]
                )

            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertLessEqual(len(context), 600)
            self.assertIn("Durable cedar preference", context)
            self.assertIn("recent cedar note", context)
            self.assertNotIn("vx_secret", stdout.getvalue())
            # Reads dispatch in parallel (ADR 0025 D4 follow-up), so arrival order is
            # nondeterministic; assert the set of endpoints, not the order.
            self.assertEqual(
                sorted(urlsplit(call[0].full_url).path for call in calls),
                ["/v1/fresh_context", "/v1/search_long_term", "/v1/search_transcript"],
            )
            for request, timeout in calls:
                self.assertEqual(timeout, 15.0)
                self.assertEqual(request.get_header("Authorization"), "Bearer vx_secret")
                self.assertEqual(request.get_header("X-vexic-project-id"), "project-a")
                self.assertEqual(request.get_header("X-vexic-session-id"), "session-a")
                self.assertEqual(request.get_header("X-vexic-agent-id"), "agent-a")

    def test_prime_context_advertises_memory_search_tools(self) -> None:
        context = build_prime_context(
            {"facts": [{"fact_text": "Durable cedar preference"}], "candidate_notes": []},
            {"hits": []},
            max_chars=6_000,
        )

        self.assertIn("Durable cedar preference", context)
        self.assertIn("vexic memory search tools", context)
        self.assertIn("Use this memory silently", context)

    def test_prime_framing_bridge_survives_end_truncation_at_small_budget(self) -> None:
        context = build_prime_context(
            {
                "facts": [{"fact_text": "long fact " * 200}],
                "candidate_notes": [],
            },
            {"hits": []},
            max_chars=300,
        )

        self.assertLessEqual(len(context), 300)
        self.assertTrue(context.startswith("Vexic memory priming:"))
        self.assertIn("Memory snapshot from prior sessions", context)
        self.assertIn("vexic recall tools reach them", context)

    def test_prime_core_guidance_sentence_complete_at_pathological_budget(self) -> None:
        context = build_prime_context(
            {
                "facts": [{"fact_text": "long fact " * 200}],
                "candidate_notes": [],
            },
            {"hits": []},
            max_chars=200,
        )

        self.assertLessEqual(len(context), 200)
        self.assertIn(
            "Memory snapshot from prior sessions — use it silently.",
            context,
            "core usage-guidance sentence must be complete even below the "
            "footer-reservation threshold",
        )

    def test_prime_footer_survives_worst_case_truncation(self) -> None:
        from vexic.recorders.hosted_prime import PRIME_FOOTER

        context = build_prime_context(
            {
                "facts": [{"fact_text": f"fact {i} " + "x" * 300} for i in range(30)],
                "candidate_notes": [],
            },
            {"hits": [{"body": f"hit {i} " + "y" * 300} for i in range(30)]},
            recap_text="recap " * 500,
            max_chars=6_000,
        )

        self.assertLessEqual(len(context), 6_000)
        self.assertTrue(
            context.endswith(PRIME_FOOTER),
            "reserved footer must be intact at the end under worst-case truncation",
        )

    def test_prime_below_reservation_threshold_prioritizes_content_over_footer(self) -> None:
        from vexic.recorders.hosted_prime import PRIME_FOOTER

        footer_block_len = len("\n" + PRIME_FOOTER)
        max_chars = 2 * footer_block_len - 100  # inside the dead band
        context = build_prime_context(
            {"facts": [{"fact_text": "long fact " * 200}], "candidate_notes": []},
            {"hits": []},
            max_chars=max_chars,
        )

        self.assertLessEqual(len(context), max_chars)
        # deliberate policy: below the reservation threshold, memory content
        # wins the budget and the footer may be truncated away
        self.assertIn("Memory snapshot from prior sessions — use it silently.", context)
        self.assertIn("long fact", context)
        self.assertFalse(context.endswith(PRIME_FOOTER))

    def test_prime_single_long_hit_cannot_starve_later_hits(self) -> None:
        context = build_prime_context(
            {"facts": [], "candidate_notes": []},
            {
                "hits": [
                    {"body": "monster hit " * 500},
                    {"body": "short survivor hit"},
                ]
            },
            max_chars=6_000,
        )

        self.assertIn("short survivor hit", context)
        self.assertIn("monster hit", context)
        # capped body: at most PRIME_ITEM_CAP chars plus the ellipsis
        for line in context.splitlines():
            if line.startswith("- monster hit"):
                self.assertLessEqual(len(line), 2 + 400 + 1)  # "- " + cap + "…"
                self.assertTrue(line.endswith("…"))

    def test_prime_recap_body_capped_per_item(self) -> None:
        context = build_prime_context(
            {"facts": [{"fact_text": "Durable cedar preference"}], "candidate_notes": []},
            {"hits": []},
            recap_text="recap words " * 400,
            max_chars=6_000,
        )

        self.assertIn("Durable cedar preference", context)
        lines = context.splitlines()
        recap_body = lines[lines.index("Prior conversation recap:") + 1]
        # capped body: at most PRIME_RECAP_CAP chars plus the ellipsis
        self.assertLessEqual(len(recap_body), 500 + 1)
        self.assertTrue(recap_body.endswith("…"))

    def test_prime_empty_memory_still_returns_empty_despite_framing(self) -> None:
        context = build_prime_context(
            {"facts": [], "candidate_notes": []},
            {"hits": []},
            max_chars=6_000,
        )

        self.assertEqual(context, "")

    def test_prime_context_stays_empty_without_memory(self) -> None:
        context = build_prime_context(
            {"facts": [], "candidate_notes": []},
            {"hits": []},
            max_chars=6_000,
        )

        self.assertEqual(context, "")

    def test_prime_includes_prior_conversation_recap_from_fresh_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            calls = []

            class _Response:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                calls.append(request)
                if request.full_url.endswith("/v1/fresh_context"):
                    return _Response(
                        {
                            "summaries": [],
                            "recent": [],
                            "text": "Recap: discussed cedar roadmap",
                            "truncated": False,
                        }
                    )
                return _Response({})

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Prior conversation recap:", context)
            self.assertIn("Recap: discussed cedar roadmap", context)
            fresh_context_call = next(
                request
                for request in calls
                if request.full_url.endswith("/v1/fresh_context")
            )
            body = json.loads(fresh_context_call.data.decode("utf-8"))
            self.assertEqual(body, {"token_budget": 6_000 // 16})

    def test_prime_fresh_context_failure_falls_back_to_search_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(
                        {
                            "hits": [
                                {
                                    "message_id": 1,
                                    "session_id": "session-a",
                                    "body": "User: remember search-only cedar",
                                }
                            ]
                        }
                    ).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    raise HTTPError(request.full_url, 403, "forbidden", hdrs={}, fp=None)
                return _Response()

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Prior conversation recap:", context)
            self.assertIn("remember search-only cedar", context)

    def test_prime_fresh_context_timeout_falls_back_to_search_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(
                        {
                            "hits": [
                                {
                                    "message_id": 1,
                                    "session_id": "session-a",
                                    "body": "User: remember timeout-fallback cedar",
                                }
                            ]
                        }
                    ).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    raise URLError(TimeoutError("timed out"))
                return _Response()

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Prior conversation recap:", context)
            self.assertIn("remember timeout-fallback cedar", context)

    def test_prime_read_phase_timeout_on_fresh_context_falls_back_to_search_only(
        self,
    ) -> None:
        # Read-phase timeout: urlopen succeeds, response.read() raises a bare
        # builtin TimeoutError (not wrapped in URLError). Prime must still
        # emit every section that succeeded.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __init__(self, payload: dict[str, object] | None) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    if self._payload is None:
                        raise TimeoutError("The read operation timed out")
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    return _Response(None)
                if request.full_url.endswith("/v1/search_long_term"):
                    return _Response(
                        {"facts": [{"fact_id": 1, "fact_text": "prefers cedar decks"}]}
                    )
                return _Response(
                    {
                        "hits": [
                            {
                                "message_id": 1,
                                "session_id": "session-a",
                                "body": "User: remember read-timeout cedar",
                            }
                        ]
                    }
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Prior conversation recap:", context)
            self.assertIn("prefers cedar decks", context)
            self.assertIn("remember read-timeout cedar", context)

    def test_prime_read_phase_timeout_on_search_long_term_keeps_other_sections(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __init__(self, payload: dict[str, object] | None) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    if self._payload is None:
                        raise TimeoutError("The read operation timed out")
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    return _Response({"text": "Recap: discussed cedar roadmap"})
                if request.full_url.endswith("/v1/search_long_term"):
                    return _Response(None)
                return _Response(
                    {
                        "hits": [
                            {
                                "message_id": 1,
                                "session_id": "session-a",
                                "body": "User: remember partial-prime cedar",
                            }
                        ]
                    }
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Recap: discussed cedar roadmap", context)
            self.assertIn("remember partial-prime cedar", context)
            self.assertNotIn("Long-term memory:", context)

    def test_prime_max_chars_cap_enforced_with_recap_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    return _Response(
                        {
                            "summaries": [],
                            "recent": [],
                            "text": "Recap cedar " * 200,
                            "truncated": False,
                        }
                    )
                if request.full_url.endswith("/v1/search_long_term"):
                    return _Response(
                        {
                            "facts": [
                                {"fact_text": "Durable cedar preference " * 50}
                            ],
                            "candidate_notes": [],
                        }
                    )
                return _Response({"hits": []})

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--max-chars",
                        "200",
                    ]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertLessEqual(len(context), 200)

    def test_prime_fetch_trimmed_recap_carries_truncation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    return _Response(
                        {
                            "summaries": [],
                            "recent": [],
                            "text": "Recap cedar " * 200,
                            "truncated": False,
                        }
                    )
                if request.full_url.endswith("/v1/search_long_term"):
                    return _Response({"facts": [], "candidate_notes": []})
                return _Response({"hits": []})

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--max-chars",
                        "2000",
                    ]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertLessEqual(len(context), 2000)
            lines = context.splitlines()
            recap_index = lines.index("Prior conversation recap:")
            recap_line = lines[recap_index + 1]
            self.assertTrue(
                recap_line.endswith("…"),
                f"expected truncated recap to end with an ellipsis marker, got: {recap_line!r}",
            )

    def test_prime_huge_recap_does_not_starve_long_term_and_transcript_sections(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    # Simulate a hosted endpoint that ignores token_budget and
                    # returns a recap large enough to fill the entire prime
                    # budget on its own if left uncapped.
                    return _Response(
                        {
                            "summaries": [],
                            "recent": [],
                            "text": "huge recap " * 2_000,
                            "truncated": False,
                        }
                    )
                if request.full_url.endswith("/v1/search_long_term"):
                    return _Response(
                        {
                            "facts": [{"fact_text": "Durable cedar preference"}],
                            "candidate_notes": [],
                        }
                    )
                return _Response(
                    {
                        "hits": [
                            {
                                "message_id": 1,
                                "session_id": "session-a",
                                "body": "User: recent cedar note",
                            }
                        ]
                    }
                )

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--max-chars",
                        "6000",
                    ]
                )

            self.assertEqual(code, 0)
            context = json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertLessEqual(len(context), 6000)
            self.assertIn("Durable cedar preference", context)
            self.assertIn("recent cedar note", context)
            self.assertIn(
                "Use this memory silently",
                context,
                "trailing footer instruction must survive a huge recap",
            )

    def test_prime_uses_transcript_when_long_term_search_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "clear"}), encoding="utf-8")

            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(
                        {
                            "hits": [
                                {
                                    "message_id": 1,
                                    "session_id": "session-a",
                                    "body": "User: remember fallback cedar",
                                }
                            ]
                        }
                    ).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/search_long_term"):
                    raise HTTPError(request.full_url, 500, "boom", hdrs={}, fp=None)
                return _Response()

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            self.assertIn(
                "remember fallback cedar",
                output["hookSpecificOutput"]["additionalContext"],
            )

    def test_prime_secret_response_warns_without_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(
                        {
                            "hits": [
                                {
                                    "message_id": 1,
                                    "session_id": "session-a",
                                    "body": "User: vx_secret",
                                }
                            ]
                        }
                    ).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/search_long_term"):
                    return _Response()
                return _Response()

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("forbidden secret", stderr.getvalue())
            self.assertNotIn("vx_secret", stderr.getvalue())

    def test_prime_fresh_context_secret_warns_without_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://api.example.test",
                        "api_key": "vx_secret",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            class _Response:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return json.dumps(self._payload).encode("utf-8")

            def fake_urlopen(request, timeout):
                if request.full_url.endswith("/v1/fresh_context"):
                    return _Response(
                        {
                            "summaries": [],
                            "recent": [],
                            "text": "Recap: vx_secret leaked",
                            "truncated": False,
                        }
                    )
                return _Response({})

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("forbidden secret", stderr.getvalue())
            self.assertNotIn("vx_secret", stderr.getvalue())

    def test_prime_rejects_non_http_base_url(self) -> None:
        config = HostedPrimeConfig(
            base_url="file:///tmp/vexic",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

        with patch("vexic.recorders.hosted_prime.urlopen") as urlopen_mock:
            with self.assertRaisesRegex(ValueError, "base_url.*http"):
                fetch_prime_context(config)

        urlopen_mock.assert_not_called()


class HostedPrimePostSearchNormalizationTests(unittest.TestCase):
    """_post_search must raise only RuntimeError for transport/decode failures.

    Downstream degradation (_safe_post_search, fetch_fresh_context) filters on
    RuntimeError; any other exception type escapes to the prime fail-open
    catch-all and discards the whole context.
    """

    def _config(self) -> HostedPrimeConfig:
        return HostedPrimeConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )

    def _run_post_search(self, body: bytes | None, exc: Exception | None):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                if exc is not None:
                    raise exc
                assert body is not None
                return body

        def fake_urlopen(request, timeout):
            return _Response()

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            return hosted_prime._post_search(
                self._config(), "search_transcript", {"query": "q", "limit": 1}
            )

    def test_read_phase_failures_normalize_to_runtime_error(self) -> None:
        cases: list[tuple[str, Exception]] = [
            ("TimeoutError", TimeoutError("The read operation timed out")),
            ("SSLError", ssl.SSLError("bad record mac")),
            ("IncompleteRead", IncompleteRead(b"partial")),
            ("RemoteDisconnected", RemoteDisconnected("closed connection")),
        ]
        for name, exc in cases:
            with self.subTest(name):
                with self.assertRaises(RuntimeError) as ctx:
                    self._run_post_search(None, exc)
                self.assertIn("hosted prime failed:", str(ctx.exception))
                self.assertIn(name, str(ctx.exception))

    def test_malformed_body_normalizes_to_runtime_error(self) -> None:
        for name, body in [
            ("JSONDecodeError", b"not json"),
            ("UnicodeDecodeError", b"\xff\xfe\xfa"),
        ]:
            with self.subTest(name):
                with self.assertRaises(RuntimeError) as ctx:
                    self._run_post_search(body, None)
                self.assertEqual(str(ctx.exception), f"hosted prime failed: {name}")

    def test_connect_phase_url_error_message_unchanged(self) -> None:
        def fake_urlopen(request, timeout):
            raise URLError(TimeoutError("timed out"))

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                hosted_prime._post_search(
                    self._config(), "search_transcript", {"query": "q", "limit": 1}
                )
        self.assertEqual(str(ctx.exception), "hosted prime failed: TimeoutError")


def _write_trigger_config(root: Path, **overrides: object) -> Path:
    config_path = root / "config.json"
    payload = {
        "base_url": "https://api.example.test",
        "api_key": "vx_secret",
        "project_id": "project-a",
        "session_id": "session-a",
        "agent_id": "agent-a",
    }
    payload.update(overrides)
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


class ClaudeCodeRecorderTriggerDreamCommandTests(unittest.TestCase):
    def test_trigger_dream_posts_summarize_phase_with_tenancy_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            calls = []

            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return b'{"status": "scheduled"}'

            def fake_urlopen(request, timeout):
                calls.append((request, timeout))
                return _Response()

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(["trigger-dream", "--config", str(config_path)])

            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            request, timeout = calls[0]
            self.assertEqual(timeout, 5.0)
            self.assertEqual(
                urlsplit(request.full_url).path, "/v1/trigger_dream_phase"
            )
            self.assertEqual(request.get_header("Authorization"), "Bearer vx_secret")
            self.assertEqual(request.get_header("X-vexic-project-id"), "project-a")
            self.assertEqual(request.get_header("X-vexic-agent-id"), "agent-a")
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body, {"phase": "summarize"})

    def test_trigger_dream_exits_zero_on_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            error = HTTPError(
                url="https://api.example.test/v1/trigger_dream_phase",
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=None,
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", side_effect=error),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(["trigger-dream", "--config", str(config_path)])

            self.assertEqual(code, 0)
            self.assertIn("HTTP 403", stderr.getvalue())

    def test_trigger_dream_exits_zero_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)

            def fake_urlopen(request, timeout):
                raise URLError(TimeoutError("timed out"))

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(["trigger-dream", "--config", str(config_path)])

            self.assertEqual(code, 0)

    def test_trigger_dream_exits_zero_on_read_phase_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)

            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    raise TimeoutError("The read operation timed out")

            def fake_urlopen(request, timeout):
                return _Response()

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(["trigger-dream", "--config", str(config_path)])

            self.assertEqual(code, 0)
            self.assertIn("hosted prime failed: TimeoutError", stderr.getvalue())

    def test_trigger_dream_exits_zero_on_connection_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)

            def fake_urlopen(request, timeout):
                raise URLError(ConnectionRefusedError())

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(["trigger-dream", "--config", str(config_path)])

            self.assertEqual(code, 0)

    def test_trigger_dream_exits_zero_on_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing_config = root / "does-not-exist.json"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = recorder_main(["trigger-dream", "--config", str(missing_config)])

            self.assertEqual(code, 0)


class ClaudeCodeRecorderPrimeSpawnsTriggerDreamTests(unittest.TestCase):
    def test_prime_spawns_trigger_dream_detached_with_safe_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            popen_calls = []

            class _FakeProcess:
                pass

            def fake_popen(argv, **kwargs):
                popen_calls.append((argv, kwargs))
                return _FakeProcess()

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.cli.subprocess.Popen", fake_popen),
                patch(
                    "vexic.recorders.cli.fetch_prime_context",
                    return_value=hosted_prime.PrimeFetchResult(context=""),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(len(popen_calls), 1)
            argv, kwargs = popen_calls[0]

            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(argv[1:4], ["-m", "vexic.cli", "recorder"])
            self.assertIn("trigger-dream", argv)
            config_index = argv.index("--config")
            self.assertEqual(argv[config_index + 1], str(config_path))
            self.assertNotIn("vx_secret", argv)
            self.assertNotIn("--api-key", argv)
            self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)
            self.assertTrue(kwargs.get("start_new_session"))

    def test_prime_output_unchanged_when_trigger_spawn_raises_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")

            def fake_popen(argv, **kwargs):
                raise OSError("spawn failed")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.cli.subprocess.Popen", side_effect=fake_popen),
                patch(
                    "vexic.recorders.cli.fetch_prime_context",
                    return_value=hosted_prime.PrimeFetchResult(context=""),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("trigger-dream", stderr.getvalue())

    def test_prime_returns_before_detached_child_exits(self) -> None:
        # Audit-mandated: an inherited stdout pipe would keep the SessionStart
        # hook's stdout open until the child exits. Prove prime's own output
        # is complete while a slow "child" is still blocked, by using a real
        # detached subprocess that waits on a file marker prime never touches.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            release_marker = root / "release.marker"

            spawned_processes = []
            real_popen = subprocess.Popen

            def blocking_popen(argv, **kwargs):
                # Replace the real trigger-dream argv with a tiny helper that
                # blocks until release_marker appears, simulating a slow
                # detached child while keeping the test hermetic (no network).
                del argv
                process = real_popen(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import pathlib, time\n"
                            f"marker = pathlib.Path({str(release_marker)!r})\n"
                            "while not marker.exists():\n"
                            "    time.sleep(0.01)\n"
                        ),
                    ],
                    **kwargs,
                )
                spawned_processes.append(process)
                return process

            stdout = io.StringIO()
            with (
                patch("vexic.recorders.cli.subprocess.Popen", blocking_popen),
                patch(
                    "vexic.recorders.cli.fetch_prime_context",
                    return_value=hosted_prime.PrimeFetchResult(context="some prime context"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

            try:
                self.assertEqual(code, 0)
                output = json.loads(stdout.getvalue())
                self.assertEqual(
                    output["hookSpecificOutput"]["additionalContext"],
                    "some prime context",
                )
                self.assertFalse(release_marker.exists())
                self.assertTrue(spawned_processes)
                self.assertIsNone(
                    spawned_processes[0].poll(),
                    "child should still be running; prime must not have waited on it",
                )
            finally:
                release_marker.write_text("go", encoding="utf-8")
                for process in spawned_processes:
                    process.wait(timeout=5)


def _prime_endpoint_payload(url: str) -> dict[str, object]:
    if url.endswith("/v1/fresh_context"):
        return {"summaries": [], "recent": [], "text": "Prior recap text", "truncated": False}
    if url.endswith("/v1/search_long_term"):
        return {
            "facts": [{"fact_text": "Durable cedar preference"}],
            "candidate_notes": [],
        }
    return {"hits": [{"body": "User: recent cedar note"}]}


class _PrimeFakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class ClaudeCodeRecorderPrimeDeadlineTests(unittest.TestCase):
    """Prime must exit cleanly inside the SessionStart hook budget (ADR 0025 D4).

    The Claude Code harness discards hook stdout entirely on timeout
    cancellation (verified empirically against the live harness), so the only
    delivery path is a clean exit before the hook timeout. These tests pin the
    client-side guarantees: parallel dispatch, an end-to-end deadline that
    degrades sections instead of losing the block, and the ADR 0024 framing/footer
    surviving partial assembly.
    """

    def _run_prime(
        self,
        fake_urlopen,
        *,
        extra_argv: list[str] | None = None,
        config_overrides: dict[str, object] | None = None,
    ) -> tuple[int, str, float, Path]:
        entered: list[str] = []

        def counting_urlopen(request, timeout):
            entered.append(request.full_url)
            return fake_urlopen(request, timeout)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root, **(config_overrides or {}))
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            stdout = io.StringIO()
            started = time.monotonic()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", counting_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen") as _popen,
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        *(extra_argv or []),
                    ]
                )
                elapsed = time.monotonic() - started
                # Abandoned daemon workers must enter the fake before the
                # patch is unwound, or a late-scheduled worker would hit the
                # real urlopen after the test ends.
                settle_deadline = time.monotonic() + 5.0
                while len(entered) < 3 and time.monotonic() < settle_deadline:
                    time.sleep(0.01)
            return code, stdout.getvalue(), elapsed, root

    def test_prime_reads_dispatch_in_parallel(self) -> None:
        lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_urlopen(request, timeout):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.25)
            with lock:
                active -= 1
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        code, output, _elapsed, _root = self._run_prime(fake_urlopen)

        self.assertEqual(code, 0)
        self.assertIn("Durable cedar preference", output)
        self.assertGreaterEqual(
            max_active,
            2,
            "prime reads must overlap; serial dispatch re-opens the 45s-vs-30s budget",
        )

    def test_prime_emits_partial_block_when_one_read_outlives_deadline(self) -> None:
        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/v1/search_transcript"):
                time.sleep(3.0)
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        code, output, elapsed, _root = self._run_prime(
            fake_urlopen, extra_argv=["--deadline-seconds", "1.0"]
        )

        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2.5, "prime must exit at the deadline, not the slow read")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Durable cedar preference", context)
        self.assertIn("Prior recap text", context)
        self.assertNotIn("recent cedar note", context)
        self.assertIn(hosted_prime.PRIME_FRAMING, context)
        self.assertIn(hosted_prime.PRIME_FOOTER, context)

    def test_prime_exits_clean_and_silent_when_all_reads_outlive_deadline(self) -> None:
        def fake_urlopen(request, timeout):
            time.sleep(3.0)
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        code, output, elapsed, _root = self._run_prime(
            fake_urlopen, extra_argv=["--deadline-seconds", "0.5"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertLess(elapsed, 2.5)

    def test_prime_writes_attempt_marker_before_reads_and_final_status(self) -> None:
        seen_during_fetch: list[object] = []
        status_holder: dict[str, Path] = {}

        def fake_urlopen(request, timeout):
            status_path = status_holder["path"]
            if status_path.exists():
                seen_during_fetch.append(json.loads(status_path.read_text(encoding="utf-8")))
            else:
                seen_during_fetch.append(None)
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "status.json"
            prime_status_path = root / "status-prime.json"
            status_holder["path"] = prime_status_path
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            stdout = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen"),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--status-path",
                        str(status_path),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertTrue(seen_during_fetch)
            for marker in seen_during_fetch:
                self.assertIsNotNone(
                    marker, "attempt marker must be on disk before any read starts"
                )
                self.assertEqual(marker["operation"], "prime")
                self.assertEqual(marker["phase"], "started")
            final = json.loads(prime_status_path.read_text(encoding="utf-8"))
            self.assertEqual(final["operation"], "prime")
            self.assertEqual(final["phase"], "finished")
            self.assertTrue(final["ok"])
            self.assertEqual(
                set(final["legs"]),
                {"fresh_context", "search_long_term", "search_transcript"},
            )
            for leg in final["legs"].values():
                self.assertEqual(leg["outcome"], "ok")
                self.assertIsInstance(leg["duration_ms"], int)

    def test_prime_final_status_marks_deadline_expired_legs(self) -> None:
        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/v1/search_transcript"):
                time.sleep(3.0)
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "status.json"
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--status-path",
                        str(status_path),
                        "--deadline-seconds",
                        "1.0",
                    ]
                )

            self.assertEqual(code, 0)
            final = json.loads((root / "status-prime.json").read_text(encoding="utf-8"))
            self.assertTrue(final["ok"])
            self.assertEqual(final["legs"]["search_transcript"]["outcome"], "deadline")
            self.assertEqual(final["legs"]["fresh_context"]["outcome"], "ok")

    def test_prime_spawns_trigger_dream_after_reads_finish(self) -> None:
        events: list[str] = []
        lock = threading.Lock()

        def fake_urlopen(request, timeout):
            with lock:
                events.append("read")
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        def fake_popen(argv, **kwargs):
            with lock:
                events.append("spawn")

            class _FakeProcess:
                pass

            return _FakeProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen", fake_popen),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )

        self.assertEqual(code, 0)
        self.assertEqual(events[-1], "spawn", "dream trigger must not precede the reads")
        self.assertEqual(events.count("spawn"), 1)
        self.assertEqual(events.count("read"), 3)

    def test_prime_prints_context_before_spawn_and_final_status(self) -> None:
        # A stalled Popen or status write after a successful fetch must not be
        # able to hold the block inside the hook kill window: stdout first.
        stdout_at_spawn: list[str] = []
        status_exists_at_spawn: list[bool] = []
        stdout = io.StringIO()
        status_holder: dict[str, Path] = {}

        def fake_urlopen(request, timeout):
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        def fake_popen(argv, **kwargs):
            stdout_at_spawn.append(stdout.getvalue())
            status_path = status_holder["path"]
            if status_path.exists():
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                status_exists_at_spawn.append(payload.get("phase") == "finished")
            else:
                status_exists_at_spawn.append(False)

            class _FakeProcess:
                pass

            return _FakeProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "status.json"
            status_holder["path"] = root / "status-prime.json"
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen", fake_popen),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--status-path",
                        str(status_path),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(len(stdout_at_spawn), 1)
        emitted = json.loads(stdout_at_spawn[0])
        self.assertIn(
            "Durable cedar preference",
            emitted["hookSpecificOutput"]["additionalContext"],
            "hook JSON must be on stdout before the dream spawn runs",
        )
        self.assertEqual(
            status_exists_at_spawn,
            [True],
            "finished status must be durable before the dream spawn runs",
        )

    def test_prime_deadline_flag_warns_when_at_or_above_hook_budget(self) -> None:
        def fake_urlopen(request, timeout):
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            stderr = io.StringIO()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen"),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--deadline-seconds",
                        "30",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIn("hook", stderr.getvalue())
        self.assertIn("30", stderr.getvalue())

    def test_prime_deadline_flag_rejects_non_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--deadline-seconds",
                        "0",
                    ]
                )
            self.assertEqual(code, 2, "usage error must surface, not run the reads")
            self.assertIn("positive", stderr.getvalue())

    def test_prime_legs_frozen_at_deadline_decision_despite_late_worker(self) -> None:
        # A worker that finishes shortly after the deadline decision must not
        # rewrite its leg to "ok" or smuggle its section into the context: the
        # emitted block and the status legs must describe the same snapshot.
        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/v1/search_transcript"):
                time.sleep(2.0)
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        config = hosted_prime.HostedPrimeConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            result = hosted_prime.fetch_prime_context(config, deadline_seconds=0.5)
            self.assertEqual(result.legs["search_transcript"]["outcome"], "deadline")
            self.assertNotIn("recent cedar note", result.context)
            time.sleep(2.5)
            self.assertEqual(
                result.legs["search_transcript"]["outcome"],
                "deadline",
                "late-finishing worker must not rewrite a decided leg",
            )

    def test_prime_status_lands_in_sibling_file_leaving_ingest_record_intact(self) -> None:
        # Prime and ingest sharing one status file would let an async Stop
        # ingest overwrite a killed prime's stale "started" marker — the very
        # evidence the marker exists to preserve — and vice versa.
        def fake_urlopen(request, timeout):
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status_path = root / "status.json"
            ingest_record = '{"ok": true, "operation": "ingest", "inserted": 7}\n'
            status_path.write_text(ingest_record, encoding="utf-8")
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--status-path",
                        str(status_path),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                status_path.read_text(encoding="utf-8"),
                ingest_record,
                "prime must never touch the ingest status file",
            )
            prime_status = json.loads(
                (root / "status-prime.json").read_text(encoding="utf-8")
            )
            self.assertEqual(prime_status["operation"], "prime")
            self.assertEqual(prime_status["phase"], "finished")

    def test_prime_deadline_flag_rejects_non_finite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = recorder_main(
                    [
                        "prime",
                        "--config",
                        str(config_path),
                        "--hook-input",
                        str(hook_payload),
                        "--deadline-seconds",
                        "inf",
                    ]
                )
            self.assertEqual(code, 2, "inf must be a usage error, not join(inf)")
            self.assertIn("finite", stderr.getvalue())

    def test_prime_exits_within_hook_budget_when_post_print_work_stalls(self) -> None:
        # The harness discards even flushed stdout unless the process exits
        # cleanly before the hook kill, so a stalled dream spawn or status
        # write after print must be abandoned, not waited on.
        def fake_urlopen(request, timeout):
            return _PrimeFakeResponse(_prime_endpoint_payload(request.full_url))

        def stalling_popen(argv, **kwargs):
            time.sleep(3.0)

            class _FakeProcess:
                pass

            return _FakeProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = _write_trigger_config(root)
            hook_payload = root / "session-start.json"
            hook_payload.write_text(json.dumps({"source": "startup"}), encoding="utf-8")
            stdout = io.StringIO()
            started = time.monotonic()
            with (
                patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen),
                patch("vexic.recorders.cli.subprocess.Popen", stalling_popen),
                patch("vexic.recorders.cli._SESSION_START_HOOK_KILL_SECONDS", 5.0),
                contextlib.redirect_stdout(stdout),
            ):
                code = recorder_main(
                    ["prime", "--config", str(config_path), "--hook-input", str(hook_payload)]
                )
            elapsed = time.monotonic() - started

        self.assertEqual(code, 0)
        self.assertLess(
            elapsed,
            2.0,
            "post-print stall must be abandoned inside the hook budget",
        )
        emitted = json.loads(stdout.getvalue())
        self.assertIn(
            "Durable cedar preference",
            emitted["hookSpecificOutput"]["additionalContext"],
        )


class _FakeSetupResult:
    def __init__(self) -> None:
        self.settings_path = Path("/fake/settings.json")
        self.config_path = Path("/fake/config.json")
        self.status_path = Path("/fake/status.json")
        self.connect_command = "claude mcp add vexic -- python -m vexic.mcp_stdio_main"
        self.command = "fake-hook-command"


class ClaudeCodeRecorderSetupTokenTests(unittest.TestCase):
    def test_setup_claude_code_accepts_token_argument(self) -> None:
        from vexic.recorders.setup_exchange import SetupExchangeResult

        captured = {}

        def fake_exchange(config, *, token):
            captured["base_url"] = config.base_url
            captured["token"] = token
            return SetupExchangeResult(
                api_key="vx_exchanged",
                key_id="key-1",
                project_id="exchanged-project",
                session_id="exchanged-session",
                agent_id="exchanged-agent",
            )

        install_kwargs = {}

        def fake_install(**kwargs):
            install_kwargs.update(kwargs)
            return _FakeSetupResult()

        stdout = io.StringIO()
        with (
            patch("vexic.recorders.cli.exchange_setup_token", fake_exchange),
            patch("vexic.recorders.cli.install_claude_code_setup", fake_install),
            contextlib.redirect_stdout(stdout),
        ):
            code = recorder_main(
                [
                    "setup-claude-code",
                    "--base-url",
                    "https://api.example.test",
                    "--token",
                    "vxsetup_secret",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(captured["base_url"], "https://api.example.test")
        self.assertEqual(captured["token"], "vxsetup_secret")
        self.assertEqual(install_kwargs["api_key"], "vx_exchanged")
        self.assertEqual(install_kwargs["project_id"], "exchanged-project")
        self.assertEqual(install_kwargs["session_id"], "exchanged-session")
        self.assertEqual(install_kwargs["agent_id"], "exchanged-agent")

        output = stdout.getvalue()
        self.assertNotIn("vx_exchanged", output)
        self.assertNotIn("vxsetup_secret", output)
        self.assertNotIn("exchanged-session", output)
        self.assertTrue(json.loads(output)["ok"])

    def test_setup_claude_code_token_and_manual_creds_are_mutually_exclusive(self) -> None:
        stderr = io.StringIO()
        with (
            patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
            patch("vexic.recorders.cli.install_claude_code_setup") as install_mock,
            contextlib.redirect_stderr(stderr),
        ):
            code = recorder_main(
                [
                    "setup-claude-code",
                    "--base-url",
                    "https://api.example.test",
                    "--token",
                    "vxsetup_secret",
                    "--api-key",
                    "vx_manual",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", stderr.getvalue())
        self.assertNotIn("vx_manual", stderr.getvalue())
        self.assertNotIn("vxsetup_secret", stderr.getvalue())
        exchange_mock.assert_not_called()
        install_mock.assert_not_called()

    def test_setup_claude_code_manual_path_still_installs(self) -> None:
        install_kwargs = {}

        def fake_install(**kwargs):
            install_kwargs.update(kwargs)
            return _FakeSetupResult()

        stdout = io.StringIO()
        with (
            patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
            patch("vexic.recorders.cli.install_claude_code_setup", fake_install),
            contextlib.redirect_stdout(stdout),
        ):
            code = recorder_main(
                [
                    "setup-claude-code",
                    "--base-url",
                    "https://api.example.test",
                    "--api-key",
                    "vx_manual",
                    "--project-id",
                    "project-a",
                    "--session-id",
                    "session-a",
                ]
            )

        self.assertEqual(code, 0)
        exchange_mock.assert_not_called()
        self.assertEqual(install_kwargs["api_key"], "vx_manual")
        self.assertEqual(install_kwargs["project_id"], "project-a")
        self.assertEqual(install_kwargs["session_id"], "session-a")
        self.assertNotIn("vx_manual", stdout.getvalue())

    def test_setup_claude_code_blank_token_is_rejected(self) -> None:
        stderr = io.StringIO()
        with (
            patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
            patch("vexic.recorders.cli.install_claude_code_setup") as install_mock,
            contextlib.redirect_stderr(stderr),
        ):
            code = recorder_main(
                [
                    "setup-claude-code",
                    "--base-url",
                    "https://api.example.test",
                    "--token",
                    "   ",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--token", stderr.getvalue())
        exchange_mock.assert_not_called()
        install_mock.assert_not_called()

    def test_setup_claude_code_empty_token_does_not_fall_through_to_manual(self) -> None:
        # An empty --token must not silently take the manual path; it is an
        # explicit (invalid) request to use token exchange.
        stderr = io.StringIO()
        with (
            patch("vexic.recorders.cli.exchange_setup_token") as exchange_mock,
            patch("vexic.recorders.cli.install_claude_code_setup") as install_mock,
            contextlib.redirect_stderr(stderr),
        ):
            code = recorder_main(
                [
                    "setup-claude-code",
                    "--base-url",
                    "https://api.example.test",
                    "--token",
                    "",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--token", stderr.getvalue())
        exchange_mock.assert_not_called()
        install_mock.assert_not_called()

    def test_setup_claude_code_manual_path_requires_full_triad(self) -> None:
        stderr = io.StringIO()
        with (
            patch("vexic.recorders.cli.install_claude_code_setup") as install_mock,
            contextlib.redirect_stderr(stderr),
        ):
            code = recorder_main(
                [
                    "setup-claude-code",
                    "--base-url",
                    "https://api.example.test",
                    "--api-key",
                    "vx_manual",
                ]
            )

        self.assertEqual(code, 2)
        message = stderr.getvalue()
        self.assertIn("--project-id", message)
        self.assertIn("--token", message)
        self.assertNotIn("vx_manual", message)
        install_mock.assert_not_called()
