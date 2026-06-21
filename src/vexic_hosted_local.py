from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from pathlib import Path

from vexic.contract import MemoryCapability, Principal, PrincipalType
from vexic.hosted import HostedAuthContext, HostedTenant
from vexic.service import LocalMemoryService


@dataclass(frozen=True)
class ProvisionedApiKey:
    key_id: str
    raw_key: str


@dataclass(frozen=True)
class _HostedApiKey:
    key_id: str
    key_hash: str
    tenant_id: str
    principal_id: str
    capabilities: frozenset[MemoryCapability]
    project_ids: frozenset[str]
    active: bool = True


class HostedTenantCatalog:
    def __init__(self, root_path: str | Path) -> None:
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)
        self._tenants: dict[str, HostedTenant] = {}

    def provision_tenant(
        self,
        tenant_id: str,
        *,
        project_ids: set[str] | frozenset[str] = frozenset(),
    ) -> HostedTenant:
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be blank.")
        if tenant_id in self._tenants:
            tenant = self._tenants[tenant_id]
            updated = replace(
                tenant,
                project_ids=tenant.project_ids | frozenset(project_ids),
            )
            self._tenants[tenant_id] = updated
            return updated
        digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        tenant = HostedTenant(
            tenant_id=tenant_id,
            db_path=self.root_path / f"customer-{digest}.db",
            project_ids=frozenset(project_ids),
        )
        LocalMemoryService(db_path=str(tenant.db_path), tenant_id=tenant_id).init_schema()
        self._tenants[tenant_id] = tenant
        return tenant

    def provision_project(self, tenant_id: str, project_id: str) -> HostedTenant:
        if not project_id.strip():
            raise ValueError("project_id must not be blank.")
        tenant = self.get_tenant(tenant_id)
        updated = HostedTenant(
            tenant_id=tenant.tenant_id,
            db_path=tenant.db_path,
            project_ids=tenant.project_ids | {project_id},
        )
        self._tenants[tenant_id] = updated
        return updated

    def get_tenant(self, tenant_id: str) -> HostedTenant:
        try:
            return self._tenants[tenant_id]
        except KeyError as exc:
            raise PermissionError("Unknown hosted tenant.") from exc


class HostedApiKeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, _HostedApiKey] = {}

    def create_key(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        capabilities: set[MemoryCapability] | frozenset[MemoryCapability],
        project_ids: set[str] | frozenset[str] = frozenset(),
    ) -> ProvisionedApiKey:
        raw_key = f"vx_{secrets.token_urlsafe(32)}"
        key_id = secrets.token_hex(8)
        self._keys[key_id] = _HostedApiKey(
            key_id=key_id,
            key_hash=self._hash(raw_key),
            tenant_id=tenant_id,
            principal_id=principal_id,
            capabilities=frozenset(capabilities),
            project_ids=frozenset(project_ids),
        )
        return ProvisionedApiKey(key_id=key_id, raw_key=raw_key)

    def authenticate(self, raw_key: str) -> HostedAuthContext:
        key_hash = self._hash(raw_key)
        # ponytail: linear scan is fine for MVP; index by hash when key counts matter.
        for stored in self._keys.values():
            if stored.active and hmac.compare_digest(stored.key_hash, key_hash):
                return HostedAuthContext(
                    key_id=stored.key_id,
                    tenant_id=stored.tenant_id,
                    principal=Principal(
                        principal_id=stored.principal_id,
                        principal_type=PrincipalType.AGENT,
                    ),
                    capabilities=stored.capabilities,
                    project_ids=stored.project_ids,
                )
        raise PermissionError("Invalid hosted API key.")

    def revoke_key(self, key_id: str) -> None:
        try:
            stored = self._keys[key_id]
        except KeyError as exc:
            raise PermissionError("Unknown hosted API key.") from exc
        self._keys[key_id] = replace(stored, active=False)

    @staticmethod
    def _hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
