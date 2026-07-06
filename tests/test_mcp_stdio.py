import asyncio
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import SearchTranscriptResult, TranscriptHit
from vexic.embeddings import EMBEDDING_DIM
from vexic.mcp_presentation import TOOL_ANNOTATIONS, server_instructions
from vexic.mcp_stdio import (
    MAX_EXPAND_HISTORY_CHARS,
    MAX_EXPAND_HISTORY_MESSAGES,
    McpServerConfig,
    _parse_args,
    handle_jsonrpc_message,
    main,
    run_stdio,
)
from vexic.models import FactCandidate
from vexic.storage import commit_dream_cycle, save_messages
from vexic.hosted_mcp import create_hosted_http_memory_service, run_recorder_config_proxy


class _HostedApiHandler(BaseHTTPRequestHandler):
    captured: ClassVar[dict[str, object]] = {}
    response_payload: ClassVar[dict[str, object]] = {}
    response_status: ClassVar[int] = 200

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        type(self).captured = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "x-vexic-project-id": self.headers.get("X-Vexic-Project-Id"),
            "x-vexic-session-id": self.headers.get("X-Vexic-Session-Id"),
            "x-vexic-agent-id": self.headers.get("X-Vexic-Agent-Id"),
            "body": json.loads(body),
        }
        payload = json.dumps(type(self).response_payload).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class McpStdioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        _HostedApiHandler.captured = {}
        _HostedApiHandler.response_payload = {}
        _HostedApiHandler.response_status = 200
        self.config = McpServerConfig(
            db_path=self.db_path,
            tenant_id="tenant-a",
            session_id="session-a",
        )
        self.config.service().init_schema()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _request(self, message: dict) -> dict | None:
        return await handle_jsonrpc_message(message, self.config)

    async def test_run_stdio_error_responses_never_echo_exception_content(self) -> None:
        sentinel = "user-pasted-credential-abc123"

        async def raising_handle(message: dict, config: McpServerConfig) -> dict | None:
            raise RuntimeError(f"validation blew up on {sentinel}")

        stdout = io.StringIO()
        with patch("vexic.mcp_stdio.handle_jsonrpc_message", raising_handle):
            await run_stdio(
                self.config,
                stdin=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/call"}\n'),
                stdout=stdout,
                stderr=io.StringIO(),
            )

        response = json.loads(stdout.getvalue())
        self.assertNotIn(sentinel, stdout.getvalue())
        self.assertIn("RuntimeError", response["error"]["message"])

    async def test_run_stdio_reports_parse_errors_by_position_without_input_echo(self) -> None:
        sentinel = "user-pasted-credential-abc123"
        stdout = io.StringIO()
        await run_stdio(
            self.config,
            stdin=io.StringIO(f'{{"jsonrpc": "2.0", "note": "{sentinel}"\n'),
            stdout=stdout,
            stderr=io.StringIO(),
        )

        response = json.loads(stdout.getvalue())
        self.assertEqual(response["error"]["code"], -32700)
        self.assertNotIn(sentinel, stdout.getvalue())
        self.assertIn("parse error at line", response["error"]["message"])

    async def test_initialize_advertises_read_only_server(self) -> None:
        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )

        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(result["serverInfo"]["name"], "vexic-local-memory")
        self.assertEqual(result["instructions"], server_instructions(False))
        self.assertIn("proactively", result["instructions"])
        self.assertIn("recall_user_memory", result["instructions"])
        self.assertIn("No transcript append", result["instructions"])
        self.assertIn("verbatim history expansion", result["instructions"])

    async def test_tools_list_is_read_only(self) -> None:
        response = await self._request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

        tools = response["result"]["tools"]
        tool_names = {tool["name"] for tool in tools}

        self.assertEqual(tool_names, {"recall_conversation_history", "recall_user_memory"})
        for tool in tools:
            self.assertEqual(tool["annotations"], TOOL_ANNOTATIONS)
            self.assertIn("proactively", tool["description"])

    async def test_tools_list_advertises_as_of_on_recall_user_memory(self) -> None:
        response = await self._request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        properties = tools["recall_user_memory"]["inputSchema"]["properties"]
        self.assertIn("as_of", properties)

    async def test_expand_history_is_unavailable_without_privileged_slice(self) -> None:
        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {"first_message_id": 1, "last_message_id": 1},
                },
            }
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("unknown tool", response["result"]["content"][0]["text"])

    async def test_tools_list_includes_expand_history_when_privileged_slice_is_enabled(
        self,
    ) -> None:
        config = McpServerConfig(
            db_path=self.db_path,
            tenant_id="tenant-a",
            session_id="session-a",
            enable_expand_history=True,
        )

        response = await handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            config,
        )

        tool_names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            tool_names,
            {"recall_conversation_history", "recall_user_memory", "expand_history"},
        )

    def test_cli_flag_enables_expand_history(self) -> None:
        config = _parse_args(
            [
                "--db-path",
                self.db_path,
                "--tenant-id",
                "tenant-a",
                "--enable-expand-history",
            ]
        )

        self.assertTrue(config.enable_expand_history)

    def test_cli_flag_binds_agent_scope(self) -> None:
        config = _parse_args(
            [
                "--db-path",
                self.db_path,
                "--tenant-id",
                "tenant-a",
                "--agent-id",
                "agent-a",
            ]
        )

        self.assertEqual(config.agent_id, "agent-a")

    def test_cli_flag_configures_hosted_api_transport(self) -> None:
        config = _parse_args(
            [
                "--api-base-url",
                "https://vexic.example",
                "--api-key-env",
                "CUSTOM_VEXIC_KEY",
                "--tenant-id",
                "tenant-a",
            ]
        )

        self.assertEqual(config.api_base_url, "https://vexic.example")
        self.assertEqual(config.api_key_env, "CUSTOM_VEXIC_KEY")

    def test_recorder_config_proxy_posts_to_hosted_mcp_without_printing_secret(self) -> None:
        _HostedApiHandler.response_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": []},
        }
        server = HTTPServer(("127.0.0.1", 0), _HostedApiHandler)
        thread = Thread(target=server.handle_request)
        thread.start()
        try:
            config_path = Path(self.temp_dir.name) / "claude-code-recorder.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "api_key": "vx_test_key",
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "agent_id": "agent-a",
                    }
                ),
                encoding="utf-8",
            )
            stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
            stdout = io.StringIO()
            stderr = io.StringIO()

            code = run_recorder_config_proxy(
                config_path,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            server.server_close()
            thread.join(timeout=1)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), _HostedApiHandler.response_payload)
        self.assertEqual(stderr.getvalue(), "")
        captured = _HostedApiHandler.captured
        self.assertEqual(captured["path"], "/mcp")
        self.assertEqual(captured["authorization"], "Bearer vx_test_key")
        self.assertEqual(captured["x-vexic-project-id"], "project-a")
        self.assertEqual(captured["x-vexic-session-id"], "session-a")
        self.assertEqual(captured["x-vexic-agent-id"], "agent-a")
        self.assertEqual(captured["body"]["method"], "tools/list")
        self.assertNotIn("vx_test_key", stdout.getvalue())
        self.assertNotIn("vx_test_key", stderr.getvalue())

    def test_main_decodes_stdin_as_utf8_not_locale(self) -> None:
        # Regression: on Windows under `uv run`, sys.stdin decodes as cp1252,
        # so non-ASCII JSON-RPC payloads are silently mojibake'd before
        # json.loads ever sees them. The stdio entry point must decode stdin as
        # UTF-8 regardless of the platform locale.
        query = "café ၁ 中"
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_conversation_history",
                        "arguments": {"query": query},
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        request_bytes = request.encode("utf-8")

        class _WindowsLikeStream:
            def __init__(self, data: bytes) -> None:
                self.buffer = io.BytesIO(data)
                # Mirror real uv-run Windows std-stream text decoding.
                self._text = data.decode("cp1252", "surrogateescape")

            def __iter__(self):
                return iter(self._text.splitlines(keepends=True))

            def write(self, _s: str) -> int:
                return 0

            def flush(self) -> None:
                return None

        captured: dict[str, object] = {}

        async def fake_handle(message, config):
            captured["message"] = message
            return None

        with (
            patch("vexic.mcp_stdio.sys.stdin", _WindowsLikeStream(request_bytes)),
            patch("vexic.mcp_stdio.sys.stdout", _WindowsLikeStream(b"")),
            patch("vexic.mcp_stdio.sys.stderr", _WindowsLikeStream(b"")),
            patch("vexic.mcp_stdio.handle_jsonrpc_message", fake_handle),
        ):
            code = main(["--db-path", self.db_path, "--tenant-id", "tenant-a"])

        self.assertEqual(code, 0)
        message = captured["message"]
        self.assertEqual(message["params"]["arguments"]["query"], query)

    def test_main_writes_stdout_as_utf8_not_locale(self) -> None:
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="cedar café ၁ 中")])],
            session_id="session-a",
        )
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_conversation_history",
                        "arguments": {"query": "cedar"},
                    },
                }
            )
            + "\n"
        )

        class _BufferedStream:
            def __init__(self, data: bytes = b"") -> None:
                self.buffer = io.BytesIO(data)

        stdout = _BufferedStream()

        with (
            patch("vexic.mcp_stdio.sys.stdin", _BufferedStream(request.encode("utf-8"))),
            patch("vexic.mcp_stdio.sys.stdout", stdout),
            patch("vexic.mcp_stdio.sys.stderr", _BufferedStream()),
        ):
            code = main(
                [
                    "--db-path",
                    self.db_path,
                    "--tenant-id",
                    "tenant-a",
                    "--session-id",
                    "session-a",
                ]
            )

        self.assertEqual(code, 0)
        output = stdout.buffer.getvalue()
        self.assertIn("café ၁ 中".encode("utf-8"), output)
        response = json.loads(output.decode("utf-8"))
        self.assertFalse(response["result"]["isError"])

    def test_recorder_config_launcher_decodes_stdin_as_utf8_not_locale(self) -> None:
        query = "café ၁ 中"
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "recall_conversation_history", "arguments": {"query": query}},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        request_bytes = request.encode("utf-8")
        captured: dict[str, object] = {}

        class _WindowsLikeStdin:
            def __init__(self, data: bytes) -> None:
                self.buffer = io.BytesIO(data)
                self._text = data.decode("cp1252", "surrogateescape")

            def __iter__(self):
                return iter(self._text.splitlines(keepends=True))

        class _BufferedStream:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

        def fake_proxy(path, *, stdin, stdout, stderr):
            captured["line"] = next(iter(stdin))
            return 0

        launcher = Path(__file__).resolve().parents[1] / "scripts" / "vexic-mcp-stdio.py"
        with (
            patch("vexic.hosted_mcp.run_recorder_config_proxy", fake_proxy),
            patch.object(sys, "argv", [str(launcher), "--recorder-config", "config.json"]),
            patch.object(sys, "stdin", _WindowsLikeStdin(request_bytes)),
            patch.object(sys, "stdout", _BufferedStream()),
            patch.object(sys, "stderr", _BufferedStream()),
        ):
            with self.assertRaises(SystemExit) as exc:
                runpy.run_path(str(launcher), run_name="__main__")

        self.assertEqual(exc.exception.code, 0)
        payload = json.loads(captured["line"])
        self.assertEqual(payload["params"]["arguments"]["query"], query)

    def test_recorder_config_proxy_forwards_hosted_mcp_http_error_body(self) -> None:
        _HostedApiHandler.response_status = 401
        _HostedApiHandler.response_payload = {
            "error": {"code": "unauthorized", "message": "Invalid hosted API key."},
        }
        server = HTTPServer(("127.0.0.1", 0), _HostedApiHandler)
        thread = Thread(target=server.handle_request)
        thread.start()
        try:
            config_path = Path(self.temp_dir.name) / "claude-code-recorder.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "api_key": "vx_test_key",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            code = run_recorder_config_proxy(
                config_path,
                stdin=io.StringIO('{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'),
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            server.server_close()
            thread.join(timeout=1)

        self.assertEqual(code, 0)
        # Upstream REST error envelopes are not JSON-RPC; the proxy must
        # re-wrap them so the client can correlate the reply to its request id.
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32000, "message": "Invalid hosted API key."},
            },
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("vx_test_key", stdout.getvalue())

    def test_recorder_config_proxy_reports_upstream_connection_errors(self) -> None:
        config_path = Path(self.temp_dir.name) / "claude-code-recorder.json"
        config_path.write_text(
            json.dumps(
                {
                    "base_url": "https://api.example.test",
                    "api_key": "vx_test_key",
                    "project_id": "project-a",
                    "session_id": "session-a",
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()

        with patch(
            "vexic.hosted_mcp.urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            code = run_recorder_config_proxy(
                config_path,
                stdin=io.StringIO('{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n'),
                stdout=stdout,
                stderr=io.StringIO(),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["id"], 7)
        self.assertIn("upstream", payload["error"]["message"])
        self.assertNotIn("vx_test_key", stdout.getvalue())

    def test_recorder_config_proxy_rejects_non_http_base_url(self) -> None:
        config_path = Path(self.temp_dir.name) / "claude-code-recorder.json"
        config_path.write_text(
            json.dumps(
                {
                    "base_url": "file:///tmp/vexic",
                    "api_key": "vx_test_key",
                    "project_id": "project-a",
                    "session_id": "session-a",
                }
            ),
            encoding="utf-8",
        )

        with patch("vexic.hosted_mcp.urllib.request.urlopen") as urlopen_mock:
            with self.assertRaisesRegex(ValueError, "base_url.*http"):
                run_recorder_config_proxy(
                    config_path,
                    stdin=io.StringIO('{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n'),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

        urlopen_mock.assert_not_called()

    def test_recorder_config_proxy_expands_home_relative_config_path(self) -> None:
        _HostedApiHandler.response_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": []},
        }
        server = HTTPServer(("127.0.0.1", 0), _HostedApiHandler)
        thread = Thread(target=server.handle_request)
        thread.start()
        try:
            home = Path(self.temp_dir.name) / "home"
            config_path = home / ".vexic" / "claude-code-recorder.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "api_key": "vx_test_key",
                        "project_id": "project-a",
                        "session_id": "session-a",
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
                code = run_recorder_config_proxy(
                    Path("~/.vexic/claude-code-recorder.json"),
                    stdin=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'),
                    stdout=stdout,
                    stderr=io.StringIO(),
                )
        finally:
            server.server_close()
            thread.join(timeout=1)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), _HostedApiHandler.response_payload)

    def test_recorder_config_proxy_rejects_unknown_config_fields(self) -> None:
        config_path = Path(self.temp_dir.name) / "claude-code-recorder.json"
        config_path.write_text(
            json.dumps(
                {
                    "base_url": "https://api.example.test",
                    "api_key": "vx_test_key",
                    "project_id": "project-a",
                    "session_id": "session-a",
                    "unexpected": "value",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "invalid recorder config"):
            run_recorder_config_proxy(
                config_path,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

    async def test_hosted_api_backed_search_uses_bearer_api_key(self) -> None:
        _HostedApiHandler.response_payload = SearchTranscriptResult(
            hits=[
                TranscriptHit(
                    message_id=7,
                    session_id="session-a",
                    body="User: remote cedar",
                )
            ]
        ).model_dump(mode="json")
        server = HTTPServer(("127.0.0.1", 0), _HostedApiHandler)
        thread = Thread(target=server.handle_request)
        thread.start()
        old_key = os.environ.get("VEXIC_API_KEY")
        os.environ["VEXIC_API_KEY"] = "vx_test_key"
        try:
            response = await handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_conversation_history",
                        "arguments": {"query": "cedar"},
                    },
                },
                McpServerConfig(
                    api_base_url=f"http://127.0.0.1:{server.server_port}",
                    tenant_id="tenant-a",
                    session_id="session-a",
                    project_id="project-a",
                    service_factory=create_hosted_http_memory_service,
                ),
            )
        finally:
            if old_key is None:
                os.environ.pop("VEXIC_API_KEY", None)
            else:
                os.environ["VEXIC_API_KEY"] = old_key
            server.server_close()
            thread.join(timeout=1)

        text = response["result"]["content"][0]["text"]
        captured = _HostedApiHandler.captured
        self.assertIn("remote cedar", text)
        self.assertEqual(captured["path"], "/v1/search_transcript")
        self.assertEqual(captured["authorization"], "Bearer vx_test_key")
        self.assertEqual(captured["body"]["scope"]["tenant_id"], "tenant-a")
        self.assertEqual(captured["body"]["scope"]["trust_boundary"], "networked")

    def test_hosted_http_client_rejects_non_http_api_base_url(self) -> None:
        with patch.dict(os.environ, {"VEXIC_API_KEY": "vx_test_key"}):
            config = McpServerConfig(
                api_base_url="file:///tmp/vexic",
                tenant_id="tenant-a",
                service_factory=create_hosted_http_memory_service,
            )

            with self.assertRaisesRegex(ValueError, "api_base_url.*http"):
                config.service()

    async def test_search_transcript_uses_configured_session_scope(self) -> None:
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="session alpha cedar")])],
            session_id="session-a",
        )
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="session beta cedar")])],
            session_id="session-b",
        )

        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar"},
                },
            }
        )

        text = response["result"]["content"][0]["text"]
        self.assertIn("session alpha cedar", text)
        self.assertNotIn("session beta cedar", text)

    async def test_search_transcript_renders_prose_without_internal_metadata(self) -> None:
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="prose cedar")])],
            session_id="session-a",
        )

        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar"},
                },
            }
        )

        text = response["result"]["content"][0]["text"]
        self.assertIn("prose cedar", text)
        self.assertIn("conversation history", text)
        self.assertNotIn("message_id", text)
        self.assertNotIn("session_id", text)
        self.assertNotIn("[message", text)
        self.assertNotIn('"hits"', text)

    async def test_search_transcript_includes_message_ids_when_expand_enabled(self) -> None:
        message_ids = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="expandable cedar")])],
            session_id="session-a",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar"},
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                enable_expand_history=True,
            ),
        )

        text = response["result"]["content"][0]["text"]
        self.assertIn(f"[message {message_ids[0]}", text)
        self.assertIn("expandable cedar", text)

    async def test_search_transcript_uses_configured_agent_scope(self) -> None:
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="agent alpha cedar")])],
            session_id="session-a",
            agent_id="agent-a",
        )
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="agent beta cedar")])],
            session_id="session-a",
            agent_id="agent-b",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar"},
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
            ),
        )

        text = response["result"]["content"][0]["text"]
        self.assertIn("agent alpha cedar", text)
        self.assertNotIn("agent beta cedar", text)

    async def test_search_rejects_caller_supplied_agent_scope(self) -> None:
        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar", "agent_id": "agent-b"},
                },
            }
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("unexpected argument", response["result"]["content"][0]["text"])

    async def test_expand_history_uses_configured_session_scope_when_enabled(self) -> None:
        alpha_ids = save_messages(
            self.db_path,
            [
                ModelRequest(parts=[UserPromptPart(content="session alpha one")]),
                ModelRequest(parts=[UserPromptPart(content="session alpha two")]),
            ],
            session_id="session-a",
        )
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="session beta")])],
            session_id="session-b",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {
                        "first_message_id": alpha_ids[0],
                        "last_message_id": alpha_ids[-1],
                    },
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                enable_expand_history=True,
            ),
        )

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(payload["egress_kind"], "expand_history")
        self.assertIn("session alpha one", payload["text"])
        self.assertIn("session alpha two", payload["text"])
        self.assertNotIn("session beta", payload["text"])

    async def test_expand_history_caps_configured_session_rows_not_id_span(self) -> None:
        first_id = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="session alpha first")])],
            session_id="session-a",
        )[0]
        save_messages(
            self.db_path,
            [
                ModelRequest(parts=[UserPromptPart(content=f"session beta {index}")])
                for index in range(MAX_EXPAND_HISTORY_MESSAGES)
            ],
            session_id="session-b",
        )
        last_id = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="session alpha last")])],
            session_id="session-a",
        )[0]

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {
                        "first_message_id": first_id,
                        "last_message_id": last_id,
                    },
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                enable_expand_history=True,
            ),
        )

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertIn("session alpha first", payload["text"])
        self.assertIn("session alpha last", payload["text"])
        self.assertNotIn("session beta", payload["text"])

    async def test_expand_history_rejects_caller_supplied_session(self) -> None:
        beta_ids = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="session beta only")])],
            session_id="session-b",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {
                        "first_message_id": beta_ids[0],
                        "last_message_id": beta_ids[-1],
                        "session_id": "session-b",
                    },
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                enable_expand_history=True,
            ),
        )

        text = response["result"]["content"][0]["text"]
        self.assertTrue(response["result"]["isError"])
        self.assertIn("unexpected argument", text)
        self.assertNotIn("session beta only", text)

    async def test_forbidden_values_fail_closed_on_search_egress(self) -> None:
        config = McpServerConfig(
            db_path=self.db_path,
            tenant_id="tenant-a",
            session_id="session-a",
            forbidden_secret_values=("cedar-secret",),
        )
        save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="cedar-secret")])],
            session_id="session-a",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar-secret"},
                },
            },
            config,
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("forbidden", response["result"]["content"][0]["text"])

    async def test_forbidden_values_fail_closed_on_expand_history_egress(self) -> None:
        message_ids = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content="cedar-secret")])],
            session_id="session-a",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {
                        "first_message_id": message_ids[0],
                        "last_message_id": message_ids[-1],
                    },
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                forbidden_secret_values=("cedar-secret",),
                enable_expand_history=True,
            ),
        )

        text = response["result"]["content"][0]["text"]
        self.assertTrue(response["result"]["isError"])
        self.assertIn("forbidden", text)
        self.assertNotIn("cedar-secret", text)

    async def test_expand_history_rejects_broad_ranges(self) -> None:
        message_ids = save_messages(
            self.db_path,
            [
                ModelRequest(parts=[UserPromptPart(content=f"session alpha {index}")])
                for index in range(MAX_EXPAND_HISTORY_MESSAGES + 1)
            ],
            session_id="session-a",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {
                        "first_message_id": message_ids[0],
                        "last_message_id": message_ids[-1],
                    },
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                enable_expand_history=True,
            ),
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("capped", response["result"]["content"][0]["text"])

    async def test_expand_history_rejects_bool_message_ids(self) -> None:
        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {"first_message_id": True, "last_message_id": True},
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                enable_expand_history=True,
            ),
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("must be an integer", response["result"]["content"][0]["text"])

    async def test_expand_history_caps_returned_text(self) -> None:
        message_ids = save_messages(
            self.db_path,
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content="x" * (MAX_EXPAND_HISTORY_CHARS + 100)
                        )
                    ]
                )
            ],
            session_id="session-a",
        )

        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {
                        "first_message_id": message_ids[0],
                        "last_message_id": message_ids[-1],
                    },
                },
            },
            McpServerConfig(
                db_path=self.db_path,
                tenant_id="tenant-a",
                session_id="session-a",
                enable_expand_history=True,
            ),
        )

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["text"]), MAX_EXPAND_HISTORY_CHARS)

    async def test_search_long_term_without_embedder_returns_tool_error(self) -> None:
        response = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "recall_user_memory",
                    "arguments": {"query": "compact reports"},
                },
            }
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("Embeddings", response["result"]["content"][0]["text"])

    async def test_recall_user_memory_accepts_as_of_argument(self) -> None:
        """COA-298: the stdio `recall_user_memory` tool's extra-key
        allowlist (`_reject_extra(arguments, {"query", "limit"})`) must
        accept `as_of` and thread it into `SearchLongTermRequest.as_of`.
        """
        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text="Ryan keeps cedar notes tentative.",
                    subject="Ryan",
                    category="fact",
                    importance=6,
                    confidence=0.8,
                    source_message_ids=[1],
                    occurred_at="2025-03-14",
                )
            ],
            candidate_embeddings=[[1.0] + [0.0] * (EMBEDDING_DIM - 1)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=1,
        )

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts],
        ):
            before = await self._request(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_user_memory",
                        "arguments": {"query": "cedar notes", "as_of": "2024-01-01"},
                    },
                }
            )
            after = await self._request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_user_memory",
                        "arguments": {"query": "cedar notes", "as_of": "2025-04-01"},
                    },
                }
            )

        self.assertFalse(before["result"]["isError"])
        self.assertNotIn("cedar notes tentative", before["result"]["content"][0]["text"])
        self.assertFalse(after["result"]["isError"])
        self.assertIn("cedar notes tentative", after["result"]["content"][0]["text"])

    async def test_invalid_tool_calls_return_tool_errors(self) -> None:
        cases = [
            ("recall_conversation_history", {"query": ""}, "query must be a non-empty string"),
            ("recall_conversation_history", {"query": "x" * 1001}, "1000 characters"),
            ("recall_conversation_history", {"query": "cedar", "limit": 0}, "between 1 and 20"),
            ("recall_conversation_history", {"query": "cedar", "limit": 21}, "between 1 and 20"),
            ("recall_conversation_history", {"query": "cedar", "limit": "5"}, "integer"),
            ("append_transcript", {"query": "cedar"}, "unknown tool"),
        ]

        for name, arguments, expected in cases:
            with self.subTest(name=name, arguments=arguments):
                response = await self._request(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )

                self.assertTrue(response["result"]["isError"])
                self.assertIn(expected, response["result"]["content"][0]["text"])

    async def test_notifications_do_not_emit_responses(self) -> None:
        response = await self._request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

        self.assertIsNone(response)


if __name__ == "__main__":
    unittest.main()
