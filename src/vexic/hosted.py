from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Awaitable, Callable, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict
from vexic.contract import (
    AppendTranscriptRequest,
    AppendTranscriptResult,
    DeleteScopeRequest,
    DeleteScopeResult,
    DreamPhase,
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
    MemoryResult,
    MemoryScope,
    Principal,
    PrincipalType,
    RecordRetrievalEventRequest,
    RecordRetrievalEventResult,
    RebuildRequest,
    RebuildResult,
    RedactionContext,
    ReplayScopeRequest,
    ReplayScopeResult,
    RetireFactRequest,
    RetireFactResult,
    SearchLongTermRequest,
    SearchLongTermResult,
    SearchTranscriptRequest,
    SearchTranscriptResult,
    SourceTranscriptMessage,
    TrustBoundary,
)
from vexic.ports import DreamPhasePorts, missing_host_port
from vexic.service import (
    LocalMemoryService,
    _run_dream_phase_with_usage as _run_local_dream_phase_with_usage,
)
from vexic.storage.connection import StorageTarget
from vexic.usage import UsageSummary


_RequestT = TypeVar("_RequestT", bound=MemoryRequest)
_ResultT = TypeVar("_ResultT", bound=MemoryResult)

HOSTED_WRITE_MAX_MESSAGES = 100
HOSTED_WRITE_MAX_CHARS = 250_000


class HostedAppendTranscriptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages_json: list[str]
    redaction: RedactionContext


class HostedIngestSourceTranscriptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[SourceTranscriptMessage]
    redaction: RedactionContext


def register_hosted_write_routes(
    app: object,
    service: HostedMemoryService,
    *,
    api_key_from_request: Callable[[object], str | None],
    handle_payload: Callable[
        [str, _RequestT, Callable[[str, _RequestT], Awaitable[_ResultT]]],
        Awaitable[object],
    ],
    error_response: Callable[[int, str, str], object],
) -> None:
    from fastapi import Request

    async def append_transcript(
        request,
        payload,
    ) -> object:
        return await _handle_hosted_write(
            "append_transcript",
            request,
            service,
            lambda scope: AppendTranscriptRequest(
                scope=scope,
                messages_json=payload.messages_json,
                redaction=payload.redaction,
            ),
            service.append_transcript,
            api_key_from_request=api_key_from_request,
            handle_payload=handle_payload,
            error_response=error_response,
        )

    async def ingest_source_transcript(
        request,
        payload,
    ) -> object:
        return await _handle_hosted_write(
            "ingest_source_transcript",
            request,
            service,
            lambda scope: IngestSourceTranscriptRequest(
                scope=scope,
                messages=payload.messages,
                redaction=payload.redaction,
            ),
            service.ingest_source_transcript,
            api_key_from_request=api_key_from_request,
            handle_payload=handle_payload,
            error_response=error_response,
        )

    append_transcript.__annotations__ = {
        "request": Request,
        "payload": HostedAppendTranscriptBody,
        "return": object,
    }
    ingest_source_transcript.__annotations__ = {
        "request": Request,
        "payload": HostedIngestSourceTranscriptBody,
        "return": object,
    }
    app.post("/v1/append_transcript")(append_transcript)
    app.post("/v1/ingest_source_transcript")(ingest_source_transcript)


def _hosted_write_cap_error(
    payload: MemoryRequest,
    error_response: Callable[[int, str, str], object],
) -> object | None:
    if isinstance(payload, AppendTranscriptRequest):
        if len(payload.messages_json) > HOSTED_WRITE_MAX_MESSAGES:
            return error_response(
                400,
                "request_too_large",
                "append_transcript message count is capped.",
            )
        if sum(len(message) for message in payload.messages_json) > HOSTED_WRITE_MAX_CHARS:
            return error_response(
                400,
                "request_too_large",
                "append_transcript payload is capped.",
            )
    if isinstance(payload, IngestSourceTranscriptRequest):
        if len(payload.messages) > HOSTED_WRITE_MAX_MESSAGES:
            return error_response(
                400,
                "request_too_large",
                "ingest_source_transcript message count is capped.",
            )
        if sum(len(message.message_json) for message in payload.messages) > HOSTED_WRITE_MAX_CHARS:
            return error_response(
                400,
                "request_too_large",
                "ingest_source_transcript payload is capped.",
            )
    return None


async def _handle_hosted_write(
    operation: str,
    request: object,
    service: HostedMemoryService,
    build: Callable[[MemoryScope], _RequestT],
    call: Callable[[str, _RequestT], Awaitable[_ResultT]],
    *,
    api_key_from_request: Callable[[object], str | None],
    handle_payload: Callable[
        [str, _RequestT, Callable[[str, _RequestT], Awaitable[_ResultT]]],
        Awaitable[object],
    ],
    error_response: Callable[[int, str, str], object],
) -> object:
    api_key = api_key_from_request(request)
    if api_key is None:
        return error_response(401, "unauthorized", "Missing hosted API key.")
    auth: HostedAuthContext | None = None
    try:
        auth = service.api_keys.authenticate(api_key)
        scope = _write_scope_from_headers(request, auth)
        payload = build(scope)
    except PermissionError as exc:
        service._record_request(
            operation,
            None,
            status="error",
            error_type=type(exc).__name__,
            auth=auth,
        )
        if str(exc) == "Invalid hosted API key.":
            return error_response(401, "unauthorized", "Invalid hosted API key.")
        return error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        service._record_request(
            operation,
            None,
            status="error",
            error_type=type(exc).__name__,
            auth=auth,
        )
        return error_response(400, "invalid_request", str(exc))
    except Exception as exc:
        service._record_request(
            operation,
            None,
            status="error",
            error_type=type(exc).__name__,
            auth=auth,
        )
        return error_response(500, "internal_error", "Hosted memory request failed.")
    cap_error = _hosted_write_cap_error(payload, error_response)
    if cap_error is not None:
        return cap_error
    return await handle_payload(api_key, payload, call)


def _write_scope_from_headers(request: object, auth: HostedAuthContext) -> MemoryScope:
    headers = request.headers
    if headers.get("x-vexic-user-id") is not None:
        raise ValueError("X-Vexic-User-Id is not supported for hosted writes.")
    if headers.get("x-vexic-correlation-id") is not None:
        raise ValueError("X-Vexic-Correlation-Id is not supported for hosted writes.")
    project_id = headers.get("x-vexic-project-id")
    if project_id is None or not project_id.strip():
        raise ValueError("X-Vexic-Project-Id header is required.")
    project_id = project_id.strip()
    session_id = headers.get("x-vexic-session-id")
    if session_id is None or not session_id.strip():
        raise ValueError("X-Vexic-Session-Id header is required.")
    session_id = session_id.strip()
    agent_id = headers.get("x-vexic-agent-id")
    if agent_id is not None:
        agent_id = agent_id.strip() or None
    return MemoryScope(
        tenant_id=auth.tenant_id,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
        principal=auth.principal,
        trust_boundary=TrustBoundary.NETWORKED,
        capabilities={MemoryCapability.WRITE},
    )


@dataclass(frozen=True)
class HostedTenant:
    tenant_id: str
    db_path: str | StorageTarget
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
    project_id: str | None = None


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
        dream_phase_ports: DreamPhasePorts | None = None,
        *,
        customer_memory_target_override: StorageTarget | None = None,
    ) -> None:
        self.catalog = catalog
        self.api_keys = api_keys
        self.telemetry = telemetry
        self.rate_limiter = rate_limiter or HostedInMemoryRateLimiter()
        self.dream_phase_ports = dream_phase_ports
        # P2 dogfood customer-memory override; superseded/removed by Task 11
        # (catalog per-tenant target model). When set, ALL tenants routed
        # through this app-lifetime singleton share one Turso StorageTarget
        # for customer memory, while the control-plane (auth + tenant
        # catalog) stays local. This is single-tenant only -- see the
        # fail-fast guard in `_local_service` below.
        self._customer_memory_target_override = customer_memory_target_override
        self._override_served_tenant_id: str | None = None

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

    async def _run_dream_phase_job(
        self,
        api_key: str,
        request: RunDreamPhaseRequest,
    ) -> tuple[RunDreamPhaseResult, UsageSummary]:
        return await self._call(
            "run_dream_phase",
            api_key,
            request,
            request.required_capability,
            lambda bound, tenant: self._run_dream_phase_with_usage(bound, tenant),
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
        db_path: str | StorageTarget = tenant.db_path
        if self._customer_memory_target_override is not None:
            self._check_override_single_tenant(tenant.tenant_id)
            db_path = self._customer_memory_target_override
        return LocalMemoryService(
            db_path=db_path,
            tenant_id=tenant.tenant_id,
            embed=self.dream_phase_ports.embed if self.dream_phase_ports else None,
            dream_phase_ports=self.dream_phase_ports,
        )

    def _check_override_single_tenant(self, tenant_id: str) -> None:
        # P2 dogfood customer-memory override; superseded/removed by Task 11.
        # HostedMemoryService is an app-lifetime singleton, so this state
        # persists across requests -- fail fast rather than silently mixing
        # a second tenant's data into the single dogfood Turso DB.
        if self._override_served_tenant_id is None:
            self._override_served_tenant_id = tenant_id
            return
        if tenant_id != self._override_served_tenant_id:
            raise PermissionError(
                "customer_memory_target_override is dogfood single-tenant only; "
                "refusing to serve a second tenant"
            )

    async def _run_dream_phase(
        self,
        request: RunDreamPhaseRequest,
        tenant: HostedTenant,
    ) -> RunDreamPhaseResult:
        result, _usage = await self._run_dream_phase_with_usage(request, tenant)
        return result

    async def _run_dream_phase_with_usage(
        self,
        request: RunDreamPhaseRequest,
        tenant: HostedTenant,
    ) -> tuple[RunDreamPhaseResult, UsageSummary]:
        try:
            return await _run_local_dream_phase_with_usage(
                self._local_service(tenant),
                request,
            )
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
            project_id = request.scope.project_id
        elif auth is not None:
            tenant_id = auth.tenant_id
            principal_id = auth.principal.principal_id
            project_id = None
        else:
            tenant_id = None
            principal_id = None
            project_id = None
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
                project_id=project_id,
            )
        )

    def record_job_usage(
        self,
        *,
        operation: str,
        tenant_id: str,
        principal_id: str,
        status: str,
        usage: UsageSummary | None = None,
        error_type: str | None = None,
        project_id: str | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        counters = usage or UsageSummary()
        try:
            self.telemetry.record_usage_event(
                HostedUsageEvent(
                    kind="job",
                    operation=operation,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    status=status,
                    recorded_at=_now(),
                    model_requests=counters.model_requests,
                    input_tokens=counters.input_tokens,
                    output_tokens=counters.output_tokens,
                    total_tokens=counters.total_tokens,
                    estimated_cost_micros=counters.estimated_cost_micros,
                    error_type=error_type,
                    project_id=project_id,
                )
            )
        except Exception:
            pass


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
            result, usage = await self.service._run_dream_phase_job(api_key, request)
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
                project_id=request.scope.project_id,
            )
            raise
        self._record_job(job_id, request, auth, status="ok")
        self.service.record_job_usage(
            operation="run_dream_phase",
            tenant_id=auth.tenant_id,
            principal_id=auth.principal.principal_id,
            status="ok",
            usage=usage,
            project_id=request.scope.project_id,
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


def add_run_dream_phase_subcommand(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    run_phase = subcommands.add_parser("run-dream-phase")
    run_phase.add_argument("--root")
    run_phase.add_argument("--api-key-env", default="VEXIC_API_KEY")
    run_phase.add_argument("--adapter", required=True)
    run_phase.add_argument("--model-group", required=True)
    run_phase.add_argument("--tenant-id", required=True)
    run_phase.add_argument("--project-id")
    run_phase.add_argument("--session-id")
    run_phase.add_argument("--agent-id")
    run_phase.add_argument(
        "--phase",
        choices=[phase.value for phase in DreamPhase],
        required=True,
    )
    run_phase.add_argument("--forbidden-value", action="append", default=[])
    run_phase.add_argument("--secret-env", action="append", default=[])


def run_dream_phase_command(args: argparse.Namespace) -> int:
    from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog

    root = _hosted_root_arg(args.root)
    catalog = HostedTenantCatalog(root)
    service = HostedMemoryService(
        catalog,
        HostedApiKeyStore(root),
        telemetry=catalog,
        rate_limiter=HostedInMemoryRateLimiter(),
        dream_phase_ports=_dream_phase_ports(args),
    )
    # ponytail: staging CLI assumes no concurrent tenant writers; add event ids if shared.
    usage_event_offset = len(catalog.usage_events(args.tenant_id))
    runner = HostedBackgroundJobRunner(service)
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(
            runner.run_dream_phase(
                _api_key_from_env(args.api_key_env),
                RunDreamPhaseRequest(
                    scope=_dream_phase_scope(args),
                    phase=DreamPhase(args.phase),
                    redaction=RedactionContext(
                        forbidden_values=tuple(args.forbidden_value),
                    ),
                ),
            )
        )
    print(
        json.dumps(
            {
                "result": result.model_dump(mode="json"),
                "job_events": [_event_dict(event) for event in runner.job_events],
                "usage_events": [
                    _event_dict(event)
                    for event in catalog.usage_events(args.tenant_id)[usage_event_offset:]
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def resolve_storage_backend(env) -> str:
    """Resolve the non-secret ``VEXIC_STORAGE_BACKEND`` selection flag.

    Defaults to ``"local"`` (filesystem SQLite, unchanged behavior). ``"turso"``
    selects the hosted libSQL/Turso backend (ADR 0019); any other value is
    rejected. This helper never reads secrets -- only the flag itself -- so it
    is safe to keep in ``src/vexic``.
    """
    value = env.get("VEXIC_STORAGE_BACKEND", "local").strip().lower()
    if value not in {"local", "turso"}:
        raise ValueError(f"invalid VEXIC_STORAGE_BACKEND: {value!r}")
    return value


def _hosted_root_arg(value: str | None) -> Path:
    return Path(value or os.environ.get("VEXIC_HOSTED_ROOT", ".hosted-memory"))


def _api_key_from_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required in the environment.")
    return value.strip()


def _secret_env_values(names: list[str]) -> dict[str, str] | None:
    secrets_by_name: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name)
        if value is None:
            raise ValueError(f"{name} is required in the environment.")
        secrets_by_name[name] = value
    return secrets_by_name or None


def _load_dream_phase_adapter(path: Path) -> ModuleType:
    if not path.exists():
        raise missing_host_port("Dream phase adapter")
    module_name = f"vexic_hosted_adapter_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise missing_host_port("Dream phase adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise missing_host_port("Dream phase adapter") from exc
    for name in (
        "embed_texts",
        "build_extraction_agent",
        "build_rem_agent",
        "build_contradiction_agent",
    ):
        if not callable(getattr(module, name, None)):
            raise missing_host_port("Dream phase adapter")
    return module


def _dream_phase_ports(args: argparse.Namespace) -> DreamPhasePorts:
    adapter = _load_dream_phase_adapter(Path(args.adapter))
    return DreamPhasePorts(
        model_group=args.model_group,
        embed=adapter.embed_texts,
        extraction_agent_factory=adapter.build_extraction_agent,
        rem_agent_factory=adapter.build_rem_agent,
        contradiction_agent_factory=adapter.build_contradiction_agent,
        secrets=_secret_env_values(args.secret_env),
    )


def _dream_phase_scope(args: argparse.Namespace) -> MemoryScope:
    return MemoryScope(
        tenant_id=args.tenant_id,
        project_id=args.project_id,
        session_id=args.session_id,
        agent_id=args.agent_id,
        principal=Principal(
            principal_id="hosted-worker-cli",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities={MemoryCapability.ADMIN_REBUILD},
    )


def _event_dict(event: object) -> dict[str, object]:
    return dict(event.__dict__)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
