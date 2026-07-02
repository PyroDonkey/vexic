import contextlib
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, UserPromptPart

from adapters.turso_adapter import TenantTokenCache, make_customer_target_resolver
from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import HostedMemoryService, HostedTenant, resolve_storage_backend
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.service import LocalMemoryService
from vexic.storage import StorageTarget, single_message_adapter
from vexic.storage.connection import connect as storage_connect

# Live gate (Task 8 / COA-273 P2): the round-trip and latency tests below hit
# the real Turso dev DB named by TURSO_DATABASE_URL/TURSO_AUTH_TOKEN, so they
# only run when creds AND the optional `libsql` (vexic[hosted]) extra are
# present. Default `uv run pytest` (no creds) collects but SKIPS them.
_TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_HAS_TURSO = bool(_TURSO_URL and _TURSO_TOKEN and importlib.util.find_spec("libsql"))


def test_default_is_local():
    assert resolve_storage_backend({}) == "local"


def test_turso_flag_selected():
    assert resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "turso"}) == "turso"


def test_unknown_flag_rejected():
    with pytest.raises(ValueError):
        resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "postgres"})


# ---------------------------------------------------------------------------
# Task 16 (COA-273 P4): the per-tenant customer-target RESOLVER seam replaces
# the Task-7b single-DB override. The resolver derives the Turso db NAME from
# the tenant's stored `customer_target` DSN and mints a short-lived, DB-scoped
# token via `TenantTokenCache` to build a connectable `StorageTarget`.


class _FakePort:
    """Fake TursoProvisioningPort for the token cache: records mint_token
    calls per db_name and returns a deterministic, distinguishable fake jwt.
    No network, no secrets."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self._counter = 0

    def mint_token(self, db_name: str, *, expiration: str = "5m", read_only: bool = True) -> str:
        self._counter += 1
        self.calls.append((db_name, expiration, read_only))
        return f"jwt-for-{db_name}-{self._counter}"


def _tenant(customer_target: str | None) -> HostedTenant:
    return HostedTenant(
        tenant_id="tenant-a",
        db_path="/local/tenant-a.db",
        project_ids=frozenset({"project-a"}),
        customer_target=customer_target,
    )


def test_resolver_returns_none_when_customer_target_unset():
    cache = TenantTokenCache(_FakePort())
    resolver = make_customer_target_resolver(cache, org="pyrodonkey")

    assert resolver(_tenant(None)) is None


def test_resolver_builds_storage_target_and_strips_org_suffix_for_db_name():
    port = _FakePort()
    cache = TenantTokenCache(port)
    resolver = make_customer_target_resolver(cache, org="pyrodonkey")
    dsn = "libsql://vexic-t123-pyrodonkey.aws-us-west-2.turso.io"

    target = resolver(_tenant(dsn))

    assert isinstance(target, StorageTarget)
    # The DSN is preserved verbatim as the connection target...
    assert target.target == dsn
    # ...and the token is minted against the derived db NAME, which is the
    # first hostname label with the `-{org}` suffix removed.
    assert target.auth_token == "jwt-for-vexic-t123-1"
    assert port.calls[0][0] == "vexic-t123"


def test_resolver_db_name_without_org_suffix_is_used_verbatim():
    # If the first hostname label does not carry the `-{org}` suffix, the
    # label is used as-is (removesuffix is a no-op).
    port = _FakePort()
    cache = TenantTokenCache(port)
    resolver = make_customer_target_resolver(cache, org="pyrodonkey")
    dsn = "libsql://vexic-t999.aws-us-west-2.turso.io"

    target = resolver(_tenant(dsn))

    assert target.target == dsn
    assert port.calls[0][0] == "vexic-t999"


def test_local_service_uses_resolver_target_when_present(tmp_path, monkeypatch):
    root = tmp_path
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    dsn = "libsql://vexic-t123-pyrodonkey.aws-us-west-2.turso.io"
    catalog.provision_tenant("tenant-a", project_ids={"project-a"}, customer_target=dsn)
    tenant = catalog.get_tenant("tenant-a")
    port = _FakePort()
    cache = TenantTokenCache(port)
    resolver = make_customer_target_resolver(cache, org="pyrodonkey")
    # `_local_service` requests schema init against the resolved target
    # (`init_db`'s process-level memo makes that cheap after the first real
    # call, but "libsql://vexic-t123-..." is never a reachable host) -- this
    # unit test is only about the db_path wiring, so stub init_schema.
    monkeypatch.setattr(LocalMemoryService, "init_schema", lambda self: None)

    service = HostedMemoryService(
        catalog,
        keys,
        customer_target_resolver=resolver,
    )
    local_service = service._local_service(tenant)

    assert isinstance(local_service, LocalMemoryService)
    assert isinstance(local_service.db_path, StorageTarget)
    assert local_service.db_path.target == dsn
    assert local_service.db_path.auth_token == "jwt-for-vexic-t123-1"


def test_local_service_falls_back_to_db_path_when_resolver_returns_none(tmp_path):
    root = tmp_path
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    # No customer_target -> resolver returns None -> use the local db_path.
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    tenant = catalog.get_tenant("tenant-a")
    port = _FakePort()
    cache = TenantTokenCache(port)
    resolver = make_customer_target_resolver(cache, org="pyrodonkey")

    service = HostedMemoryService(
        catalog,
        keys,
        customer_target_resolver=resolver,
    )
    local_service = service._local_service(tenant)

    assert isinstance(local_service, LocalMemoryService)
    assert local_service.db_path == tenant.db_path
    assert port.calls == []


def test_local_service_uses_db_path_when_no_resolver(tmp_path):
    root = tmp_path
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    tenant = catalog.get_tenant("tenant-a")

    service = HostedMemoryService(catalog, keys)
    local_service = service._local_service(tenant)

    assert local_service.db_path == tenant.db_path


class _FakeProvisioning:
    """Test double for the factory's `_TursoProvisioning` seam. Builds a fake
    port + real TenantTokenCache + real resolver -- no network, no secrets."""

    def __init__(self, created_dsn: str) -> None:
        self.created_dsn = created_dsn
        self.create_calls: list[str] = []
        self.mint_port = _FakePort()

    def build_port(self, env):
        created_dsn = self.created_dsn
        create_calls = self.create_calls

        class _FakeProvPort(_FakePort):
            def create_database(self, name: str) -> str:
                create_calls.append(name)
                return created_dsn

        port = _FakeProvPort()
        # keep a single mint counter surface for assertions
        self.mint_port = port
        return port

    def build_token_cache(self, port):
        return TenantTokenCache(port)

    def build_resolver(self, token_cache, *, org: str):
        return make_customer_target_resolver(token_cache, org=org)


def test_factory_turso_branch_provisions_dogfood_and_wires_resolver(monkeypatch, tmp_path):
    from vexic.hosted_http import _customer_database_name, create_service_from_env

    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "turso")
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("TURSO_ORG", "pyrodonkey")
    monkeypatch.setenv("VEXIC_DOGFOOD_TENANT_ID", "tenant-dog")
    # Pre-provision the dogfood tenant locally (no customer_target yet).
    catalog = HostedTenantCatalog(tmp_path)
    catalog.provision_tenant("tenant-dog", project_ids={"project-dog"})

    db_name = _customer_database_name("tenant-dog")
    dsn = f"libsql://{db_name}-pyrodonkey.aws-us-west-2.turso.io"
    provisioning = _FakeProvisioning(dsn)

    service = create_service_from_env(turso_provisioning=provisioning)

    assert isinstance(service, HostedMemoryService)
    # The dogfood tenant now carries a per-tenant DSN (not a shared DB).
    assert provisioning.create_calls == [db_name]
    tenant = service.catalog.get_tenant("tenant-dog")
    assert tenant.customer_target == dsn
    # The resolver is wired and mints a token for the derived db name.
    target = service._customer_target_resolver(tenant)
    assert isinstance(target, StorageTarget)
    assert target.target == dsn
    assert provisioning.mint_port.calls[0][0] == db_name


def test_customer_database_name_sanitizes_generated_tenant_ids():
    from vexic.hosted_http import _customer_database_name

    name = _customer_database_name("tenant_abcd1234")

    assert name.startswith("vexic-tenant-abcd1234-")
    assert "_" not in name
    assert name == name.lower()
    assert all(ch.isalnum() or ch == "-" for ch in name)
    assert len(name) <= 48


def test_factory_turso_branch_provisions_new_control_plane_tenant(monkeypatch, tmp_path):
    from vexic.hosted_control_plane_http import create_app as create_control_plane_app
    from vexic.hosted_http import _customer_database_name, create_service_from_env

    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "turso")
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("TURSO_ORG", "pyrodonkey")
    dsn = "libsql://fresh-tenant-pyrodonkey.aws-us-west-2.turso.io"
    provisioning = _FakeProvisioning(dsn)
    service = create_service_from_env(turso_provisioning=provisioning)
    client = TestClient(
        create_control_plane_app(service, control_plane_tokens=("console-secret",))
    )

    response = client.post(
        "/control/v1/clerk-orgs/org_new/projects",
        headers={"Authorization": "Bearer console-secret"},
        json={"name": "Solo"},
    )

    assert response.status_code == 201
    tenant_id = response.json()["project"]["tenantId"]
    assert "_" in tenant_id
    assert provisioning.create_calls == [_customer_database_name(tenant_id)]
    assert service.catalog.get_tenant(tenant_id).customer_target == dsn


def test_factory_turso_branch_can_provision_existing_local_tenants(monkeypatch, tmp_path):
    from vexic.hosted_http import _customer_database_name, create_service_from_env

    tenant_id = "tenant_existing"
    HostedTenantCatalog(tmp_path).provision_tenant(
        tenant_id,
        project_ids={"project-existing"},
    )
    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "turso")
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("TURSO_ORG", "pyrodonkey")
    monkeypatch.setenv("VEXIC_PROVISION_EXISTING_TURSO_TARGETS", "1")
    dsn = "libsql://existing-tenant-pyrodonkey.aws-us-west-2.turso.io"
    provisioning = _FakeProvisioning(dsn)

    service = create_service_from_env(turso_provisioning=provisioning)

    assert provisioning.create_calls == [_customer_database_name(tenant_id)]
    assert "_" not in provisioning.create_calls[0]
    assert service.catalog.get_tenant(tenant_id).customer_target == dsn


def test_factory_local_branch_has_no_resolver(monkeypatch, tmp_path):
    from vexic.hosted_http import create_service_from_env

    monkeypatch.delenv("VEXIC_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))

    service = create_service_from_env()

    assert service._customer_target_resolver is None


def test_override_wiring_is_removed():
    # The Task-7b dogfood override + single-tenant guard are superseded by the
    # resolver seam and must no longer exist on the service surface.
    assert not hasattr(HostedMemoryService, "_check_override_single_tenant")
    root = None  # not needed; only checking the constructor surface
    with pytest.raises(TypeError):
        HostedMemoryService(root, root, customer_memory_target_override=object())


# ---------------------------------------------------------------------------
# Task 16 (COA-273 P4, live): full per-tenant provision -> ingest/search
# round-trip -> destroy against the real Turso Platform API + a real per-tenant
# database. Gated on creds + the libsql extra; self-cleaning (the throwaway DB
# is destroyed in a `finally` and asserted gone). Never prints the platform
# token or a minted jwt.

_TURSO_ORG = os.environ.get("TURSO_ORG")
_TURSO_GROUP = os.environ.get("TURSO_GROUP")
_TURSO_PLATFORM_TOKEN = os.environ.get("TURSO_PLATFORM_API_TOKEN")
_HAS_PROVISIONING = bool(
    _TURSO_ORG
    and _TURSO_GROUP
    and _TURSO_PLATFORM_TOKEN
    and importlib.util.find_spec("libsql")
)


def _scope(*, tenant_id: str, capabilities: set[MemoryCapability]) -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant_id,
        project_id="project-a",
        session_id="default",
        agent_id=None,
        principal=Principal(principal_id="caller", principal_type=PrincipalType.HUMAN),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities=capabilities,
    )


@pytest.mark.turso
@pytest.mark.skipif(not _HAS_PROVISIONING, reason="Turso platform creds/libsql missing")
def test_per_tenant_provision_round_trip_then_destroy(tmp_path):
    """Live e2e (COA-273 Task 16): provision a throwaway per-tenant Turso DB,
    round-trip ingest -> search through the hosted HTTP API via the real
    token-minting resolver, then DESTROY the DB and assert it is gone.

    The control-plane (tenant catalog + API-key store) is LOCAL, rooted at a
    tmp dir; only the customer-memory target routes to the throwaway Turso DB,
    resolved per-tenant by `make_customer_target_resolver` (which mints a
    fresh, DB-scoped jwt). Self-cleaning: the DB is destroyed in `finally` and
    the platform's list-databases no longer includes it. The platform token
    and any minted jwt are never printed.
    """
    from adapters.turso_adapter import TursoProvisioningPort
    from vexic.hosted_http import create_app

    db_name = f"vexic-itest-{uuid.uuid4().hex[:8]}"
    port = TursoProvisioningPort.from_env(os.environ)
    marker = f"cedar-{uuid.uuid4().hex[:8]}"
    session_id = f"prov-live-{uuid.uuid4().hex[:12]}"

    dsn = port.create_database(db_name)
    try:
        # LOCAL control-plane; the tenant carries the throwaway DSN as its
        # per-tenant customer_target so the resolver mints a token for it.
        root = Path(tmp_path)
        catalog = HostedTenantCatalog(root)
        keys = HostedApiKeyStore(root)
        catalog.provision_tenant(
            "tenant-live",
            project_ids={"project-live"},
            customer_target=dsn,
        )
        raw_key = keys.create_key(
            tenant_id="tenant-live",
            principal_id="agent-live",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-live"},
        ).raw_key

        cache = TenantTokenCache(port)
        resolver = make_customer_target_resolver(cache, org=_TURSO_ORG)
        service = HostedMemoryService(
            catalog,
            keys,
            telemetry=catalog,
            customer_target_resolver=resolver,
        )
        client = TestClient(create_app(service))
        headers = {
            "Authorization": f"Bearer {raw_key}",
            "X-Vexic-Project-Id": "project-live",
            "X-Vexic-Session-Id": session_id,
        }
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content=f"live provision round trip {marker}")])
        ).decode()

        ingest_response = client.post(
            "/v1/ingest_source_transcript",
            headers=headers,
            json={
                "messages": [
                    {
                        "source_host": "turso-prov-test",
                        "source_session_id": session_id,
                        "source_message_id": "msg-1",
                        "message_json": message_json,
                    }
                ],
                "redaction": {"forbidden_values": []},
            },
        )
        search_response = client.post(
            "/v1/search_transcript",
            headers=headers,
            json={"query": marker, "limit": 5},
        )

        assert ingest_response.status_code == 200, ingest_response.text
        assert ingest_response.json()["items"][0]["status"] == "inserted"
        assert search_response.status_code == 200, search_response.text
        hits = search_response.json()["hits"]
        assert len(hits) == 1
        assert marker in hits[0]["body"]
    finally:
        port.destroy_database(db_name)

    # The throwaway DB must be gone from the platform after destroy.
    status, payload = port._call("GET", "/databases")
    remaining = {db.get("Name") for db in payload.get("databases", [])}
    assert db_name not in remaining
