from __future__ import annotations

import secrets
import threading
import time
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
    agent_ids: frozenset[str | None] = frozenset()


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


@dataclass(frozen=True)
class HostedRateLimitRule:
    limit: int = 120
    window_seconds: int = 60

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("rate limit must be at least 1.")
        if self.window_seconds < 1:
            raise ValueError("rate limit window_seconds must be at least 1.")


class HostedRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(
            f"Hosted rate limit exceeded. Retry after {self.retry_after_seconds} seconds."
        )


@dataclass
class _RateBucket:
    count: int
    expires_at: float


_EXPENSIVE_OPERATION_LIMITS = {
    "expand_history": HostedRateLimitRule(limit=30, window_seconds=60),
    "run_dream_phase": HostedRateLimitRule(limit=6, window_seconds=3600),
    "export_scope": HostedRateLimitRule(limit=6, window_seconds=3600),
    "replay_scope": HostedRateLimitRule(limit=6, window_seconds=3600),
    "rebuild": HostedRateLimitRule(limit=6, window_seconds=3600),
    "delete_scope": HostedRateLimitRule(limit=6, window_seconds=3600),
}


class HostedInMemoryRateLimiter:
    def __init__(
        self,
        *,
        default_rule: HostedRateLimitRule = HostedRateLimitRule(),
        operation_rules: dict[str, HostedRateLimitRule] | None = None,
        max_buckets: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_buckets < 1:
            raise ValueError("max_buckets must be at least 1.")
        self.default_rule = default_rule
        self.operation_rules = dict(_EXPENSIVE_OPERATION_LIMITS)
        if operation_rules is not None:
            self.operation_rules.update(operation_rules)
        self.max_buckets = max_buckets
        self.clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str, str, str], _RateBucket] = {}
        self._next_prune_at = float("inf")

    def check(self, operation: str, auth: HostedAuthContext) -> None:
        rule = self.operation_rules.get(operation, self.default_rule)
        now = self.clock()
        key = (
            auth.tenant_id,
            auth.principal.principal_id,
            auth.key_id,
            operation,
        )
        with self._lock:
            if now >= self._next_prune_at:
                self._prune(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_buckets:
                    raise HostedRateLimitExceeded(self._shortest_retry_after(now))
                expires_at = now + rule.window_seconds
                self._buckets[key] = _RateBucket(
                    count=1,
                    expires_at=expires_at,
                )
                self._next_prune_at = min(self._next_prune_at, expires_at)
                return
            if bucket.expires_at <= now:
                expires_at = now + rule.window_seconds
                bucket.count = 1
                bucket.expires_at = expires_at
                self._next_prune_at = min(self._next_prune_at, expires_at)
                return
            if bucket.count >= rule.limit:
                raise HostedRateLimitExceeded(int(bucket.expires_at - now) + 1)
            bucket.count += 1

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, bucket in self._buckets.items()
            if bucket.expires_at <= now
        ]
        # ponytail: O(n) prune is fine for staging; production needs a durable limiter.
        for key in expired:
            del self._buckets[key]
        self._next_prune_at = min(
            (bucket.expires_at for bucket in self._buckets.values()),
            default=float("inf"),
        )

    def _shortest_retry_after(self, now: float) -> int:
        return min(
            (
                max(1, int(bucket.expires_at - now) + 1)
                for bucket in self._buckets.values()
            ),
            default=1,
        )


class HostedTenantDirectory(Protocol):
    def get_tenant(self, tenant_id: str) -> HostedTenant: ...


class HostedApiKeyAuthenticator(Protocol):
    def authenticate(self, raw_key: str) -> HostedAuthContext: ...


class HostedTelemetrySink(Protocol):
    def record_audit_event(self, event: HostedAuditEvent) -> None: ...

    def record_usage_event(self, event: HostedUsageEvent) -> None: ...

    def record_job_event(self, event: HostedJobEvent) -> None: ...


class HostedMemoryService:
    def __init__(
        self,
        catalog: HostedTenantDirectory,
        api_keys: HostedApiKeyAuthenticator,
        telemetry: HostedTelemetrySink | None = None,
        rate_limiter: HostedInMemoryRateLimiter | None = None,
    ) -> None:
        self.catalog = catalog
        self.api_keys = api_keys
        self.telemetry = telemetry
        self.rate_limiter = rate_limiter or HostedInMemoryRateLimiter()

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
            lambda bound, tenant: self._local_service(tenant).append_transcript(bound),
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
            lambda bound, tenant: self._local_service(tenant).ingest_source_transcript(bound),
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
            lambda bound, tenant: self._local_service(tenant).search_transcript(bound),
        )

    async def expand_history(
        self,
        api_key: str,
        request: ExpandHistoryRequest,
        *,
        max_rows: int | None = None,
    ) -> ExpandHistoryResult:
        return await self._call(
            "expand_history",
            api_key,
            request,
            request.required_capability,
            lambda bound, tenant: self._local_service(tenant).expand_history(
                bound,
                max_rows=max_rows,
            ),
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
            lambda bound, tenant: self._local_service(tenant).search_long_term(bound),
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
            lambda bound, tenant: self._local_service(tenant).record_retrieval_event(bound),
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
            lambda bound, tenant: self._local_service(tenant).retire_fact(bound),
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
            lambda bound, tenant: self._run_dream_phase(bound, tenant),
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
            lambda bound, tenant: self._local_service(tenant).export_scope(bound),
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
            lambda bound, tenant: self._local_service(tenant).replay_scope(bound),
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
            lambda bound, tenant: self._local_service(tenant).rebuild(bound),
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
            lambda bound, tenant: self._local_service(tenant).delete_scope(bound),
        )

    async def _call(
        self,
        operation: str,
        api_key: str,
        request: _RequestT,
        capability: MemoryCapability,
        delegate: Callable[[_RequestT, HostedTenant], Awaitable[object]],
    ) -> object:
        auth: HostedAuthContext | None = None
        bound: _RequestT | None = None
        try:
            auth = self.api_keys.authenticate(api_key)
            bound, tenant = self._bind_request(auth, request, capability)
            self.rate_limiter.check(operation, auth)
            result = await delegate(bound, tenant)
        except HostedRateLimitExceeded as exc:
            self._record_request(
                operation,
                bound,
                status="rate_limited",
                error_type=type(exc).__name__,
                auth=auth,
            )
            raise
        except Exception as exc:
            self._record_request(
                operation,
                bound,
                status="error",
                error_type=type(exc).__name__,
                auth=auth,
            )
            raise
        self._record_request(operation, bound, status="ok")
        return result

    def _bind_request(
        self,
        auth: HostedAuthContext,
        request: _RequestT,
        capability: MemoryCapability,
    ) -> tuple[_RequestT, HostedTenant]:
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
        if auth.agent_ids and request.scope.agent_id not in auth.agent_ids:
            raise PermissionError("Memory scope agent_id is not allowed for API key.")
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
        return request.model_copy(update={"scope": scope}), tenant

    def _local_service(self, tenant: HostedTenant) -> LocalMemoryService:
        return LocalMemoryService(db_path=str(tenant.db_path), tenant_id=tenant.tenant_id)

    async def _run_dream_phase(
        self,
        request: RunDreamPhaseRequest,
        tenant: HostedTenant,
    ) -> RunDreamPhaseResult:
        try:
            return await self._local_service(tenant).run_dream_phase(request)
        except NotImplementedError as exc:
            raise missing_host_port("Dream phase") from exc

    def _record_request(
        self,
        operation: str,
        request: MemoryRequest | None,
        *,
        status: str,
        error_type: str | None = None,
        auth: HostedAuthContext | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        if request is not None:
            tenant_id = request.scope.tenant_id
            principal_id = request.scope.principal.principal_id
        elif auth is not None:
            tenant_id = auth.tenant_id
            principal_id = auth.principal.principal_id
        else:
            tenant_id = None
            principal_id = None
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
        telemetry = service.telemetry
        if telemetry is None:
            raise ValueError("HostedBackgroundJobRunner requires durable telemetry.")
        self.service = service
        self.telemetry = telemetry
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
        event = HostedJobEvent(
            job_id=job_id,
            operation="run_dream_phase",
            tenant_id=auth.tenant_id,
            principal_id=auth.principal.principal_id,
            status=status,
            phase=request.phase.value,
            recorded_at=_now(),
            error_type=error_type,
        )
        self.job_events.append(event)
        try:
            self.telemetry.record_job_event(event)
        except Exception:
            pass


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
