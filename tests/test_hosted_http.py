import contextlib
import io
import json
import os
import sqlite3
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

from vexic.hosted_control_plane_http import create_app as create_control_plane_app
from vexic import hosted_http
from vexic.contract import (
    AppendTranscriptRequest,
    ExpandHistoryRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    SourceTranscriptMessage,
    TrustBoundary,
)
from vexic.hosted import (
    HostedInMemoryRateLimiter,
    HostedMemoryService,
    HostedRateLimitRule,
    HostedUsageEvent,
)
from vexic.embeddings import EMBEDDING_DIM
from vexic.ports import DreamPhasePorts
from vexic.storage import single_message_adapter
from vexic.hosted_http import create_app
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


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

    def _write_headers(
        self,
        api_key: str,
        *,
        project_id: str = "project-a",
        session_id: str = "session-a",
        agent_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            **self._auth(api_key),
            "X-Vexic-Project-Id": project_id,
            "X-Vexic-Session-Id": session_id,
        }
        if agent_id is not None:
            headers["X-Vexic-Agent-Id"] = agent_id
        return headers

    def _append_body(
        self,
        text: str,
        *,
        forbidden_values: tuple[str, ...] = (),
    ) -> dict[str, object]:
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content=text)])
        ).decode()
        return {
            "messages_json": [message_json],
            "redaction": {"forbidden_values": list(forbidden_values)},
        }

    def _control_auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer console-secret"}

    def test_health_requires_no_api_key(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_core_hosted_http_app_does_not_expose_control_plane_routes(self) -> None:
        response = self.client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 404)

    def test_control_plane_tenant_provisioning_requires_console_service_credential(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )

        response = client.post("/control/v1/clerk-orgs/org_123/tenant")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_control_plane_credentials_can_be_loaded_from_env_for_factory_startup(self) -> None:
        with patch.dict(
            os.environ,
            {"VEXIC_CONTROL_PLANE_TOKENS": "console-secret,rotated-secret"},
        ):
            client = TestClient(create_control_plane_app(self.service))

        response = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers={"Authorization": "Bearer rotated-secret"},
        )

        self.assertEqual(response.status_code, 200)

    def test_control_plane_env_token_parser_ignores_blank_entries(self) -> None:
        for env_value, accepted_tokens in (
            ("console-secret,", ("console-secret",)),
            ("console-secret,,rotated-secret", ("console-secret", "rotated-secret")),
        ):
            with self.subTest(env_value=env_value):
                with patch.dict(os.environ, {"VEXIC_CONTROL_PLANE_TOKENS": env_value}):
                    client = TestClient(create_control_plane_app(self.service))

                for token in accepted_tokens:
                    response = client.post(
                        "/control/v1/clerk-orgs/org_123/tenant",
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    self.assertEqual(response.status_code, 200)

    def test_control_plane_blank_clerk_org_returns_bad_request(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",)),
            raise_server_exceptions=False,
        )

        response = client.post(
            "/control/v1/clerk-orgs/%20/tenant",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    def test_control_plane_sqlite_integrity_errors_are_sanitized(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",)),
            raise_server_exceptions=False,
        )

        with patch.object(
            self.catalog,
            "create_control_project",
            side_effect=sqlite3.IntegrityError("UNIQUE constraint failed: secret"),
        ):
            with self.assertLogs("vexic.hosted_control_plane_http", level="WARNING") as logs:
                response = client.post(
                    "/control/v1/clerk-orgs/org_123/projects",
                    headers={
                        **self._control_auth(),
                        "X-Request-Id": "req-integrity",
                    },
                    json={"name": "A"},
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "conflict")
        self.assertNotIn("UNIQUE", response.text)
        self.assertNotIn("secret", response.text)
        log_text = "\n".join(logs.output)
        self.assertIn("category=integrity", log_text)
        self.assertIn("exception_type=IntegrityError", log_text)
        self.assertIn("path=/control/v1/clerk-orgs/org_123/projects", log_text)
        self.assertIn("correlation_id=req-integrity", log_text)
        self.assertNotIn("UNIQUE", log_text)
        self.assertNotIn("secret", log_text)

    def test_control_plane_sqlite_operational_errors_are_sanitized(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",)),
            raise_server_exceptions=False,
        )

        with patch.object(
            self.catalog,
            "list_control_projects",
            side_effect=sqlite3.OperationalError("database is locked: secret"),
        ):
            with self.assertLogs("vexic.hosted_control_plane_http", level="WARNING") as logs:
                response = client.get(
                    "/control/v1/clerk-orgs/org_123/projects",
                    headers={
                        **self._control_auth(),
                        "X-Request-Id": "req-locked",
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "storage_unavailable")
        self.assertNotIn("database is locked", response.text)
        self.assertNotIn("secret", response.text)
        log_text = "\n".join(logs.output)
        self.assertIn("category=retryable_operational", log_text)
        self.assertIn("exception_type=OperationalError", log_text)
        self.assertIn("path=/control/v1/clerk-orgs/org_123/projects", log_text)
        self.assertIn("correlation_id=req-locked", log_text)
        self.assertNotIn("database is locked", log_text)
        self.assertNotIn("secret", log_text)

        with patch.object(
            self.catalog,
            "list_control_projects",
            side_effect=sqlite3.OperationalError("syntax error near secret"),
        ):
            with self.assertLogs("vexic.hosted_control_plane_http", level="WARNING") as logs:
                response = client.get(
                    "/control/v1/clerk-orgs/org_123/projects",
                    headers={
                        **self._control_auth(),
                        "X-Correlation-Id": "corr-syntax",
                    },
                )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("syntax error", response.text)
        self.assertNotIn("secret", response.text)
        log_text = "\n".join(logs.output)
        self.assertIn("category=operational", log_text)
        self.assertIn("exception_type=OperationalError", log_text)
        self.assertIn("path=/control/v1/clerk-orgs/org_123/projects", log_text)
        self.assertIn("correlation_id=corr-syntax", log_text)
        self.assertNotIn("syntax error", log_text)
        self.assertNotIn("secret", log_text)

    def test_control_plane_tenant_provisioning_is_idempotent_per_clerk_org(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )

        first = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        )
        second = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["tenant"]["clerkOrgId"], "org_123")
        self.assertEqual(first.json()["tenant"]["tenantId"], second.json()["tenant"]["tenantId"])

    def test_control_plane_project_create_list_and_get_use_hosted_project_ids(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )

        created = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo", "environment": "staging"},
        )

        self.assertEqual(created.status_code, 201)
        project = created.json()["project"]
        self.assertTrue(project["id"].startswith("proj_"))
        self.assertEqual(project["name"], "Solo")
        self.assertEqual(project["environment"], "staging")

        listed = client.get(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
        )
        fetched = client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}",
            headers=self._control_auth(),
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual([item["id"] for item in listed.json()["projects"]], [project["id"]])
        self.assertEqual(fetched.json()["project"]["id"], project["id"])

    def test_control_plane_project_create_rejects_null_string_fields(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )

        null_name = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": None},
        )
        null_environment = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo", "environment": None},
        )

        self.assertEqual(null_name.status_code, 400)
        self.assertEqual(null_environment.status_code, 400)
        self.assertEqual(null_name.json()["error"]["code"], "invalid_request")
        self.assertEqual(null_environment.json()["error"]["code"], "invalid_request")

    def test_control_plane_project_put_is_idempotent(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )

        first = client.put(
            "/control/v1/clerk-orgs/org_123/projects/proj_manual",
            headers=self._control_auth(),
            json={"name": "Manual", "environment": "production"},
        )
        second = client.put(
            "/control/v1/clerk-orgs/org_123/projects/proj_manual",
            headers=self._control_auth(),
            json={"name": "Manual", "environment": "production"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["project"]["id"], "proj_manual")
        self.assertEqual(second.json()["project"]["id"], "proj_manual")

    def test_control_plane_project_put_hides_cross_tenant_project_id_collision(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )

        created = client.put(
            "/control/v1/clerk-orgs/org_a/projects/proj_manual",
            headers=self._control_auth(),
            json={"name": "Alpha", "environment": "production"},
        )
        collided = client.put(
            "/control/v1/clerk-orgs/org_b/projects/proj_manual",
            headers=self._control_auth(),
            json={"name": "Beta", "environment": "staging"},
        )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(collided.status_code, 404)
        self.assertEqual(collided.json()["error"]["code"], "not_found")

    def test_control_plane_key_create_and_list_hide_raw_secret(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]

        created = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker", "agentScope": "agent-a"},
        )

        self.assertEqual(created.status_code, 201)
        payload = created.json()
        self.assertTrue(payload["rawKey"].startswith("vx_"))
        self.assertEqual(payload["key"]["name"], "Worker")
        self.assertEqual(payload["key"]["capability"], "v1-memory")
        self.assertEqual(payload["key"]["agentScope"], "agent-a")
        self.assertNotIn(payload["rawKey"], json.dumps(payload["key"]))

        listed = client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual([key["id"] for key in listed.json()["keys"]], [payload["key"]["id"]])
        self.assertNotIn("rawKey", listed.text)
        self.assertNotIn("keyHash", listed.text)

    def test_control_plane_key_scope_template_runs_memory_round_trip_and_revocation(self) -> None:
        service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            dream_phase_ports=DreamPhasePorts(
                model_group="test",
                embed=lambda texts: [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts],
            ),
        )
        client = TestClient(
            create_control_plane_app(service, control_plane_tokens=("console-secret",))
        )
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]
        created = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker", "agentScope": "agent-a"},
        ).json()
        raw_key = created["rawKey"]
        key = created["key"]
        scope_template = key["scopeTemplate"]

        self.assertEqual(project["tenantId"], key["tenantId"])
        self.assertEqual(scope_template["tenant_id"], project["tenantId"])
        self.assertEqual(scope_template["project_id"], project["id"])
        self.assertEqual(scope_template["agent_id"], "agent-a")
        self.assertEqual(scope_template["principal"]["principal_id"], "agent-a")
        self.assertEqual(scope_template["trust_boundary"], "networked")
        self.assertIn("memory:search", scope_template["capabilities"])

        append_response = client.post(
            "/v1/append_transcript",
            headers=self._write_headers(raw_key, project_id=project["id"], agent_id="agent-a"),
            json=self._append_body("control plane scope cedar"),
        )
        search_scope = MemoryScope.model_validate(scope_template | {"session_id": "session-a"})
        search_response = client.post(
            "/v1/search_transcript",
            headers=self._auth(raw_key),
            json=SearchTranscriptRequest(
                scope=search_scope,
                query="cedar",
            ).model_dump(mode="json"),
        )
        long_term_response = client.post(
            "/v1/search_long_term",
            headers=self._auth(raw_key),
            json=SearchLongTermRequest(
                scope=MemoryScope.model_validate(scope_template),
                query="cedar",
            ).model_dump(mode="json"),
        )

        source_message = SourceTranscriptMessage(
            source_host="claude-code",
            source_session_id="claude-session",
            source_message_id="source-message-1",
            message_json=single_message_adapter.dump_json(
                ModelRequest(parts=[UserPromptPart(content="recorder hosted orchid")])
            ).decode(),
        )
        ingest_response = client.post(
            "/v1/ingest_source_transcript",
            headers=self._write_headers(raw_key, project_id=project["id"], agent_id="agent-a"),
            json={
                "messages": [source_message.model_dump(mode="json")],
                "redaction": {"forbidden_values": []},
            },
        )
        ingest_search_response = client.post(
            "/v1/search_transcript",
            headers=self._auth(raw_key),
            json=SearchTranscriptRequest(
                scope=search_scope,
                query="orchid",
            ).model_dump(mode="json"),
        )
        revoked = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys/{key['id']}/revoke",
            headers=self._control_auth(),
        )
        denied = client.post(
            "/v1/search_long_term",
            headers=self._auth(raw_key),
            json=SearchLongTermRequest(
                scope=MemoryScope.model_validate(scope_template),
                query="cedar",
            ).model_dump(mode="json"),
        )

        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(
            [hit["body"] for hit in search_response.json()["hits"]],
            ["User: control plane scope cedar"],
        )
        self.assertEqual(long_term_response.status_code, 200)
        self.assertEqual(long_term_response.json()["facts"], [])
        self.assertEqual(long_term_response.json()["candidate_notes"], [])
        self.assertEqual(ingest_response.status_code, 200)
        self.assertEqual(ingest_response.json()["items"][0]["status"], "inserted")
        self.assertEqual(
            [hit["body"] for hit in ingest_search_response.json()["hits"]],
            ["User: recorder hosted orchid"],
        )
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(denied.status_code, 401)

    def test_control_plane_key_create_rejects_null_string_fields(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]

        null_name = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": None},
        )
        null_agent_scope = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker", "agentScope": None},
        )

        self.assertEqual(null_name.status_code, 400)
        self.assertEqual(null_agent_scope.status_code, 400)
        self.assertEqual(null_name.json()["error"]["code"], "invalid_request")
        self.assertEqual(null_agent_scope.json()["error"]["code"], "invalid_request")

    def test_control_plane_key_revoke_invalidates_v1_memory_access(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        tenant = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]
        created = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker"},
        ).json()
        raw_key = created["rawKey"]
        key_id = created["key"]["id"]
        append_response = client.post(
            "/v1/append_transcript",
            headers=self._write_headers(raw_key, project_id=project["id"]),
            json=self._append_body("control plane key cedar"),
        )

        self.assertEqual(append_response.status_code, 200)

        revoked = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys/{key_id}/revoke",
            headers=self._control_auth(),
        )
        denied = client.post(
            "/v1/search_transcript",
            headers=self._auth(raw_key),
            json=SearchTranscriptRequest(
                scope=_scope(
                    tenant_id=tenant["tenantId"],
                    project_id=project["id"],
                    capabilities={MemoryCapability.SEARCH},
                ),
                query="cedar",
            ).model_dump(mode="json"),
        )

        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(denied.status_code, 401)

    def test_control_plane_key_authenticates_against_mcp(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        )
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]
        raw_key = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker"},
        ).json()["rawKey"]
        client.post(
            "/v1/append_transcript",
            headers=self._write_headers(raw_key, project_id=project["id"]),
            json=self._append_body("mcp cedar"),
        )

        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {raw_key}",
                "X-Vexic-Project-Id": project["id"],
                "X-Vexic-Session-Id": "session-a",
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_transcript",
                    "arguments": {"query": "cedar"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual([hit["body"] for hit in payload["hits"]], ["User: mcp cedar"])

    def test_control_plane_key_appends_without_body_tenant_and_reads_through_mcp(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        )
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]
        raw_key = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker"},
        ).json()["rawKey"]
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="header-bound cedar")])
        ).decode()
        write_headers = {
            **self._auth(raw_key),
            "X-Vexic-Project-Id": project["id"],
            "X-Vexic-Session-Id": "session-a",
        }

        append_response = client.post(
            "/v1/append_transcript",
            headers=write_headers,
            json={
                "messages_json": [message_json],
                "redaction": {"forbidden_values": []},
            },
        )
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {raw_key}",
                "X-Vexic-Project-Id": project["id"],
                "X-Vexic-Session-Id": "session-a",
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_transcript",
                    "arguments": {"query": "cedar"},
                },
            },
        )

        self.assertEqual(append_response.status_code, 200)
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual([hit["body"] for hit in payload["hits"]], ["User: header-bound cedar"])

    def test_control_plane_key_ingests_source_rows_without_body_tenant(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        )
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]
        raw_key = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker"},
        ).json()["rawKey"]
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="ingested cedar")])
        ).decode()

        ingest_response = client.post(
            "/v1/ingest_source_transcript",
            headers={
                **self._auth(raw_key),
                "X-Vexic-Project-Id": project["id"],
                "X-Vexic-Session-Id": "session-a",
            },
            json={
                "messages": [
                    {
                        "source_host": "claude-code",
                        "source_session_id": "source-session-a",
                        "source_message_id": "source-message-a",
                        "message_json": message_json,
                    }
                ],
                "redaction": {"forbidden_values": []},
            },
        )
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {raw_key}",
                "X-Vexic-Project-Id": project["id"],
                "X-Vexic-Session-Id": "session-a",
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_transcript",
                    "arguments": {"query": "cedar"},
                },
            },
        )

        self.assertEqual(ingest_response.status_code, 200)
        self.assertEqual(ingest_response.json()["items"][0]["status"], "inserted")
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual([hit["body"] for hit in payload["hits"]], ["User: ingested cedar"])

    def test_hosted_writes_reject_unsupported_scope_inputs(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE})

        for extra in (
            {"scope": {"tenant_id": "tenant-a"}},
            {"user_id": "user-a"},
            {"correlation_id": "trace-a"},
        ):
            with self.subTest(extra=extra):
                response = self.client.post(
                    "/v1/append_transcript",
                    headers=self._write_headers(api_key),
                    json=self._append_body("scope cedar") | extra,
                )
                self.assertEqual(response.status_code, 422)

        for header in ("X-Vexic-User-Id", "X-Vexic-Correlation-Id"):
            with self.subTest(header=header):
                response = self.client.post(
                    "/v1/append_transcript",
                    headers=self._write_headers(api_key) | {header: "unsupported"},
                    json=self._append_body("scope cedar"),
                )
                self.assertEqual(response.status_code, 400)

    def test_hosted_writes_require_project_and_session_headers(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE})

        for missing in ("X-Vexic-Project-Id", "X-Vexic-Session-Id"):
            with self.subTest(missing=missing):
                headers = self._write_headers(api_key)
                del headers[missing]
                response = self.client.post(
                    "/v1/append_transcript",
                    headers=headers,
                    json=self._append_body("missing header cedar"),
                )
                self.assertEqual(response.status_code, 400)

    def test_hosted_write_accepts_legacy_v1_api_key_header_for_self_hosting(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH})
        headers = {
            "X-Vexic-Api-Key": api_key,
            "X-Vexic-Project-Id": "project-a",
            "X-Vexic-Session-Id": "session-a",
        }

        response = self.client.post(
            "/v1/append_transcript",
            headers=headers,
            json=self._append_body("legacy header cedar"),
        )

        self.assertEqual(response.status_code, 200)

    def test_hosted_write_bearer_takes_precedence_over_legacy_api_key_header(self) -> None:
        bearer_key = self._api_key(
            capabilities={MemoryCapability.WRITE},
            tenant_id="tenant-a",
            project_ids={"project-a"},
        )
        legacy_key = self._api_key(
            capabilities={MemoryCapability.WRITE},
            tenant_id="tenant-b",
            project_ids={"project-b"},
        )

        response = self.client.post(
            "/v1/append_transcript",
            headers={
                "Authorization": f"Bearer {bearer_key}",
                "X-Vexic-Api-Key": legacy_key,
                "X-Vexic-Project-Id": "project-a",
                "X-Vexic-Session-Id": "session-a",
            },
            json=self._append_body("bearer wins cedar"),
        )

        self.assertEqual(response.status_code, 200)

    def test_hosted_write_strips_scope_headers_before_binding(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-a"},
            agent_ids={"agent-a"},
        ).raw_key

        append_response = self.client.post(
            "/v1/append_transcript",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Vexic-Project-Id": " project-a ",
                "X-Vexic-Session-Id": " session-a ",
                "X-Vexic-Agent-Id": " agent-a ",
            },
            json=self._append_body("trimmed header cedar"),
        )
        search_response = self.client.post(
            "/v1/search_transcript",
            headers=self._auth(api_key),
            json=SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}).model_copy(
                    update={"agent_id": "agent-a"}
                ),
                query="cedar",
            ).model_dump(mode="json"),
        )

        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(
            [hit["body"] for hit in search_response.json()["hits"]],
            ["User: trimmed header cedar"],
        )

    def test_hosted_search_transcript_accepts_header_bound_scope(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH})
        append_response = self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key),
            json=self._append_body("header scoped cedar"),
        )

        search_response = self.client.post(
            "/v1/search_transcript",
            headers=self._write_headers(api_key),
            json={"query": "cedar", "limit": 5},
        )

        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(
            [hit["body"] for hit in search_response.json()["hits"]],
            ["User: header scoped cedar"],
        )

    def test_hosted_append_rejects_forbidden_values_without_persisting(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH})

        append_response = self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key),
            json=self._append_body("cedar-secret", forbidden_values=("cedar-secret",)),
        )
        search_response = self.client.post(
            "/v1/search_transcript",
            headers=self._auth(api_key),
            json=SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="cedar-secret",
            ).model_dump(mode="json"),
        )

        self.assertEqual(append_response.status_code, 400)
        self.assertEqual(search_response.json()["hits"], [])

    def test_hosted_append_rejects_polluted_rows_without_persisting(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH})
        polluted = single_message_adapter.dump_json(
            ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={})])
        ).decode()

        append_response = self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key),
            json={"messages_json": [polluted], "redaction": {"forbidden_values": []}},
        )
        search_response = self.client.post(
            "/v1/search_transcript",
            headers=self._auth(api_key),
            json=SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="lookup",
            ).model_dump(mode="json"),
        )

        self.assertEqual(append_response.status_code, 400)
        self.assertEqual(search_response.json()["hits"], [])

    def test_hosted_write_header_rejection_records_sanitized_telemetry(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE})

        response = self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key) | {"X-Vexic-User-Id": "user-a"},
            json=self._append_body("telemetry cedar"),
        )
        audit_events = self.catalog.audit_events("tenant-a")
        usage_events = self.catalog.usage_events("tenant-a")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(audit_events[-1].operation, "append_transcript")
        self.assertEqual(audit_events[-1].status, "error")
        self.assertEqual(audit_events[-1].error_type, "ValueError")
        self.assertEqual(usage_events[-1].operation, "append_transcript")
        self.assertEqual(usage_events[-1].status, "error")
        self.assertNotIn("telemetry cedar", repr(audit_events) + repr(usage_events))

    def test_hosted_write_unexpected_auth_failure_returns_json_and_telemetry(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE})

        def _boom(_: str) -> object:
            raise RuntimeError("key store unavailable: cedar-secret")

        self.service.api_keys.authenticate = _boom  # type: ignore[method-assign]

        response = self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key),
            json=self._append_body("preflight cedar"),
        )
        audit_events = self.catalog.audit_events(None)
        usage_events = self.catalog.usage_events(None)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("cedar-secret", response.text)
        self.assertEqual(audit_events[-1].operation, "append_transcript")
        self.assertEqual(audit_events[-1].status, "error")
        self.assertEqual(audit_events[-1].error_type, "RuntimeError")
        self.assertEqual(usage_events[-1].operation, "append_transcript")
        self.assertEqual(usage_events[-1].status, "error")

    def test_hosted_ingest_rejects_polluted_rows_per_row(self) -> None:
        api_key = self._api_key(capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH})
        polluted = single_message_adapter.dump_json(
            ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={})])
        ).decode()

        ingest_response = self.client.post(
            "/v1/ingest_source_transcript",
            headers=self._write_headers(api_key),
            json={
                "messages": [
                    {
                        "source_host": "claude-code",
                        "source_session_id": "source-session-a",
                        "source_message_id": "tool-call",
                        "message_json": polluted,
                    }
                ],
                "redaction": {"forbidden_values": []},
            },
        )
        search_response = self.client.post(
            "/v1/search_transcript",
            headers=self._auth(api_key),
            json=SearchTranscriptRequest(
                scope=_scope(capabilities={MemoryCapability.SEARCH}),
                query="lookup",
            ).model_dump(mode="json"),
        )

        self.assertEqual(ingest_response.status_code, 200)
        self.assertEqual(ingest_response.json()["items"][0]["status"], "rejected")
        self.assertEqual(search_response.json()["hits"], [])

    def test_control_plane_routes_and_keys_are_tenant_isolated(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        tenant_a = client.post(
            "/control/v1/clerk-orgs/org_a/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        tenant_b = client.post(
            "/control/v1/clerk-orgs/org_b/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        project_a = client.post(
            "/control/v1/clerk-orgs/org_a/projects",
            headers=self._control_auth(),
            json={"name": "A"},
        ).json()["project"]
        project_b = client.post(
            "/control/v1/clerk-orgs/org_b/projects",
            headers=self._control_auth(),
            json={"name": "B"},
        ).json()["project"]
        raw_key_a = client.post(
            f"/control/v1/clerk-orgs/org_a/projects/{project_a['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker A"},
        ).json()["rawKey"]

        hidden = client.get(
            f"/control/v1/clerk-orgs/org_b/projects/{project_a['id']}",
            headers=self._control_auth(),
        )
        denied = client.post(
            "/v1/search_transcript",
            headers=self._auth(raw_key_a),
            json=SearchTranscriptRequest(
                scope=_scope(
                    tenant_id=tenant_b["tenantId"],
                    project_id=project_b["id"],
                    capabilities={MemoryCapability.SEARCH},
                ),
                query="cedar",
            ).model_dump(mode="json"),
        )

        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(denied.status_code, 403)
        self.assertNotEqual(tenant_a["tenantId"], tenant_b["tenantId"])

    def test_control_plane_usage_reads_are_tenant_scoped_and_project_attributed(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        tenant = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        project_a = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "A"},
        ).json()["project"]
        project_b = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "B"},
        ).json()["project"]
        raw_key_a = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project_a['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker A"},
        ).json()["rawKey"]
        raw_key_b = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project_b['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker B"},
        ).json()["rawKey"]

        with patch("vexic.hosted._now", return_value="2026-06-10T00:00:00Z"):
            for raw_key, project_id, text in (
                (raw_key_a, project_a["id"], "alpha cedar"),
                (raw_key_b, project_b["id"], "beta cedar"),
            ):
                client.post(
                    "/v1/append_transcript",
                    headers=self._write_headers(raw_key, project_id=project_id),
                    json=self._append_body(text),
                )

        for event in (
            HostedUsageEvent(
                kind="request",
                operation="legacy_unattributed",
                tenant_id=tenant["tenantId"],
                principal_id="legacy",
                status="ok",
                recorded_at="2026-06-11T00:00:00Z",
            ),
            HostedUsageEvent(
                kind="request",
                operation="old_unattributed",
                tenant_id=tenant["tenantId"],
                principal_id="legacy",
                status="ok",
                recorded_at="2000-01-01T00:00:00Z",
            ),
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id=tenant["tenantId"],
                principal_id="legacy",
                status="ok",
                recorded_at="2000-01-01T00:00:00Z",
                project_id=project_a["id"],
            ),
        ):
            self.catalog.record_usage_event(event)

        with patch(
            "vexic.hosted_control_plane_http._usage_period",
            return_value=("2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        ):
            tenant_usage = client.get(
                "/control/v1/clerk-orgs/org_123/usage",
                headers=self._control_auth(),
            )
            project_usage = client.get(
                f"/control/v1/clerk-orgs/org_123/projects/{project_a['id']}/usage",
                headers=self._control_auth(),
            )

        self.assertEqual(tenant_usage.status_code, 200)
        self.assertEqual(project_usage.status_code, 200)
        self.assertEqual(tenant_usage.json()["usage"]["totals"]["requests"], 3)
        self.assertEqual(project_usage.json()["usage"]["totals"]["requests"], 1)
        self.assertEqual(project_usage.json()["usage"]["projectId"], project_a["id"])

    def test_control_plane_blank_token_config_fails_closed(self) -> None:
        client = TestClient(create_control_plane_app(self.service, control_plane_tokens=("",)))

        response = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 401)

    def test_control_plane_rotation_accepts_multiple_tokens(self) -> None:
        client = TestClient(
            create_control_plane_app(
                self.service,
                control_plane_tokens=("console-secret", "rotated-secret"),
            )
        )

        first = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers={"Authorization": "Bearer console-secret"},
        )
        second = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers={"Authorization": "Bearer rotated-secret"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["tenant"]["tenantId"], second.json()["tenant"]["tenantId"])

    def test_control_plane_compare_digest_checks_each_configured_token(self) -> None:
        calls: list[str] = []

        def fake_compare_digest(left: str, right: str) -> bool:
            calls.append(left)
            return left == right

        with patch("vexic.hosted_control_plane_http.hmac.compare_digest", side_effect=fake_compare_digest):
            client = TestClient(
                create_control_plane_app(
                    self.service,
                    control_plane_tokens=("console-secret", "rotated-secret"),
                )
            )
            response = client.post(
                "/control/v1/clerk-orgs/org_123/tenant",
                headers=self._control_auth(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["console-secret", "rotated-secret"])

    def test_control_plane_auth_failures_do_not_echo_supplied_token(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        bad_token = "leaky-console-token"

        response = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers={"Authorization": f"Bearer {bad_token}"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(bad_token, response.text)

    def test_control_plane_agent_scoped_key_only_allows_matching_agent_id(self) -> None:
        client = TestClient(
            create_control_plane_app(self.service, control_plane_tokens=("console-secret",))
        )
        tenant = client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        project = client.post(
            "/control/v1/clerk-orgs/org_123/projects",
            headers=self._control_auth(),
            json={"name": "Solo"},
        ).json()["project"]
        raw_key = client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
            json={"name": "Worker", "agentScope": "agent-a"},
        ).json()["rawKey"]
        denied_scope = _scope(
            tenant_id=tenant["tenantId"],
            project_id=project["id"],
            capabilities={MemoryCapability.SEARCH},
        ).model_copy(update={"agent_id": "agent-b"})

        allowed = client.post(
            "/v1/append_transcript",
            headers=self._write_headers(raw_key, project_id=project["id"], agent_id="agent-a"),
            json=self._append_body("agent cedar"),
        )
        denied = client.post(
            "/v1/search_transcript",
            headers=self._auth(raw_key),
            json=SearchTranscriptRequest(
                scope=denied_scope,
                query="cedar",
            ).model_dump(mode="json"),
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)

    def test_append_and_search_round_trip_through_hosted_service(self) -> None:
        api_key = self._api_key(
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH}
        )
        append_response = self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key),
            json=self._append_body("hosted http cedar"),
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
            headers=self._write_headers(api_key),
            json={"messages_json": [], "redaction": {"forbidden_values": []}},
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
            headers=self._write_headers(api_key),
            json=self._append_body("session alpha first"),
        )
        self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key, session_id="session-b"),
            json={
                "messages_json": [
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content=f"session beta {index}")])
                    ).decode()
                    for index in range(100)
                ],
                "redaction": {"forbidden_values": []},
            },
        )
        last_response = self.client.post(
            "/v1/append_transcript",
            headers=self._write_headers(api_key),
            json=self._append_body("session alpha last"),
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
            headers=self._write_headers(api_key),
            json={
                "messages_json": [
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content=f"session alpha {index}")])
                    ).decode()
                    for index in range(99)
                ],
                "redaction": {"forbidden_values": []},
            },
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

    def test_run_dream_phase_cli_uses_host_supplied_adapter(self) -> None:
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        api_key = self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-a",
            capabilities={MemoryCapability.ADMIN_REBUILD, MemoryCapability.WRITE},
            project_ids={"project-a"},
            agent_ids={"agent-a"},
        )
        service = HostedMemoryService(self.catalog, self.keys, telemetry=self.catalog)
        scoped = _scope(capabilities={MemoryCapability.WRITE}).model_copy(
            update={"agent_id": "agent-a"}
        )
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="hosted worker ultraviolet")])
        )
        adapter = Path(self.temp_dir.name) / "adapter.py"
        adapter.write_text(
            textwrap.dedent(
                """
                import re

                from vexic.embeddings import EMBEDDING_DIM
                from vexic.models import FactCandidate

                class _Result:
                    def __init__(self, output):
                        self.output = output

                    def usage(self):
                        return type(
                            "Usage",
                            (),
                            {
                                "requests": 1,
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "total_tokens": 5,
                            },
                        )()

                class _ExtractionAgent:
                    async def run(self, transcript):
                        message_id = int(re.search(r"message_id=(\\d+)", transcript).group(1))
                        return _Result(
                            [
                                FactCandidate(
                                    fact_text="Ryan's favorite color is ultraviolet.",
                                    subject="Ryan",
                                    category="preference",
                                    importance=7,
                                    confidence=0.9,
                                    source_message_ids=[message_id],
                                )
                            ]
                        )

                def build_extraction_agent(model_group, secrets=None):
                    return _ExtractionAgent()

                def build_rem_agent(model_group, secrets=None):
                    raise AssertionError("REM should not run in this test")

                def build_contradiction_agent(model_group, secrets=None):
                    raise AssertionError("Deep should not run in this test")

                def embed_texts(texts):
                    return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]
                """
            )
        )

        async def append() -> None:
            await service.append_transcript(
                api_key.raw_key,
                AppendTranscriptRequest(
                    scope=scoped,
                    messages_json=[message_json],
                    redaction=RedactionContext(forbidden_values=()),
                ),
            )

        import asyncio

        asyncio.run(append())
        stdout = io.StringIO()

        with patch.dict(os.environ, {"VEXIC_TEST_API_KEY": f"{api_key.raw_key}\n"}):
            with contextlib.redirect_stdout(stdout):
                exit_code = _main_result(
                    [
                        "run-dream-phase",
                        "--root",
                        self.temp_dir.name,
                        "--api-key-env",
                        "VEXIC_TEST_API_KEY",
                        "--adapter",
                        str(adapter),
                        "--model-group",
                        "fake",
                        "--tenant-id",
                        "tenant-a",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "session-a",
                        "--agent-id",
                        "agent-a",
                        "--phase",
                        "light",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["result"]["status"], "ok")
        self.assertEqual(
            [event["status"] for event in payload["job_events"]],
            ["running", "ok"],
        )
        self.assertEqual(
            [event["operation"] for event in payload["usage_events"]],
            ["run_dream_phase", "run_dream_phase"],
        )
        job_usage = [
            event
            for event in self.catalog.usage_events("tenant-a")
            if event.kind == "job"
        ]
        self.assertEqual(job_usage[-1].model_requests, 1)
        self.assertEqual(job_usage[-1].total_tokens, 5)

    def test_run_dream_phase_cli_missing_adapter_fails_as_missing_host_port(self) -> None:
        stderr = io.StringIO()

        with patch.dict(os.environ, {"VEXIC_TEST_API_KEY": "vx_fake_secret"}):
            with contextlib.redirect_stderr(stderr):
                exit_code = _main_result(
                    [
                        "run-dream-phase",
                        "--root",
                        self.temp_dir.name,
                        "--api-key-env",
                        "VEXIC_TEST_API_KEY",
                        "--adapter",
                        str(Path(self.temp_dir.name) / "missing.py"),
                        "--model-group",
                        "fake",
                        "--tenant-id",
                        "tenant-a",
                        "--phase",
                        "light",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("requires a host-supplied model port", stderr.getvalue())


def _main_result(argv: list[str]) -> int:
    try:
        return hosted_http.main(argv)
    except SystemExit as exc:
        return int(exc.code)


if __name__ == "__main__":
    unittest.main()
