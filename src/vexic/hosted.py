from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    FreshContextRequest,
    FreshContextResult,
    IngestSourceTranscriptRequest,
    IngestSourceTranscriptResult,
    LoadActiveContextRequest,
    LoadActiveContextResult,
    RunDreamPhaseRequest,
    RunDreamPhaseResult,
    TriggerDreamPhaseRequest,
    TriggerDreamPhaseResult,
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


logger = logging.getLogger(__name__)

_RequestT = TypeVar("_RequestT", bound=MemoryRequest)
_ResultT = TypeVar("_ResultT", bound=MemoryResult)

HOSTED_WRITE_MAX_MESSAGES = 100
HOSTED_WRITE_MAX_CHARS = 250_000

# How long a dream lease survives without a heartbeat before another container
# may steal it. This bounds a holder that DIED: short enough that a crash costs
# at most one skipped sweep rather than a wedged scope, and comfortably under
# the default 30-minute tick.
DREAM_LEASE_TTL = timedelta(minutes=20)

# A live holder heartbeats, so the TTL never lapses under a running chain --
# Deep scales with candidate count and has run 8 minutes in production, so no
# fixed TTL is safely "long enough" on its own. The 4x margin over the interval
# tolerates a few missed renewals (a transient control-plane blip) before the
# lease is at risk.
DREAM_LEASE_RENEW_INTERVAL = timedelta(minutes=5)


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
        request: Request,
        payload: HostedAppendTranscriptBody,
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
        request: Request,
        payload: HostedIngestSourceTranscriptBody,
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
    # Catalog data model only: `customer_target` is the DSN
    # string for the tenant's customer-memory database, or `None` to use the
    # local `db_path` (unchanged behavior). NEVER a token here -- resolving
    # this into a connectable, token-bearing `StorageTarget` is P4 work.
    # `generation` is a repoint counter bumped by Task 12; both fields default
    # so existing `HostedTenant(...)` construction keeps working.
    customer_target: str | None = None
    generation: int = 1


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
    project_id: str | None = None
    key_id: str | None = None


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
    key_id: str | None = None


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
    project_id: str | None = None


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
    "fresh_context": HostedRateLimitRule(limit=30, window_seconds=60),
    "load_active_context": HostedRateLimitRule(limit=30, window_seconds=60),
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
        # NOTE(alpha): O(n) prune is fine for staging; production needs a durable limiter.
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
        customer_target_resolver: Callable[[HostedTenant], StorageTarget | None]
        | None = None,
    ) -> None:
        self.catalog = catalog
        self.api_keys = api_keys
        self.telemetry = telemetry
        self.rate_limiter = rate_limiter or HostedInMemoryRateLimiter()
        self.dream_phase_ports = dream_phase_ports
        # Per-tenant customer-memory resolver. Given a
        # `HostedTenant`, it returns a connectable, token-bearing
        # `StorageTarget` for the tenant's Turso customer-memory DB (derived
        # from its catalog `customer_target` DSN), or `None` to use the local
        # `db_path`. This replaces the Task-7b single-DB override + its
        # single-tenant guard: resolution is now per-tenant, so there is no
        # shared-DB / second-tenant hazard. Secrets (the minted jwt) live only
        # inside the resolver, which is built in `adapters/`.
        self._customer_target_resolver = customer_target_resolver
        # Per-(tenant_id, agent_id) in-flight guard so a concurrent trigger or
        # sweep is a cheap no-op instead of a second dream over one scope. Two
        # layers: this in-process set is the fast path, and a durable
        # control-plane lease (ADR 0032) makes the guard hold across container
        # boundaries -- a rolling deploy overlaps two containers, each sweeping
        # on boot, and a process-local lock cannot see the other one.
        self._dream_trigger_lock = threading.Lock()
        self._dream_trigger_inflight: set[tuple[str, str | None]] = set()
        # Identifies this process as a lease holder. A lease is only released
        # by the holder that took it, so a late release cannot free a scope
        # another container has since claimed.
        self._dream_lease_holder = f"dream-{uuid.uuid4()}"
        self._dream_lease_heartbeats: set[asyncio.Task[None]] = set()
        # Background dream-trigger job events, mirroring
        # `HostedBackgroundJobRunner.job_events` for the trigger path. The
        # list append itself is safe on the asyncio event loop (single
        # cooperative thread), but `threading.Lock` is cheap insurance since
        # the surrounding job body crosses a real worker thread via
        # `asyncio.to_thread` (see `_run_dream_trigger_job`).
        self._dream_trigger_job_events_lock = threading.Lock()
        self.dream_trigger_job_events: list[HostedJobEvent] = []
        # Strong references to in-flight `asyncio.create_task(...)` background
        # jobs so they are never garbage-collected mid-flight; tests can await
        # them deterministically via this set instead of sleeping.
        self._background_tasks: set[asyncio.Task] = set()

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

    async def fresh_context(
        self,
        api_key: str,
        request: FreshContextRequest,
    ) -> FreshContextResult:
        return await self._call(
            "fresh_context",
            api_key,
            request,
            request.required_capability,
            lambda bound, tenant: self._local_service(tenant).fresh_context(bound),
        )

    async def load_active_context(
        self,
        api_key: str,
        request: LoadActiveContextRequest,
    ) -> LoadActiveContextResult:
        return await self._call(
            "load_active_context",
            api_key,
            request,
            request.required_capability,
            lambda bound, tenant: self._local_service(tenant).load_active_context(bound),
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

    async def trigger_dream_phase(
        self,
        api_key: str,
        request: TriggerDreamPhaseRequest,
    ) -> TriggerDreamPhaseResult:
        """Schedule a tenant(+agent)-wide summarize sweep and return at once.

        THE CRITICAL ROUTING (ADR 0025, plan D1-D3): this
        authenticates + binds + rate-checks EXACTLY ONCE, at this trigger
        boundary, against `TriggerDreamPhaseRequest`'s own
        `MemoryCapability.DREAM_TRIGGER` -- deliberately NOT
        `ADMIN_REBUILD`. The actual phase execution is a server-minted,
        pre-bound `RunDreamPhaseRequest` (built in `_schedule_dream_trigger`)
        that is executed by calling `_run_dream_phase_with_usage` DIRECTLY.
        It must never re-enter `self._call` / `self._bind_request` /
        `HostedBackgroundJobRunner.run_dream_phase`: the capability
        intersection in `_bind_request` would strip the minted
        `ADMIN_REBUILD` (a trigger-only key doesn't hold it) and 403 the
        background job, and the 6/hour `run_dream_phase` rate bucket would be
        double-counted.
        """
        operation = "run_dream_phase"
        auth: HostedAuthContext | None = None
        bound: TriggerDreamPhaseRequest | None = None
        try:
            auth = self.api_keys.authenticate(api_key)
            bound, tenant = self._bind_request(auth, request, request.required_capability)
            self.rate_limiter.check(operation, auth)
            result = self._schedule_dream_trigger(bound, tenant, auth)
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
        self._record_request(operation, bound, status="ok", auth=auth)
        return result

    def _schedule_dream_trigger(
        self,
        request: TriggerDreamPhaseRequest,
        tenant: HostedTenant,
        auth: HostedAuthContext,
    ) -> TriggerDreamPhaseResult:
        ports = self.dream_phase_ports
        if ports is None or ports.summary_agent_factory is None:
            # Fail closed SYNCHRONOUSLY, before any task is scheduled, so the
            # caller gets a real 503 signal at trigger time instead of a
            # silently-swallowed background failure.
            raise missing_host_port("Dream phase")
        lock_key = (request.scope.tenant_id, request.scope.agent_id)
        if not self._acquire_dream_trigger_lock(lock_key):
            return TriggerDreamPhaseResult(status="skipped", reason="already_running")
        try:
            minted_scope = request.scope.model_copy(
                update={"capabilities": {MemoryCapability.ADMIN_REBUILD}}
            )
            minted_request = RunDreamPhaseRequest(
                scope=minted_scope,
                phase=DreamPhase.SUMMARIZE,
                # Deliberate: matches the fresh_context header-bound precedent
                # (hosted_http.py). The phase still receives adapter-level
                # `forbidden_secret_values` via `ports.secrets`.
                redaction=RedactionContext(forbidden_values=()),
            )
            job_id = secrets.token_hex(8)
            self._record_dream_trigger_job(job_id, minted_request, auth, status="running")
            task = asyncio.create_task(
                self._run_dream_trigger_job(job_id, minted_request, tenant, auth, lock_key)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            self._start_dream_lease_heartbeat(lock_key, task)
        except BaseException:
            # Anything raised between acquiring the lock and successfully
            # handing the job off to `asyncio.create_task` must release the
            # lock -- otherwise it wedges forever: this trigger 500s and
            # every subsequent trigger for the same (tenant, agent) silently
            # returns `skipped`/`already_running` until process restart.
            self._release_dream_trigger_lock(lock_key)
            raise
        return TriggerDreamPhaseResult(status="scheduled")

    def _acquire_dream_trigger_lock(self, key: tuple[str, str | None]) -> bool:
        with self._dream_trigger_lock:
            if key in self._dream_trigger_inflight:
                return False
            self._dream_trigger_inflight.add(key)
        try:
            claimed = self._acquire_dream_lease(key)
        except BaseException:
            # A throwing control plane must not strand the in-process claim:
            # the scope would then be skipped as "already running" by every
            # later sweep in this process until it restarts.
            with self._dream_trigger_lock:
                self._dream_trigger_inflight.discard(key)
            raise
        if claimed:
            return True
        # Another container holds the scope. Drop the in-process claim too, or
        # this process would refuse the scope forever once the other releases.
        with self._dream_trigger_lock:
            self._dream_trigger_inflight.discard(key)
        return False

    def _release_dream_trigger_lock(
        self,
        key: tuple[str, str | None],
        *,
        release_lease: bool = True,
    ) -> None:
        with self._dream_trigger_lock:
            self._dream_trigger_inflight.discard(key)
        if not release_lease:
            # A cancelled job's phase keeps running: the worker-thread event
            # loop cannot be interrupted. Releasing now would hand the scope to
            # the next container while this one is still writing it -- exactly
            # the collision the lease exists to prevent. Let the lease lapse
            # instead; the TTL covers the draining worker.
            return
        try:
            self._release_dream_lease(key)
        except Exception:
            # A throwing control plane must not escape the job's `finally` and
            # mask the job's own outcome. The lease row simply lapses on its
            # TTL, so the scope is skipped for at most one lease period instead
            # of being stranded forever.
            logger.exception("Dream lease release failed; leaving it to lapse.")

    def _acquire_dream_lease(self, key: tuple[str, str | None]) -> bool:
        tenant_id, agent_id = key
        now = datetime.now(UTC)
        return self.catalog.acquire_dream_lease(
            tenant_id,
            agent_id,
            holder=self._dream_lease_holder,
            now=now.isoformat(),
            expires_at=(now + DREAM_LEASE_TTL).isoformat(),
        )

    def _release_dream_lease(self, key: tuple[str, str | None]) -> None:
        tenant_id, agent_id = key
        self.catalog.release_dream_lease(
            tenant_id, agent_id, holder=self._dream_lease_holder
        )

    def _start_dream_lease_heartbeat(
        self,
        key: tuple[str, str | None],
        job: "asyncio.Task[None]",
    ) -> None:
        """Keep this holder's lease fresh for as long as `job` runs.

        Without this, a chain that outlives DREAM_LEASE_TTL lapses under itself
        and a second container steals the scope mid-write -- the very collision
        the lease exists to prevent.
        """
        tenant_id, agent_id = key

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(DREAM_LEASE_RENEW_INTERVAL.total_seconds())
                try:
                    still_ours = await asyncio.to_thread(
                        self.catalog.renew_dream_lease,
                        tenant_id,
                        agent_id,
                        holder=self._dream_lease_holder,
                        expires_at=(
                            datetime.now(UTC) + DREAM_LEASE_TTL
                        ).isoformat(),
                    )
                except Exception:
                    # A blip must not kill the chain: the TTL's margin over the
                    # renew interval tolerates missed renewals, and the job's
                    # own completion still releases the lease.
                    logger.exception("Dream lease renewal failed.")
                    continue
                if not still_ours:
                    # The lease lapsed and another container took the scope.
                    # Running the rest of the chain would write to the tenant
                    # database while the new holder dreams the same scope -- the
                    # collision this lease exists to prevent, only silent.
                    #
                    # Cancelling stops the REMAINING phases. It cannot abort the
                    # phase already in flight: that runs on a worker-thread event
                    # loop which cannot be interrupted (see
                    # `_run_system_dream_job`), so its writes still land. This
                    # bounds the overlap to one phase rather than eliminating it.
                    # A cancelled job leaves sweep state untouched, so the next
                    # tick re-evaluates the scope cleanly.
                    logger.warning(
                        "Dream lease lost mid-chain; stopping the job for one scope."
                    )
                    job.cancel()
                    return

        # Deliberately NOT in `_background_tasks`: those are awaited on drain
        # and shutdown, and a heartbeat is cancelled rather than completed. The
        # separate set just keeps a strong reference so it is not GC'd mid-flight.
        heartbeat = asyncio.create_task(_heartbeat())
        self._dream_lease_heartbeats.add(heartbeat)
        heartbeat.add_done_callback(self._dream_lease_heartbeats.discard)
        job.add_done_callback(lambda _job: heartbeat.cancel())

    async def _run_dream_trigger_job(
        self,
        job_id: str,
        request: RunDreamPhaseRequest,
        tenant: HostedTenant,
        auth: HostedAuthContext,
        lock_key: tuple[str, str | None],
    ) -> None:
        """Run the minted dream phase on its own worker-thread event loop.

        `run_summarize_phase` is itself a coroutine mixing `await agent.run`
        with synchronous sqlite I/O; running it on the serving loop would
        stall every other request. `asyncio.to_thread(asyncio.run, ...)`
        gives it its own loop on a worker thread instead -- sqlite is safe
        here because every storage call opens/closes its own connection
        in-thread. Exceptions are swallowed into the existing `_record_job`
        error path; they never propagate out of this task.
        """

        def _run_in_worker_thread() -> tuple[RunDreamPhaseResult, UsageSummary]:
            return asyncio.run(self._run_dream_phase_with_usage(request, tenant))

        cancelled = False
        try:
            result, usage = await asyncio.to_thread(_run_in_worker_thread)
        except asyncio.CancelledError:
            # The worker thread cannot be interrupted, so the phase may still be
            # writing this scope. Hold the lease and let it lapse on its TTL
            # rather than handing a live scope to the next container.
            cancelled = True
            raise
        except Exception as exc:
            self._record_dream_trigger_job(
                job_id, request, auth, status="error", error_type=type(exc).__name__
            )
            self.record_job_usage(
                operation="run_dream_phase",
                tenant_id=auth.tenant_id,
                principal_id=auth.principal.principal_id,
                status="error",
                error_type=type(exc).__name__,
                project_id=request.scope.project_id,
                key_id=auth.key_id,
            )
        else:
            self._record_dream_trigger_job(job_id, request, auth, status="ok")
            self.record_job_usage(
                operation="run_dream_phase",
                tenant_id=auth.tenant_id,
                principal_id=auth.principal.principal_id,
                status="ok",
                usage=usage,
                project_id=request.scope.project_id,
                key_id=auth.key_id,
            )
        finally:
            self._release_dream_trigger_lock(
                lock_key, release_lease=not cancelled
            )

    def _record_dream_trigger_job(
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
            project_id=request.scope.project_id,
        )
        with self._dream_trigger_job_events_lock:
            self.dream_trigger_job_events.append(event)
        if self.telemetry is not None:
            try:
                self.telemetry.record_job_event(event)
            except Exception:
                pass

    def schedule_system_dream(
        self,
        tenant_id: str,
        *,
        agent_id: str | None,
        phases: tuple[DreamPhase, ...],
    ) -> "asyncio.Task[None] | None":
        """In-server sweeper seam (ADR 0030): schedule pre-bound dream phases
        for one tenant+agent scope under a system principal — no API key.

        Mirrors the trigger endpoint's containment exactly: the minted
        `RunDreamPhaseRequest`s never re-enter `_call`/`_bind_request`, the
        per-(tenant, agent) in-flight lock dedups against concurrent triggers,
        and execution happens on a worker-thread event loop. Returns the
        background task, or None when the scope is already running. Raises
        `HostPortNotConfigured` when the requested phases' ports are absent so
        the sweeper can skip fail-closed instead of scheduling doomed jobs.
        """
        ports = self.dream_phase_ports
        if ports is None:
            raise missing_host_port("Dream phase")
        if DreamPhase.SUMMARIZE in phases and ports.summary_agent_factory is None:
            raise missing_host_port("Dream phase")
        if DreamPhase.LIGHT in phases and ports.extraction_agent_factory is None:
            raise missing_host_port("Dream phase")
        tenant = self.catalog.get_tenant(tenant_id)
        lock_key = (tenant_id, agent_id)
        if not self._acquire_dream_trigger_lock(lock_key):
            return None
        try:
            principal = Principal(
                principal_id="dream-sweeper",
                principal_type=PrincipalType.SYSTEM,
            )
            auth = HostedAuthContext(
                key_id="system:dream-sweeper",
                tenant_id=tenant_id,
                principal=principal,
                capabilities=frozenset({MemoryCapability.ADMIN_REBUILD}),
                project_ids=tenant.project_ids,
            )
            minted_scope = MemoryScope(
                tenant_id=tenant_id,
                agent_id=agent_id,
                principal=principal,
                trust_boundary=TrustBoundary.LOCAL_TRUSTED,
                capabilities={MemoryCapability.ADMIN_REBUILD},
            )
            minted_requests = tuple(
                RunDreamPhaseRequest(
                    scope=minted_scope,
                    phase=phase,
                    redaction=RedactionContext(forbidden_values=()),
                )
                for phase in phases
            )
            task = asyncio.create_task(
                self._run_system_dream_job(minted_requests, tenant, auth, lock_key)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            self._start_dream_lease_heartbeat(lock_key, task)
        except BaseException:
            # Same wedge-prevention rule as `_schedule_dream_trigger`: any
            # failure between lock acquisition and task handoff must release
            # the lock or every later sweep of this scope silently skips.
            self._release_dream_trigger_lock(lock_key)
            raise
        return task

    async def _run_system_dream_job(
        self,
        requests: tuple[RunDreamPhaseRequest, ...],
        tenant: HostedTenant,
        auth: HostedAuthContext,
        lock_key: tuple[str, str | None],
    ) -> None:
        """Run the sweeper's minted phases sequentially on a worker-thread
        event loop (same isolation rationale as `_run_dream_trigger_job`).
        A failing phase records its error and stops the chain — a Deep run
        over a failed Light extraction would promote from stale candidates."""
        cancelled = False
        try:
            for request in requests:
                job_id = secrets.token_hex(8)
                self._record_dream_trigger_job(job_id, request, auth, status="running")

                def _run_in_worker_thread(
                    bound: RunDreamPhaseRequest = request,
                ) -> tuple[RunDreamPhaseResult, UsageSummary]:
                    return asyncio.run(self._run_dream_phase_with_usage(bound, tenant))

                try:
                    _result, usage = await asyncio.to_thread(_run_in_worker_thread)
                except asyncio.CancelledError:
                    # Shutdown cancellation: the worker thread cannot be
                    # interrupted and may still be finishing its phase, so
                    # record the orchestration as errored (callers must not
                    # treat the chain as swept) and propagate.
                    self._record_dream_trigger_job(
                        job_id,
                        request,
                        auth,
                        status="error",
                        error_type="CancelledError",
                    )
                    raise
                except Exception as exc:
                    self._record_dream_trigger_job(
                        job_id,
                        request,
                        auth,
                        status="error",
                        error_type=type(exc).__name__,
                    )
                    self.record_job_usage(
                        operation="run_dream_phase",
                        tenant_id=auth.tenant_id,
                        principal_id=auth.principal.principal_id,
                        status="error",
                        error_type=type(exc).__name__,
                        project_id=request.scope.project_id,
                        key_id=auth.key_id,
                    )
                    return
                self._record_dream_trigger_job(job_id, request, auth, status="ok")
                self.record_job_usage(
                    operation="run_dream_phase",
                    tenant_id=auth.tenant_id,
                    principal_id=auth.principal.principal_id,
                    status="ok",
                    usage=usage,
                    project_id=request.scope.project_id,
                    key_id=auth.key_id,
                )
        except asyncio.CancelledError:
            # The worker thread cannot be interrupted, so a phase may still be
            # writing this scope. Hold the lease and let it lapse on its TTL
            # rather than handing a live scope to the next container.
            cancelled = True
            raise
        finally:
            self._release_dream_trigger_lock(
                lock_key, release_lease=not cancelled
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
        self._record_request(operation, bound, status="ok", auth=auth)
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
        target_scope = getattr(request, "target_scope", None)
        if target_scope is not None:
            # target_scope.tenant_id == scope.tenant_id is enforced by the
            # request model validator; scope.tenant_id is checked above. Bind
            # target_scope.project_id against the same allowed-projects rules as
            # the actor scope so a project-scoped key cannot tombstone another
            # project (including via a None/wildcard target) in its tenant.
            target_project_id = target_scope.project_id
            if target_project_id is None:
                if auth.project_ids:
                    raise PermissionError(
                        "Target scope project_id is required for project-scoped API key."
                    )
            else:
                if target_project_id not in tenant.project_ids:
                    raise PermissionError(
                        "Target scope project_id is not provisioned for tenant."
                    )
                if target_project_id not in auth.project_ids:
                    raise PermissionError(
                        "Target scope project_id is not allowed for API key."
                    )
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

    def storage_target_for(self, tenant: HostedTenant) -> str | StorageTarget:
        """Resolve a tenant's memory storage: the customer-target resolver's
        `StorageTarget` (Turso backend) when configured, else the local
        `db_path`. Every read of tenant memory storage must go through this
        seam; reading `tenant.db_path` directly bypasses the Turso backend."""
        target = (
            self._customer_target_resolver(tenant)
            if self._customer_target_resolver is not None
            else None
        )
        return target if target is not None else tenant.db_path

    def _local_service(self, tenant: HostedTenant) -> LocalMemoryService:
        db_path = self.storage_target_for(tenant)
        needs_schema_init = isinstance(db_path, StorageTarget)
        service = LocalMemoryService(
            db_path=db_path,
            tenant_id=tenant.tenant_id,
            embed=self.dream_phase_ports.embed if self.dream_phase_ports else None,
            dream_phase_ports=self.dream_phase_ports,
        )
        if needs_schema_init:
            # A per-tenant Turso customer-memory DB is provisioned/schema-init'd
            # out of band (factory or provisioning flow), not by filesystem
            # tenant provisioning. `init_db`'s process-level memo (keyed on
            # target identity) makes this a cheap no-op after the first real
            # call, so requesting it here is safe and idempotent.
            service.init_schema()
        return service

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
        key_id = auth.key_id if auth is not None else None
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
                key_id=key_id,
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
        key_id: str | None = None,
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
                    key_id=key_id,
                )
            )
        except Exception:
            # Best-effort telemetry; recording must never fail the job itself.
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
                key_id=auth.key_id,
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
            key_id=auth.key_id,
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
            project_id=request.scope.project_id,
        )
        self.job_events.append(event)
        try:
            self.telemetry.record_job_event(event)
        except Exception:
            # Best-effort telemetry; recording must never fail the job itself.
            pass


def add_run_dream_phase_subcommand(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    run_phase = subcommands.add_parser("run-dream-phase")
    run_phase.add_argument("--root")
    run_phase.add_argument("--api-key-env", default="VEXIC_API_KEY")
    run_phase.add_argument("--adapter", help=f"defaults to {DREAM_PHASE_ADAPTER_ENV}")
    run_phase.add_argument(
        "--model-group",
        help=f"defaults to {DREAM_PHASE_MODEL_GROUP_ENV} or {DEFAULT_DREAM_PHASE_MODEL_GROUP!r}",
    )
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


def run_dream_phase_command(
    args: argparse.Namespace,
    *,
    catalog: Any | None = None,
    keys: Any | None = None,
    customer_target_resolver: Callable[[HostedTenant], StorageTarget | None]
    | None = None,
) -> int:
    """Run one dream phase from the operator CLI.

    The CLI entry point (`vexic.hosted_http.main`) builds `catalog`/`keys`/
    `customer_target_resolver` through the shared `_build_hosted_stores` seam
    so this command honors `VEXIC_CONTROL_PLANE_TARGET` and
    `VEXIC_STORAGE_BACKEND` exactly like `create_service_from_env`.
    The local-filesystem fallback below serves only direct callers in tests.
    """
    from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog

    root = _hosted_root_arg(args.root)
    if catalog is None:
        catalog = HostedTenantCatalog(root)
    if keys is None:
        keys = HostedApiKeyStore(root)
    service = HostedMemoryService(
        catalog,
        keys,
        telemetry=catalog,
        rate_limiter=HostedInMemoryRateLimiter(),
        dream_phase_ports=_dream_phase_ports(args),
        customer_target_resolver=customer_target_resolver,
    )
    # NOTE(alpha): staging CLI assumes no concurrent tenant writers; add event ids if shared.
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


def resolve_storage_backend(env: Mapping[str, str]) -> str:
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


def resolve_control_plane_target(env: Mapping[str, str]) -> str:
    """Resolve the non-secret ``VEXIC_CONTROL_PLANE_TARGET`` selection flag.

    Selects where the control-plane catalog and API-key store live, separate
    from ``VEXIC_STORAGE_BACKEND`` (which routes only customer memory). Defaults
    to ``"local"`` (the filesystem ``control-plane.db`` on the hosted root,
    unchanged behavior). ``"turso"`` routes the catalog to the managed libSQL
    control-plane database resolved by ``adapters.turso_adapter`` (ADR 0019
    Addendum 4); any other value is rejected. Never reads secrets --
    only the flag -- so it is safe to keep in ``src/vexic``.
    """
    value = env.get("VEXIC_CONTROL_PLANE_TARGET", "local").strip().lower()
    if value not in {"local", "turso"}:
        raise ValueError(f"invalid VEXIC_CONTROL_PLANE_TARGET: {value!r}")
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


DREAM_PHASE_ADAPTER_ENV = "VEXIC_DREAM_PHASE_ADAPTER"
DREAM_PHASE_MODEL_GROUP_ENV = "VEXIC_DREAM_PHASE_MODEL_GROUP"
DEFAULT_DREAM_PHASE_MODEL_GROUP = "hosted-dream"
SUMMARIZE_DAILY_SPAN_BUDGET_ENV = "VEXIC_SUMMARIZE_DAILY_SPAN_BUDGET"
DEFAULT_SUMMARIZE_DAILY_SPAN_BUDGET = 50


def dream_phase_ports_from_env(env: Mapping[str, str]) -> DreamPhasePorts | None:
    """Build dream-phase ports from non-secret deploy configuration.

    Reads only the host adapter *file path* and model group name; provider
    secrets stay inside the adapter module itself (which reads its own
    environment, e.g. ``OPENROUTER_API_KEY``), never in ``src/vexic``. An
    unset/blank adapter path returns ``None`` so every model-backed operation
    keeps failing closed with ``HostPortNotConfigured``. A configured but
    unloadable adapter raises at service build time so a misconfigured deploy
    fails loudly instead of serving with silently-missing ports.
    """
    adapter_path = env.get(DREAM_PHASE_ADAPTER_ENV, "").strip()
    if not adapter_path:
        return None
    adapter = _load_dream_phase_adapter(Path(adapter_path))
    return DreamPhasePorts(
        model_group=_dream_phase_model_group(env),
        embed=adapter.embed_texts,
        extraction_agent_factory=adapter.build_extraction_agent,
        contradiction_agent_factory=adapter.build_contradiction_agent,
        summary_agent_factory=getattr(adapter, "build_summary_agent", None),
        daily_span_budget=_dream_phase_daily_span_budget(env),
    )


def _dream_phase_model_group(env: Mapping[str, str]) -> str:
    return (
        env.get(DREAM_PHASE_MODEL_GROUP_ENV, "").strip()
        or DEFAULT_DREAM_PHASE_MODEL_GROUP
    )


def _dream_phase_daily_span_budget(env: Mapping[str, str]) -> int:
    """Parse the summarize phase's daily span budget (cost runaway guard).

    Unset/blank/unparseable -> the default (50); a negative value is treated
    as 0 (fully closed) rather than raising, since this gates a background
    job rather than serving a request -- fail closed on cost, not loud.
    """
    raw = env.get(SUMMARIZE_DAILY_SPAN_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_SUMMARIZE_DAILY_SPAN_BUDGET
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SUMMARIZE_DAILY_SPAN_BUDGET
    return max(value, 0)


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
        "build_contradiction_agent",
    ):
        if not callable(getattr(module, name, None)):
            raise missing_host_port("Dream phase adapter")
    return module


def _dream_phase_ports(args: argparse.Namespace) -> DreamPhasePorts:
    adapter_path = args.adapter or os.environ.get(DREAM_PHASE_ADAPTER_ENV, "").strip()
    if not adapter_path:
        raise missing_host_port(
            "Dream phase adapter",
            hint=f"Pass --adapter or set {DREAM_PHASE_ADAPTER_ENV}.",
        )
    adapter = _load_dream_phase_adapter(Path(adapter_path))
    return DreamPhasePorts(
        model_group=args.model_group or _dream_phase_model_group(os.environ),
        embed=adapter.embed_texts,
        extraction_agent_factory=adapter.build_extraction_agent,
        contradiction_agent_factory=adapter.build_contradiction_agent,
        summary_agent_factory=getattr(adapter, "build_summary_agent", None),
        secrets=_secret_env_values(args.secret_env),
        daily_span_budget=_dream_phase_daily_span_budget(os.environ),
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
