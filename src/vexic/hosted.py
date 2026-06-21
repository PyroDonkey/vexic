from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from vexic.contract import (
    AppendTranscriptRequest,
    AppendTranscriptResult,
    DeleteScopeRequest,
    DeleteScopeResult,
    ExpandHistoryRequest,
    ExpandHistoryResult,
    ExportScopeRequest,
    ExportScopeResult,
    IngestSourceTranscriptRequest,
    IngestSourceTranscriptResult,
    RunDreamPhaseRequest,
    RunDreamPhaseResult,
    MemoryCapability,
    MemoryRequest,
    Principal,
    PrincipalType,
    RecordRetrievalEventRequest,
    RecordRetrievalEventResult,
    RebuildRequest,
    RebuildResult,
    ReplayScopeRequest,
    ReplayScopeResult,
    RetireFactRequest,
    RetireFactResult,
    SearchLongTermRequest,
    SearchLongTermResult,
    SearchTranscriptRequest,
    SearchTranscriptResult,
    TrustBoundary,
)
from vexic.ports import missing_host_port
from vexic.service import LocalMemoryService


_RequestT = TypeVar("_RequestT", bound=MemoryRequest)


@dataclass(frozen=True)
class HostedTenant:
    tenant_id: str
    db_path: Path
    project_ids: frozenset[str]


@dataclass(frozen=True)
class ProvisionedApiKey:
    key_id: str
    raw_key: str


@dataclass(frozen=True)
class HostedApiKey:
    key_id: str
    key_hash: str
    tenant_id: str
    principal_id: str
    capabilities: frozenset[MemoryCapability]
    project_ids: frozenset[str]
    active: bool = True


@dataclass(frozen=True)
class HostedAuthContext:
    key_id: str
    tenant_id: str
    principal: Principal
    capabilities: frozenset[MemoryCapability]
    project_ids: frozenset[str]


@dataclass(frozen=True)
class HostedAuditEvent:
    operation: str
    tenant_id: str | None
    principal_id: str | None
    status: str
    recorded_at: str
    error_type: str | None = None


@dataclass(frozen=True)
class HostedUsageEvent:
    kind: str
    operation: str
    tenant_id: str | None
    principal_id: str | None
    status: str
    recorded_at: str
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_micros: int = 0
    error_type: str | None = None


@dataclass(frozen=True)
class HostedJobEvent:
    job_id: str
    operation: str
    tenant_id: str
    principal_id: str
    status: str
    recorded_at: str
    phase: str | None = None
    error_type: str | None = None


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
        self._keys: dict[str, HostedApiKey] = {}

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
        self._keys[key_id] = HostedApiKey(
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


class HostedMemoryService:
    def __init__(
        self,
        catalog: HostedTenantCatalog,
        api_keys: HostedApiKeyStore,
    ) -> None:
        self.catalog = catalog
        self.api_keys = api_keys
        self.audit_events: list[HostedAuditEvent] = []
        self.usage_events: list[HostedUsageEvent] = []

    async def append_transcript(
        self,
        api_key: str,
        request: AppendTranscriptRequest,
    ) -> AppendTranscriptResult:
        return await self._call(
            "append_transcript",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).append_transcript(bound),
        )

    async def ingest_source_transcript(
        self,
        api_key: str,
        request: IngestSourceTranscriptRequest,
    ) -> IngestSourceTranscriptResult:
        return await self._call(
            "ingest_source_transcript",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).ingest_source_transcript(bound),
        )

    async def search_transcript(
        self,
        api_key: str,
        request: SearchTranscriptRequest,
    ) -> SearchTranscriptResult:
        return await self._call(
            "search_transcript",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).search_transcript(bound),
        )

    async def expand_history(
        self,
        api_key: str,
        request: ExpandHistoryRequest,
    ) -> ExpandHistoryResult:
        return await self._call(
            "expand_history",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).expand_history(bound),
        )

    async def search_long_term(
        self,
        api_key: str,
        request: SearchLongTermRequest,
    ) -> SearchLongTermResult:
        return await self._call(
            "search_long_term",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).search_long_term(bound),
        )

    async def record_retrieval_event(
        self,
        api_key: str,
        request: RecordRetrievalEventRequest,
    ) -> RecordRetrievalEventResult:
        return await self._call(
            "record_retrieval_event",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).record_retrieval_event(bound),
        )

    async def retire_fact(
        self,
        api_key: str,
        request: RetireFactRequest,
    ) -> RetireFactResult:
        return await self._call(
            "retire_fact",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).retire_fact(bound),
        )

    async def run_dream_phase(
        self,
        api_key: str,
        request: RunDreamPhaseRequest,
    ) -> RunDreamPhaseResult:
        return await self._call(
            "run_dream_phase",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).run_dream_phase(bound),
        )

    async def export_scope(
        self,
        api_key: str,
        request: ExportScopeRequest,
    ) -> ExportScopeResult:
        return await self._call(
            "export_scope",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).export_scope(bound),
        )

    async def replay_scope(
        self,
        api_key: str,
        request: ReplayScopeRequest,
    ) -> ReplayScopeResult:
        return await self._call(
            "replay_scope",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).replay_scope(bound),
        )

    async def rebuild(
        self,
        api_key: str,
        request: RebuildRequest,
    ) -> RebuildResult:
        return await self._call(
            "rebuild",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).rebuild(bound),
        )

    async def delete_scope(
        self,
        api_key: str,
        request: DeleteScopeRequest,
    ) -> DeleteScopeResult:
        return await self._call(
            "delete_scope",
            api_key,
            request,
            request.required_capability,
            lambda bound: self._local_service(bound).delete_scope(bound),
        )

    async def _call(
        self,
        operation: str,
        api_key: str,
        request: _RequestT,
        capability: MemoryCapability,
        delegate: Callable[[_RequestT], Awaitable[object]],
    ) -> object:
        bound: _RequestT | None = None
        try:
            bound = self._bind_request(api_key, request, capability)
            result = await delegate(bound)
        except Exception as exc:
            self._record_request(
                operation,
                bound,
                status="error",
                error_type=type(exc).__name__,
            )
            raise
        self._record_request(operation, bound, status="ok")
        return result

    def _bind_request(
        self,
        api_key: str,
        request: _RequestT,
        capability: MemoryCapability,
    ) -> _RequestT:
        auth = self.api_keys.authenticate(api_key)
        tenant = self.catalog.get_tenant(auth.tenant_id)
        if request.scope.tenant_id != auth.tenant_id:
            raise PermissionError("Memory scope tenant_id does not match API key.")
        if request.scope.project_id is not None and request.scope.project_id not in tenant.project_ids:
            raise PermissionError("Memory scope project_id is not provisioned for tenant.")
        if request.scope.project_id not in auth.project_ids:
            raise PermissionError("Memory scope project_id is not allowed for API key.")
        effective_capabilities = request.scope.capabilities & auth.capabilities
        if capability not in effective_capabilities:
            raise PermissionError(f"Memory capability required: {capability.value}")
        scope = request.scope.model_copy(
            update={
                "principal": auth.principal,
                "trust_boundary": TrustBoundary.NETWORKED,
                "capabilities": effective_capabilities,
            }
        )
        return request.model_copy(update={"scope": scope})

    def _local_service(self, request: MemoryRequest) -> LocalMemoryService:
        tenant = self.catalog.get_tenant(request.scope.tenant_id)
        return LocalMemoryService(db_path=str(tenant.db_path), tenant_id=tenant.tenant_id)

    def _record_request(
        self,
        operation: str,
        request: MemoryRequest | None,
        *,
        status: str,
        error_type: str | None = None,
    ) -> None:
        tenant_id = request.scope.tenant_id if request is not None else None
        principal_id = request.scope.principal.principal_id if request is not None else None
        recorded_at = _now()
        self.audit_events.append(
            HostedAuditEvent(
                operation=operation,
                tenant_id=tenant_id,
                principal_id=principal_id,
                status=status,
                recorded_at=recorded_at,
                error_type=error_type,
            )
        )
        self.usage_events.append(
            HostedUsageEvent(
                kind="request",
                operation=operation,
                tenant_id=tenant_id,
                principal_id=principal_id,
                status=status,
                recorded_at=recorded_at,
                error_type=error_type,
            )
        )

    def record_job_usage(
        self,
        *,
        operation: str,
        tenant_id: str,
        principal_id: str,
        status: str,
        error_type: str | None = None,
    ) -> None:
        self.usage_events.append(
            HostedUsageEvent(
                kind="job",
                operation=operation,
                tenant_id=tenant_id,
                principal_id=principal_id,
                status=status,
                recorded_at=_now(),
                error_type=error_type,
            )
        )


class HostedBackgroundJobRunner:
    def __init__(self, service: HostedMemoryService) -> None:
        self.service = service
        self.job_events: list[HostedJobEvent] = []

    async def run_dream_phase(
        self,
        api_key: str,
        request: RunDreamPhaseRequest,
    ) -> RunDreamPhaseResult:
        auth = self.service.api_keys.authenticate(api_key)
        job_id = secrets.token_hex(8)
        self._record_job(
            job_id,
            request,
            auth,
            status="running",
        )
        try:
            result = await self.service.run_dream_phase(api_key, request)
        except NotImplementedError as exc:
            error = missing_host_port("Dream phase")
            self._record_job(
                job_id,
                request,
                auth,
                status="error",
                error_type=type(error).__name__,
            )
            self.service.record_job_usage(
                operation="run_dream_phase",
                tenant_id=auth.tenant_id,
                principal_id=auth.principal.principal_id,
                status="error",
                error_type=type(error).__name__,
            )
            raise error from exc
        except Exception as exc:
            self._record_job(
                job_id,
                request,
                auth,
                status="error",
                error_type=type(exc).__name__,
            )
            self.service.record_job_usage(
                operation="run_dream_phase",
                tenant_id=auth.tenant_id,
                principal_id=auth.principal.principal_id,
                status="error",
                error_type=type(exc).__name__,
            )
            raise
        self._record_job(job_id, request, auth, status="ok")
        self.service.record_job_usage(
            operation="run_dream_phase",
            tenant_id=auth.tenant_id,
            principal_id=auth.principal.principal_id,
            status="ok",
        )
        return result

    def _record_job(
        self,
        job_id: str,
        request: RunDreamPhaseRequest,
        auth: HostedAuthContext,
        *,
        status: str,
        error_type: str | None = None,
    ) -> None:
        self.job_events.append(
            HostedJobEvent(
                job_id=job_id,
                operation="run_dream_phase",
                tenant_id=auth.tenant_id,
                principal_id=auth.principal.principal_id,
                status=status,
                phase=request.phase.value,
                recorded_at=_now(),
                error_type=error_type,
            )
        )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
