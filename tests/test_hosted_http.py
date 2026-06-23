import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    AppendTranscriptRequest,
    ExpandHistoryRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import HostedInMemoryRateLimiter, HostedMemoryService, HostedRateLimitRule
from vexic.storage import single_message_adapter
from vexic_hosted_http import create_app
from vexic_hosted_local import HostedApiKeyStore, HostedTenantCatalog


def _scope(
    *,
    tenant_id: str = "tenant-a",
    project_id: str | None = "project-a",
    capabilities: set[MemoryCapability],
) -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant_id,
        project_id=project_id,
        session_id="session-a",
        principal=Principal(
            principal_id="caller-supplied",
            principal_type=PrincipalType.HUMAN,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities=capabilities,
    )


class HostedHttpTests(unittest.TestCase):
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
        capabilities: set[MemoryCapability],
        tenant_id: str = "tenant-a",
        project_ids: set[str] | None = None,
    ) -> str:
        project_ids = project_ids or {"project-a"}
        self.catalog.provision_tenant(tenant_id, project_ids=project_ids)
        return self.keys.create_key(
            tenant_id=tenant_id,
            principal_id="agent-a",
            capabilities=capabilities,
            project_ids=project_ids,
        ).raw_key

    def _auth(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def test_health_requires_no_api_key(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_append_and_search_round_trip_through_hosted_service(self) -> None:
        api_key = self._api_key(
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH}
        )
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="hosted http cedar")])
        )

        append_response = self.client.post(
            "/v1/append_transcript",
            headers=self._auth(api_key),
            json=AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[message_json],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        )
        search_response = self.client.post(
            "/v1/search_transcript",
            headers=self._auth(api_key),
            json=SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar",
            ).model_dump(mode="json"),
        )

        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(append_response.json()["message_ids"], [1])
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(
            [hit["body"] for hit in search_response.json()["hits"]],
            ["User: hosted http cedar"],
        )
        self.assertNotIn("rowid", search_response.text.lower())
        self.assertNotIn("rank", search_response.text.lower())

    def test_api_key_auth_and_capability_errors_are_mapped(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.SEARCH})
        request = SearchTranscriptRequest(
            scope=_scope(capabilities={MemoryCapability.SEARCH}),
            query="cedar",
        ).model_dump(mode="json")

        missing_auth = self.client.post("/v1/search_transcript", json=request)
        append_denied = self.client.post(
            "/v1/append_transcript",
            headers=self._auth(api_key),
            json=AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        )

        self.assertEqual(missing_auth.status_code, 401)
        self.assertEqual(append_denied.status_code, 403)
        self.assertEqual(append_denied.json()["error"]["code"], "permission_denied")

    def test_search_request_caps_are_enforced_before_delegation(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.SEARCH})

        malformed_length = self.client.post(
            "/v1/search_transcript",
            headers={"Content-Length": "nope"},
            content=b"{}",
        )
        response = self.client.post(
            "/v1/search_transcript",
            headers=self._auth(api_key),
            json=SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="x" * 1001,
            ).model_dump(mode="json"),
        )

        self.assertEqual(malformed_length.status_code, 400)
        self.assertEqual(malformed_length.json()["error"]["code"], "invalid_request")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")

    def test_rate_limit_sets_retry_after_without_leaking_payload(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.SEARCH})
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(
                default_rule=HostedRateLimitRule(limit=1, window_seconds=60),
            ),
        )
        client = TestClient(create_app(service))
        request = SearchTranscriptRequest(
            scope=_scope(capabilities={MemoryCapability.SEARCH}),
            query="cedar-secret",
        ).model_dump(mode="json")

        client.post("/v1/search_transcript", headers=self._auth(api_key), json=request)
        response = client.post(
            "/v1/search_transcript",
            headers=self._auth(api_key),
            json=request,
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        self.assertNotIn("cedar-secret", response.text)

    def test_expand_history_caps_scoped_rows_not_global_id_span(self) -> None:
        api_key = self._api_key(
            capabilities={MemoryCapability.WRITE, MemoryCapability.EXPAND_HISTORY}
        )
        first_response = self.client.post(
            "/v1/append_transcript",
            headers=self._auth(api_key),
            json=AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="session alpha first")])
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        )
        beta_scope = _scope(capabilities={MemoryCapability.WRITE}).model_copy(
            update={"session_id": "session-b"}
        )
        self.client.post(
            "/v1/append_transcript",
            headers=self._auth(api_key),
            json=AppendTranscriptRequest(
                scope=beta_scope,
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content=f"session beta {index}")])
                    )
                    for index in range(100)
                ],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        )
        last_response = self.client.post(
            "/v1/append_transcript",
            headers=self._auth(api_key),
            json=AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="session alpha last")])
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        )

        response = self.client.post(
            "/v1/expand_history",
            headers=self._auth(api_key),
            json=ExpandHistoryRequest(
                scope=_scope(capabilities={MemoryCapability.EXPAND_HISTORY}),
                first_message_id=first_response.json()["message_ids"][0],
                last_message_id=last_response.json()["message_ids"][0],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("session alpha first", response.json()["text"])
        self.assertIn("session alpha last", response.json()["text"])
        self.assertNotIn("session beta", response.text)

        message_ids = self.client.post(
            "/v1/append_transcript",
            headers=self._auth(api_key),
            json=AppendTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.WRITE}),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content=f"session alpha {index}")])
                    )
                    for index in range(99)
                ],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        ).json()["message_ids"]
        response = self.client.post(
            "/v1/expand_history",
            headers=self._auth(api_key),
            json=ExpandHistoryRequest(
                scope=_scope(capabilities={MemoryCapability.EXPAND_HISTORY}),
                first_message_id=first_response.json()["message_ids"][0],
                last_message_id=message_ids[-1],
                redaction=RedactionContext(forbidden_values=()),
            ).model_dump(mode="json"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")


if __name__ == "__main__":
    unittest.main()
