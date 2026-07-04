import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
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
from vexic.recorders.cli import main as recorder_main
from vexic.recorders.claude_setup import (
    install_claude_code_setup,
    uninstall_claude_code_setup,
)
from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
from vexic.recorders.hosted_prime import (
    HostedPrimeConfig,
    build_prime_context,
    fetch_prime_context,
)
from vexic.recorders.status import RecorderStatus, write_status


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

            def fake_post(config, *, messages, forbidden_values):
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

            def fake_post(config, *, messages, forbidden_values):
                return {"items": [{"status": "inserted"} for _ in messages]}

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

            def fake_post(config, *, messages, forbidden_values):
                calls.append(messages)
                if len(calls) == 1:
                    return {"items": [{"status": "inserted"} for _ in messages]}
                if len(calls) == 2:
                    return {
                        "items": [{"status": "skipped"} for _ in messages[:-1]]
                        + [{"status": "rejected"}]
                    }
                return {"items": [{"status": "inserted"} for _ in messages]}

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

            def fake_post(config, *, messages, forbidden_values):
                calls.append(messages)
                return {"items": [{"status": "inserted"} for _ in messages]}

            with (
                patch(
                    "vexic.recorders.cli.iter_claude_code_source_messages",
                    return_value=iter(messages),
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
                    "vexic.recorders.cli.iter_claude_code_source_messages",
                    return_value=iter(messages),
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
                    self._content = content

                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self) -> bytes:
                    return self._content

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
            project_root = home / "project"
            project_root.mkdir()
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
                project_root=project_root,
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
            mcp_config = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
            vexic_server = mcp_config["mcpServers"]["vexic"]
            repo_root = Path(__file__).resolve().parents[1]
            launcher = Path(__file__).resolve().parents[1] / "scripts" / "vexic-mcp-stdio.py"
            self.assertEqual(vexic_server["command"], shutil.which("uv"))
            self.assertEqual(
                vexic_server["args"],
                [
                    "run",
                    "--with-editable",
                    f"{repo_root}[local-embed]",
                    "python",
                    str(launcher),
                    "--recorder-config",
                    str(result.config_path),
                ],
            )
            self.assertNotIn("vx_secret", json.dumps(mcp_config))

    def test_setup_uses_home_relative_recorder_path_in_project_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()

            with patch("vexic.recorders.claude_setup.Path.home", return_value=home):
                result = install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                    project_root=project_root,
                )

            mcp_config = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
            args = mcp_config["mcpServers"]["vexic"]["args"]
            self.assertEqual(args[-1], "~/.vexic/claude-code-recorder.json")
            self.assertNotIn(str(home), json.dumps(mcp_config))
            self.assertTrue(result.config_path.exists())

    def test_setup_disables_project_mcp_entry_until_user_enables_it(self) -> None:
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
                project_root=project_root,
            )

            mcp_config = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
            settings = json.loads(result.settings_path.read_text(encoding="utf-8"))
            self.assertIn("vexic", mcp_config["mcpServers"])
            self.assertEqual(settings["disabledMcpjsonServers"], ["vexic"])
            self.assertNotIn("vexic", settings.get("enabledMcpjsonServers", []))

    def test_setup_writes_mcp_launcher_that_runs_outside_vexic_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "customer-project"
            project_root.mkdir()

            install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
                project_root=project_root,
            )

            repo_root = Path(__file__).resolve().parents[1]
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                [str(repo_root / "src"), env.get("PYTHONPATH", "")]
            )
            mcp_config = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
            server = mcp_config["mcpServers"]["vexic"]
            result = subprocess.run(
                [server["command"], *server["args"]],
                input="",
                text=True,
                cwd=project_root,
                env=env,
                capture_output=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_setup_rejects_missing_project_root_before_writing_setup_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "missing-project"

            with self.assertRaisesRegex(ValueError, "project_root must be an existing directory"):
                install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                    project_root=project_root,
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

    def test_setup_secret_write_failure_does_not_leave_project_mcp_entry(self) -> None:
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
                        project_root=project_root,
                    )

            mcp_path = project_root / ".mcp.json"
            if mcp_path.exists():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
                self.assertNotIn("vexic", mcp_config.get("mcpServers", {}))
            self.assertFalse((home / ".vexic" / "claude-code-recorder.json").exists())
            self.assertFalse((home / ".claude" / "settings.json").exists())

    def test_setup_mcp_parse_failure_does_not_leave_secret_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()
            (project_root / ".mcp.json").write_text("{", encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                    project_root=project_root,
                )

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
            self.assertIn("C:/Users/Ryan/.local/bin/uv.exe", command)
            self.assertIn(str(result.config_path).replace("\\", "/"), command)
            self.assertNotIn("\\", command)
            self.assertFalse(hook["async"])

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
                        "--project-root",
                        str(project_root),
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
            mcp_config = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(list(mcp_config["mcpServers"]), ["vexic"])

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
                        "--project-root",
                        str(project_root),
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
            project_root = home / "project"
            project_root.mkdir()
            install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
                project_root=project_root,
            )
            mcp_path = project_root / ".mcp.json"
            mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
            mcp_config["mcpServers"]["other"] = {"command": "echo", "args": ["keep"]}
            mcp_path.write_text(json.dumps(mcp_config), encoding="utf-8")
            settings_path = home / ".claude" / "settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["hooks"]["Stop"].append(
                {"hooks": [{"type": "command", "command": "echo keep"}]}
            )
            settings["hooks"]["SessionStart"].append(
                {"hooks": [{"type": "command", "command": "echo keep session"}]}
            )
            settings_path.write_text(json.dumps(settings), encoding="utf-8")

            removed = uninstall_claude_code_setup(home=home, project_root=project_root)

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
            mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
            self.assertEqual(mcp_config["mcpServers"], {"other": {"command": "echo", "args": ["keep"]}})

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
                    "--project-root",
                    str(project_root),
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
            self.assertTrue((project_root / ".mcp.json").exists())

    def test_top_level_setup_uses_stable_uv_launcher_not_setup_python(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project_root = home / "project"
            project_root.mkdir()
            setup_python = home / "uv-cache" / ".tmpdead" / "Scripts" / "python.exe"
            uv_path = home / "bin" / "uv.exe"

            with (
                patch("sys.executable", str(setup_python)),
                patch("shutil.which", return_value=str(uv_path)),
            ):
                code = vexic_main(
                    [
                        "setup",
                        "claude-code",
                        "--home",
                        str(home),
                        "--project-root",
                        str(project_root),
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
            mcp_config = json.loads((project_root / ".mcp.json").read_text(encoding="utf-8"))
            server = mcp_config["mcpServers"]["vexic"]
            repo_root = str(Path(__file__).resolve().parents[1])

            self.assertEqual(code, 0)
            self.assertNotIn(str(setup_python).replace("\\", "/"), hook_command)
            self.assertIn(str(uv_path).replace("\\", "/"), hook_command)
            self.assertIn("run --with-editable", hook_command)
            self.assertIn(repo_root.replace("\\", "/"), hook_command)
            self.assertEqual(server["command"], str(uv_path))
            self.assertIn("--with-editable", server["args"])
            self.assertIn(f"{repo_root}[local-embed]", server["args"])
            self.assertNotIn(str(setup_python), json.dumps(mcp_config))

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
                        "--project-root",
                        str(project_root),
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
                        "--project-root",
                        str(project_root),
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

            def fake_post(config, *, messages, forbidden_values):
                calls.append((config, messages, forbidden_values))
                return {"items": [{"status": "inserted"}]}

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
    def test_ingest_failure_writes_status_and_returns_two(self) -> None:
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

            with patch(
                "vexic.recorders.cli.post_source_messages",
                side_effect=RuntimeError("hosted ingest failed: HTTP 403"),
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

            self.assertEqual(code, 2)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"], "hosted ingest failed: HTTP 403")
            self.assertEqual(status["source_session_id"], "claude-session")
            self.assertEqual(status["transcript_path"], str(transcript))
            self.assertNotIn("vx_secret", json.dumps(status))

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

            with (
                patch(
                    "vexic.recorders.cli.post_source_messages",
                    return_value={"items": [{"status": "inserted"}]},
                ),
                patch("vexic.recorders.cli.write_status", side_effect=OSError("disk full")),
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
                return_value={"items": [{"status": "inserted"}]},
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
                        "160",
                    ]
                )

            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertLessEqual(len(context), 160)
            self.assertIn("Durable cedar preference", context)
            self.assertIn("recent cedar note", context)
            self.assertNotIn("vx_secret", stdout.getvalue())
            self.assertEqual(
                [urlsplit(call[0].full_url).path for call in calls],
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
            self.assertEqual(body, {"token_budget": 6_000 // 4})

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
