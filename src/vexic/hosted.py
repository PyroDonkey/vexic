from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol, TypeVar

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


class HostedTenantDirectory(Protocol):
    def get_tenant(self, tenant_id: str) -> HostedTenant: ...


class HostedApiKeyAuthenticator(Protocol):
    def authenticate(self, raw_key: str) -> HostedAuthContext: ...


class HostedTelemetrySink(Protocol):
    def record_audit_event(self, event: HostedAuditEvent) -> None: ...

    def record_usage_event(self, event: HostedUsageEvent) -> None: ...


class HostedMemoryService:
    def __init__(
        self,
        catalog: HostedTenantDirectory,
        api_keys: HostedApiKeyAuthenticator,
        telemetry: HostedTelemetrySink | None = None,
    ) -> None:
        self.catalog = catalog
        self.api_keys = api_keys
        self.telemetry = telemetry

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
            lambda bound: self._run_dream_phase(bound),
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
        project_id = request.scope.project_id
        if project_id is None:
            if auth.project_ids:
                raise PermissionError("Memory scope project_id is required for project-scoped API key.")
        else:
            if project_id not in tenant.project_ids:
                raise PermissionError("Memory scope project_id is not provisioned for tenant.")
            if project_id not in auth.project_ids:
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

    async def _run_dream_phase(self, request: RunDreamPhaseRequest) -> RunDreamPhaseResult:
        try:
            return await self._local_service(request).run_dream_phase(request)
        except NotImplementedError as exc:
            raise missing_host_port("Dream phase") from exc

    def _record_request(
        self,
        operation: str,
        request: MemoryRequest | None,
        *,
        status: str,
        error_type: str | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        tenant_id = request.scope.tenant_id if request is not None else None
        principal_id = request.scope.principal.principal_id if request is not None else None
        recorded_at = _now()
        self.telemetry.record_audit_event(
            HostedAuditEvent(
                operation=operation,
                tenant_id=tenant_id,
                principal_id=principal_id,
                status=status,
                recorded_at=recorded_at,
                error_type=error_type,
            )
        )
        self.telemetry.record_usage_event(
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
        if self.telemetry is None:
            return
        self.telemetry.record_usage_event(
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
