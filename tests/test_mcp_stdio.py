import asyncio
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.mcp_stdio import McpServerConfig, handle_jsonrpc_message
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

    async def test_tools_list_is_read_only(self) -> None:
        response = await self._request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )

        tool_names = {tool["name"] for tool in response["result"]["tools"]}

        self.assertEqual(tool_names, {"search_transcript", "search_long_term"})

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

    async def test_notifications_do_not_emit_responses(self) -> None:
        response = await self._request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

        self.assertIsNone(response)


if __name__ == "__main__":
    unittest.main()
