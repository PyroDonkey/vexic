import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    TrustBoundary,
)
from vexic.embeddings import EMBEDDING_DIM
from vexic.hosted import HostedInMemoryRateLimiter, HostedMemoryService, HostedRateLimitRule
from vexic.hosted_http import MAX_BODY_BYTES, create_app
from vexic.mcp_presentation import server_instructions
from vexic.mcp_stdio import MCP_PROTOCOL_VERSION
from vexic.models import FactCandidate
from vexic.storage import commit_dream_cycle, single_message_adapter
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


class McpHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(root)
        self.keys = HostedApiKeyStore(root)
        self.service = HostedMemoryService(self.catalog, self.keys, telemetry=self.catalog)
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _api_key(
        self,
        *,
        capabilities: set[MemoryCapability] | None = None,
        project_ids: set[str] | None = None,
    ) -> str:
        project_ids = project_ids or {"project-a"}
        self.catalog.provision_tenant("tenant-a", project_ids=project_ids)
        return self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities=capabilities or {MemoryCapability.SEARCH},
            project_ids=project_ids,
        ).raw_key

    def _scope(
        self,
        *,
        session_id: str = "session-a",
        capabilities: set[MemoryCapability],
    ) -> MemoryScope:
        return MemoryScope(
            tenant_id="tenant-a",
            project_id="project-a",
            session_id=session_id,
            principal=Principal(
                principal_id="caller-supplied",
                principal_type=PrincipalType.HUMAN,
            ),
            trust_boundary=TrustBoundary.LOCAL_TRUSTED,
            capabilities=capabilities,
        )

    def _mcp_headers(
        self,
        api_key: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {api_key}",
        }
        if session_id is not None:
            headers["X-Vexic-Project-Id"] = "project-a"
            headers["X-Vexic-Session-Id"] = session_id
        return headers

    def _append(self, api_key: str, *, session_id: str, text: str) -> None:
        response = self.client.post(
            "/v1/append_transcript",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Vexic-Project-Id": "project-a",
                "X-Vexic-Session-Id": session_id,
            },
            json={
                "messages_json": [
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content=text)])
                    ).decode()
                ],
                "redaction": {"forbidden_values": []},
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_initialize_returns_json_and_no_session_header(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertNotIn("MCP-Session-Id", response.headers)
        self.assertEqual(response.json()["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)
        self.assertEqual(
            response.json()["result"]["capabilities"],
            {"tools": {"listChanged": False}},
        )
        self.assertEqual(response.json()["result"]["serverInfo"]["name"], "vexic-remote-memory")
        self.assertEqual(
            response.json()["result"]["instructions"],
            server_instructions(False),
        )

    def test_tools_list_is_read_only(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )

        self.assertEqual(response.status_code, 200)
        tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
        self.assertEqual(tool_names, {"recall_conversation_history", "recall_user_memory"})
        self.assertNotIn("expand_history", response.text)

    def test_tools_list_advertises_as_of_on_recall_user_memory(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        properties = tools["recall_user_memory"]["inputSchema"]["properties"]
        self.assertIn("as_of", properties)

    def test_tools_list_advertises_event_bounds_on_recall_user_memory(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        properties = tools["recall_user_memory"]["inputSchema"]["properties"]
        self.assertIn("event_after", properties)
        self.assertIn("event_before", properties)

    def test_search_transcript_uses_header_bound_session_scope(self) -> None:
        api_key = self._api_key(
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH}
        )
        self._append(api_key, session_id="session-a", text="session alpha cedar")
        self._append(api_key, session_id="session-b", text="session beta cedar")

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["result"]["content"][0]["text"]
        self.assertIn("User: session alpha cedar", text)
        self.assertNotIn("session beta cedar", response.text)
        self.assertNotIn("message_id", text)
        self.assertNotIn("session_id", text)
        self.assertNotIn('"hits"', text)

    def test_search_long_term_without_embedder_returns_tool_error(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "recall_user_memory",
                    "arguments": {"query": "compact reports"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Embeddings", result["content"][0]["text"])

    def test_recall_user_memory_accepts_as_of_argument(self) -> None:
        """The `recall_user_memory` tool's extra-key allowlist
        (`_reject_extra(arguments, {"query", "limit"})`) must accept `as_of`
        and thread it into `SearchLongTermRequest.as_of`.
        """
        api_key = self._api_key()
        tenant_db_path = self.catalog.get_tenant("tenant-a").db_path
        commit_dream_cycle(
            tenant_db_path,
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
            before = self.client.post(
                "/mcp",
                headers=self._mcp_headers(api_key, session_id="session-a"),
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_user_memory",
                        "arguments": {"query": "cedar notes", "as_of": "2024-01-01"},
                    },
                },
            )
            after = self.client.post(
                "/mcp",
                headers=self._mcp_headers(api_key, session_id="session-a"),
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_user_memory",
                        "arguments": {"query": "cedar notes", "as_of": "2025-04-01"},
                    },
                },
            )

        self.assertEqual(before.status_code, 200)
        before_result = before.json()["result"]
        self.assertFalse(before_result["isError"])
        self.assertNotIn("cedar notes tentative", before_result["content"][0]["text"])

        self.assertEqual(after.status_code, 200)
        after_result = after.json()["result"]
        self.assertFalse(after_result["isError"])
        self.assertIn("cedar notes tentative", after_result["content"][0]["text"])

    def test_recall_user_memory_accepts_event_bound_arguments(self) -> None:
        """The `recall_user_memory` extra-key allowlist must accept
        `event_after`/`event_before` and thread them into
        `SearchLongTermRequest`; an unknown key must still be rejected.
        """
        api_key = self._api_key()
        tenant_db_path = self.catalog.get_tenant("tenant-a").db_path
        commit_dream_cycle(
            tenant_db_path,
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

        def _recall(arguments: dict[str, object]) -> dict:
            return self.client.post(
                "/mcp",
                headers=self._mcp_headers(api_key, session_id="session-a"),
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "recall_user_memory", "arguments": arguments},
                },
            ).json()["result"]

        with patch(
            "vexic.subagents.retrieval.embed_texts",
            side_effect=lambda texts: [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts],
        ):
            after_excluded = _recall({"query": "cedar notes", "event_after": "2025-04-01"})
            after_included = _recall({"query": "cedar notes", "event_after": "2024-01-01"})
            before_excluded = _recall({"query": "cedar notes", "event_before": "2024-01-01"})
            before_included = _recall({"query": "cedar notes", "event_before": "2025-04-01"})
            unknown = _recall({"query": "cedar notes", "event_between": "2025-04-01"})

        self.assertFalse(after_excluded["isError"])
        self.assertNotIn("cedar notes tentative", after_excluded["content"][0]["text"])
        self.assertFalse(after_included["isError"])
        self.assertIn("cedar notes tentative", after_included["content"][0]["text"])
        self.assertFalse(before_excluded["isError"])
        self.assertNotIn("cedar notes tentative", before_excluded["content"][0]["text"])
        self.assertFalse(before_included["isError"])
        self.assertIn("cedar notes tentative", before_included["content"][0]["text"])
        self.assertTrue(unknown["isError"])
        self.assertIn("unexpected argument", unknown["content"][0]["text"])

    def test_origin_header_is_rejected_by_default(self) -> None:
        api_key = self._api_key()
        headers = self._mcp_headers(api_key)
        headers["Origin"] = "https://evil.example"

        response = self.client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 5, "method": "ping"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "origin_forbidden")

    def test_ping_returns_empty_jsonrpc_result(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 6, "method": "ping"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"jsonrpc": "2.0", "id": 6, "result": {}})

    def test_notification_returns_accepted_with_no_body(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_jsonrpc_response_message_returns_accepted_with_no_body(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 6, "result": {}},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_invalid_jsonrpc_version_returns_invalid_request(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "1.0", "id": 7, "method": "ping"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_initialize_negotiates_server_protocol_version(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)

    def test_invalid_protocol_version_header_returns_bad_request(self) -> None:
        api_key = self._api_key()
        headers = self._mcp_headers(api_key)
        headers["MCP-Protocol-Version"] = "not-a-version"

        response = self.client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_protocol_version")

    def test_mcp_requires_valid_bearer_api_key(self) -> None:
        api_key = self._api_key()

        cases = [
            ({}, "Missing hosted API key."),
            ({"Authorization": "Bearer nope"}, "Invalid hosted API key."),
            ({"X-Vexic-Api-Key": api_key}, "Missing hosted API key."),
        ]
        for headers, expected_message in cases:
            with self.subTest(headers=headers):
                response = self.client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 10, "method": "ping"},
                )

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "unauthorized")
                self.assertEqual(response.json()["error"]["message"], expected_message)

    def test_mcp_rejects_query_strings_to_avoid_token_leakage(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp?api_key=leaky",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 11, "method": "ping"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertNotIn("leaky", response.text)

    def test_mcp_inherits_hosted_body_cap(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            content=b"{" + (b'"x":' + b'"' + b"x" * MAX_BODY_BYTES + b'"}'),
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")

    def test_search_capability_denial_returns_tool_error(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE})

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("memory:search", result["content"][0]["text"])

    def test_tool_arguments_cannot_override_scope(self) -> None:
        api_key = self._api_key()

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar", "session_id": "session-b"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("unexpected argument", result["content"][0]["text"])

    def test_search_transcript_hostile_query_stays_scoped(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH})
        self._append(api_key, session_id="session-a", text="cedar OR 1 1 visible")
        self._append(api_key, session_id="session-b", text="cedar OR 1 1 hidden")

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json={
                "jsonrpc": "2.0",
                "id": 17,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar') OR 1=1 --"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertFalse(result.get("isError", False))
        text = result["content"][0]["text"]
        self.assertIn("visible", text)
        self.assertNotIn("hidden", text)

    def test_write_admin_and_expand_tools_are_unreachable(self) -> None:
        api_key = self._api_key()

        for name in ("append_transcript", "expand_history", "delete_scope", "rebuild"):
            with self.subTest(name=name):
                response = self.client.post(
                    "/mcp",
                    headers=self._mcp_headers(api_key, session_id="session-a"),
                    json={
                        "jsonrpc": "2.0",
                        "id": 14,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": {"query": "cedar"}},
                    },
                )

                self.assertEqual(response.status_code, 200)
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertIn("unknown tool", result["content"][0]["text"])

    def test_get_mcp_returns_method_not_allowed_without_sse(self) -> None:
        response = self.client.get(
            "/mcp",
            headers={"Accept": "text/event-stream"},
        )

        self.assertEqual(response.status_code, 405)
        self.assertNotEqual(response.headers.get("content-type"), "text/event-stream")

    def test_forbidden_values_fail_closed_on_search_egress(self) -> None:
        api_key = self._api_key(
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH}
        )
        client = TestClient(
            create_app(
                self.service,
                mcp_forbidden_secret_values=("cedar-secret",),
            )
        )
        self._append(api_key, session_id="session-a", text="cedar-secret")

        response = client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json={
                "jsonrpc": "2.0",
                "id": 15,
                "method": "tools/call",
                "params": {
                    "name": "recall_conversation_history",
                    "arguments": {"query": "cedar-secret"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("forbidden", result["content"][0]["text"])
        self.assertNotIn("cedar-secret", response.text)

    def test_rate_limit_returns_429_without_leaking_query(self) -> None:
        api_key = self._api_key()
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(
                default_rule=HostedRateLimitRule(limit=1, window_seconds=60),
            ),
        )
        client = TestClient(create_app(service))
        request = {
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {
                "name": "recall_conversation_history",
                "arguments": {"query": "cedar-secret"},
            },
        }

        client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json=request,
        )
        response = client.post(
            "/mcp",
            headers=self._mcp_headers(api_key, session_id="session-a"),
            json=request,
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        self.assertNotIn("cedar-secret", response.text)

    def test_search_requires_session_header_and_fails_closed(self) -> None:
        api_key = self._api_key()

        for tool in ("recall_conversation_history", "recall_user_memory"):
            with self.subTest(tool=tool):
                response = self.client.post(
                    "/mcp",
                    headers=self._mcp_headers(api_key),
                    json={
                        "jsonrpc": "2.0",
                        "id": 18,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": {"query": "cedar"}},
                    },
                )

                self.assertEqual(response.status_code, 200)
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertIn("X-Vexic-Session-Id", result["content"][0]["text"])
                self.assertNotIn("default", result["content"][0]["text"])

    def test_unexpected_auth_failure_returns_json_error(self) -> None:
        api_key = self._api_key()

        def _boom(_: str) -> object:
            raise RuntimeError("key store unavailable")

        self.service.api_keys.authenticate = _boom  # type: ignore[method-assign]

        response = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 19, "method": "ping"},
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("key store unavailable", response.text)

    def test_payload_egress_guard_covers_non_text_fields(self) -> None:
        from vexic.redaction import assert_no_forbidden_secret_values_in_payload

        payload = {
            "facts": [
                {"fact_text": "harmless", "subject": "cedar-secret in subject"}
            ]
        }
        with self.assertRaises(ValueError):
            assert_no_forbidden_secret_values_in_payload(("cedar-secret",), payload)

    def test_jsonrpc_parse_and_unknown_method_errors(self) -> None:
        api_key = self._api_key()

        malformed = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            content=b"{",
        )
        unknown = self.client.post(
            "/mcp",
            headers=self._mcp_headers(api_key),
            json={"jsonrpc": "2.0", "id": 17, "method": "resources/list"},
        )

        self.assertEqual(malformed.status_code, 200)
        self.assertEqual(malformed.json()["error"]["code"], -32700)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.json()["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
