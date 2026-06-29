import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
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
                        "http://testserver",
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
            self.assertIn(str(result.config_path), vexic_commands[0])
            config = json.loads(result.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["api_key"], "vx_secret")
            self.assertEqual(config["agent_id"], "agent-a")

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
            self.assertIn(str(result.config_path), command)
            self.assertIn(f'--config "{result.config_path}"', command)
            self.assertNotIn("vx_secret", command)

    def test_setup_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for _ in range(2):
                install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                )

            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for group in settings["hooks"]["Stop"]
                for hook in group["hooks"]
                if "vexic" in hook["command"]
            ]
            self.assertEqual(len(commands), 1)

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
