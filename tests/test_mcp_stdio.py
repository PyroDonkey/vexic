import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.mcp_stdio import (
    MAX_EXPAND_HISTORY_CHARS,
    McpServerConfig,
    _parse_args,
    handle_jsonrpc_message,
)
from vexic.storage import save_messages


class McpStdioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
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
        self.assertIn("Read-only Vexic memory", result["instructions"])
        self.assertIn("No transcript append", result["instructions"])
        self.assertIn("verbatim history expansion", result["instructions"])

    async def test_tools_list_is_read_only(self) -> None:
        response = await self._request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

        tool_names = {tool["name"] for tool in response["result"]["tools"]}

        self.assertEqual(tool_names, {"search_transcript", "search_long_term"})

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
            {"search_transcript", "search_long_term", "expand_history"},
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
                    "name": "search_transcript",
                    "arguments": {"query": "cedar"},
                },
            }
        )

        text = response["result"]["content"][0]["text"]
        self.assertIn("session alpha cedar", text)
        self.assertNotIn("session beta cedar", text)

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
                    "name": "search_transcript",
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
        response = await handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "expand_history",
                    "arguments": {"first_message_id": 1, "last_message_id": 101},
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
                    "name": "search_long_term",
                    "arguments": {"query": "compact reports"},
                },
            }
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("Embeddings", response["result"]["content"][0]["text"])

    async def test_invalid_tool_calls_return_tool_errors(self) -> None:
        cases = [
            ("search_transcript", {"query": ""}, "query must be a non-empty string"),
            ("search_transcript", {"query": "x" * 1001}, "1000 characters"),
            ("search_transcript", {"query": "cedar", "limit": 0}, "between 1 and 20"),
            ("search_transcript", {"query": "cedar", "limit": 21}, "between 1 and 20"),
            ("search_transcript", {"query": "cedar", "limit": "5"}, "integer"),
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
