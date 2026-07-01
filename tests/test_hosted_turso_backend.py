import tempfile
from pathlib import Path

import pytest

from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import HostedMemoryService, resolve_storage_backend  # new helper
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.service import LocalMemoryService
from vexic.storage import StorageTarget


def test_default_is_local():
    assert resolve_storage_backend({}) == "local"


def test_turso_flag_selected():
    assert resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "turso"}) == "turso"


def test_unknown_flag_rejected():
    with pytest.raises(ValueError):
        resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "postgres"})


class _FakeTursoTargetResolver:
    """Test double for the injected resolver seam; never reads real env/secrets."""

    def __init__(self, target: StorageTarget) -> None:
        self._target = target
        self.control_plane_calls = 0
        self.customer_memory_calls = 0

    def control_plane_target(self, env):
        self.control_plane_calls += 1
        return StorageTarget("libsql://fake-control-plane", auth_token="unused")

    def customer_memory_target(self, env):
        self.customer_memory_calls += 1
        return self._target


def _hosted_root(tmp_path: Path) -> Path:
    return tmp_path


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


def test_factory_wires_customer_memory_override_from_fake_resolver(monkeypatch, tmp_path):
    from vexic.hosted_http import create_service_from_env

    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "turso")
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))
    fake_target = StorageTarget("libsql://fake", auth_token="x")
    resolver = _FakeTursoTargetResolver(fake_target)

    service = create_service_from_env(turso_target_resolver=resolver)

    assert isinstance(service, HostedMemoryService)
    assert service._customer_memory_target_override == fake_target
    assert resolver.customer_memory_calls == 1


def test_local_service_uses_override_for_db_path(tmp_path):
    root = _hosted_root(tmp_path)
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    tenant = catalog.get_tenant("tenant-a")
    fake_target = StorageTarget("libsql://fake", auth_token="x")

    service = HostedMemoryService(
        catalog,
        keys,
        customer_memory_target_override=fake_target,
    )
    local_service = service._local_service(tenant)

    assert isinstance(local_service, LocalMemoryService)
    assert local_service.db_path == fake_target


def test_override_guard_raises_on_second_distinct_tenant(tmp_path):
    root = _hosted_root(tmp_path)
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    catalog.provision_tenant("tenant-b", project_ids={"project-b"})
    tenant_a = catalog.get_tenant("tenant-a")
    tenant_b = catalog.get_tenant("tenant-b")
    fake_target = StorageTarget("libsql://fake", auth_token="x")

    service = HostedMemoryService(
        catalog,
        keys,
        customer_memory_target_override=fake_target,
    )

    service._local_service(tenant_a)
    with pytest.raises((PermissionError, RuntimeError)):
        service._local_service(tenant_b)
