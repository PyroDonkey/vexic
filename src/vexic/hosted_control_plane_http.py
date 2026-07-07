from __future__ import annotations

import hmac
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import NoReturn, ParamSpec, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from vexic.hosted import HostedJobEvent, HostedMemoryService, HostedUsageEvent
from vexic.hosted_http import create_app as create_hosted_memory_app
from vexic.hosted_http import create_service_from_env
from vexic.hosted_local import (
    HostedApiKeyRecord,
    HostedProjectRecord,
    HostedSetupTokenRecord,
    _CONTROL_PLANE_AGENT_CAPABILITIES,
)
from vexic.storage.errors import (
    is_operational_error,
    is_retryable_operational_error,
    is_unique_violation,
)


logger = logging.getLogger(__name__)

_ControlPlaneParams = ParamSpec("_ControlPlaneParams")
_ControlPlaneResponseT = TypeVar("_ControlPlaneResponseT", bound=Response)


def create_app(
    service: HostedMemoryService | None = None,
    *,
    mcp_forbidden_secret_values: tuple[str, ...] = (),
    control_plane_tokens: tuple[str, ...] | None = None,
) -> FastAPI:
    service = service or create_service_from_env()
    if control_plane_tokens is None:
        control_plane_tokens = _control_plane_tokens_from_env()
    app = create_hosted_memory_app(
        service,
        mcp_forbidden_secret_values=mcp_forbidden_secret_values,
    )
    register_control_plane_routes(
        app,
        service,
        control_plane_tokens=_normalize_control_plane_tokens(control_plane_tokens),
    )
    return app


def register_control_plane_routes(
    app: FastAPI,
    service: HostedMemoryService,
    *,
    control_plane_tokens: tuple[str, ...],
) -> None:
    @app.exception_handler(_ControlPlaneBadRequest)
    async def control_plane_bad_request(
        _: Request,
        exc: _ControlPlaneBadRequest,
    ) -> JSONResponse:
        return _error_response(400, "invalid_request", str(exc))

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/tenant")
    @_control_plane_storage_boundary
    async def provision_control_plane_tenant(
        clerk_org_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(
                401,
                "unauthorized",
                "Invalid control-plane credential.",
            )
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        return JSONResponse(
            {"tenant": {"clerkOrgId": clerk_org_id, "tenantId": tenant_id}}
        )

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects")
    @_control_plane_storage_boundary
    async def list_control_plane_projects(
        clerk_org_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return JSONResponse({"projects": []})
        projects = service.catalog.list_control_projects(tenant_id)
        return JSONResponse({"projects": [_project_payload(project) for project in projects]})

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/projects")
    @_control_plane_storage_boundary
    async def create_control_plane_project(
        clerk_org_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        payload = await _json_body(request)
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        try:
            project = service.catalog.create_control_project(
                tenant_id,
                name=_string_field(payload, "name", default=""),
                environment=_string_field(payload, "environment", default="production"),
            )
        except ValueError as exc:
            return _error_response(400, "invalid_request", str(exc))
        return JSONResponse({"project": _project_payload(project)}, status_code=201)

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}")
    @_control_plane_storage_boundary
    async def get_control_plane_project(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            project = service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        return JSONResponse({"project": _project_payload(project)})

    @app.put("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}")
    @_control_plane_storage_boundary
    async def put_control_plane_project(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        payload = await _json_body(request)
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        try:
            project = service.catalog.upsert_control_project(
                tenant_id,
                project_id,
                name=_string_field(payload, "name", default=""),
                environment=_string_field(payload, "environment", default="production"),
            )
        except ValueError as exc:
            return _error_response(400, "invalid_request", str(exc))
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        return JSONResponse({"project": _project_payload(project)})

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/keys")
    @_control_plane_storage_boundary
    async def list_control_plane_keys(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        keys = service.api_keys.list_control_plane_keys(
            tenant_id=tenant_id,
            project_id=project_id,
            include_revoked=request.query_params.get("include") == "revoked",
        )
        return JSONResponse({"keys": [_key_payload(key) for key in keys]})

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/keys")
    @_control_plane_storage_boundary
    async def create_control_plane_key(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        payload = await _json_body(request)
        try:
            capability = _string_field(payload, "capability", default="v1-memory")
            if capability != "v1-memory":
                return _error_response(400, "invalid_request", "Unsupported capability.")
        except ValueError as exc:
            return _error_response(400, "invalid_request", str(exc))
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        try:
            provisioned, key = service.api_keys.create_control_plane_key(
                tenant_id=tenant_id,
                project_id=project_id,
                name=_string_field(payload, "name", default=""),
                agent_scope=_string_field(payload, "agentScope", default="shared"),
            )
        except ValueError as exc:
            return _error_response(400, "invalid_request", str(exc))
        return JSONResponse({"rawKey": provisioned.raw_key, "key": _key_payload(key)}, status_code=201)

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/keys/{key_id}/revoke")
    @_control_plane_storage_boundary
    async def revoke_control_plane_key(
        clerk_org_id: str,
        project_id: str,
        key_id: str,
        request: Request,
    ) -> Response:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Key not found.")
        try:
            service.api_keys.revoke_control_plane_key(
                tenant_id=tenant_id,
                project_id=project_id,
                key_id=key_id,
                revoked_by="console-service",
            )
        except PermissionError:
            return _error_response(404, "not_found", "Key not found.")
        return Response(status_code=204)

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/setup-tokens")
    @_control_plane_storage_boundary
    async def create_control_plane_setup_token(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        payload = await _json_body(request)
        try:
            agent_scope = _string_field(payload, "agentScope", default="")
            session_id = _string_field(payload, "sessionId", default="")
        except ValueError as exc:
            return _error_response(400, "invalid_request", str(exc))
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        provisioned, record = service.api_keys.create_setup_token(
            tenant_id=tenant_id,
            project_id=project_id,
            agent_scope=agent_scope,
            session_id=session_id,
        )
        return JSONResponse(
            {"rawToken": provisioned.raw_token, "token": _setup_token_payload(record)},
            status_code=201,
        )

    @app.post(
        "/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}"
        "/setup-tokens/{token_id}/revoke"
    )
    @_control_plane_storage_boundary
    async def revoke_control_plane_setup_token(
        clerk_org_id: str,
        project_id: str,
        token_id: str,
        request: Request,
    ) -> Response:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Setup token not found.")
        try:
            service.api_keys.revoke_setup_token(
                tenant_id=tenant_id,
                project_id=project_id,
                token_id=token_id,
                revoked_by="console-service",
            )
        except PermissionError:
            return _error_response(404, "not_found", "Setup token not found.")
        return Response(status_code=204)

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/setup-tokens")
    @_control_plane_storage_boundary
    async def list_control_plane_setup_tokens(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        tokens = service.api_keys.list_setup_tokens(
            tenant_id=tenant_id,
            project_id=project_id,
            include_consumed=_query_flag(request, "includeConsumed"),
            include_revoked=_query_flag(request, "includeRevoked"),
        )
        now = _utc_iso(datetime.now(UTC))
        return JSONResponse(
            {"tokens": [_setup_token_list_payload(token, now=now) for token in tokens]}
        )

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/usage")
    @_control_plane_storage_boundary
    async def get_control_plane_tenant_usage(
        clerk_org_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        period_start, period_end = _usage_period()
        if tenant_id is None:
            return JSONResponse(
                {
                    "usage": _usage_payload(
                        [],
                        period_start=period_start,
                        period_end=period_end,
                    )
                }
            )
        events = service.catalog.usage_events(
            tenant_id,
            recorded_at_gte=period_start,
            recorded_at_lt=period_end,
        )
        return JSONResponse(
            {
                "usage": _usage_payload(
                    events,
                    period_start=period_start,
                    period_end=period_end,
                )
            }
        )

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/usage/by-key")
    @_control_plane_storage_boundary
    async def get_control_plane_project_usage_by_key(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        window_start, window_end = _days_window(request)
        return JSONResponse(
            {
                "byKey": service.catalog.usage_by_key(
                    tenant_id,
                    project_id=project_id,
                    recorded_at_gte=window_start,
                    recorded_at_lt=window_end,
                )
            }
        )

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/usage")
    @_control_plane_storage_boundary
    async def get_control_plane_project_usage(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        period_start, period_end = _usage_period()
        events = service.catalog.usage_events(
            tenant_id,
            project_id=project_id,
            recorded_at_gte=period_start,
            recorded_at_lt=period_end,
        )
        usage = _usage_payload(
            events,
            period_start=period_start,
            period_end=period_end,
            project_id=project_id,
        )
        if request.query_params.get("granularity") == "day":
            window_start, window_end = _days_window(request)
            usage["daily"] = service.catalog.usage_daily(
                tenant_id,
                project_id=project_id,
                recorded_at_gte=window_start,
                recorded_at_lt=window_end,
            )
        return JSONResponse({"usage": usage})

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/jobs")
    @_control_plane_storage_boundary
    async def list_control_plane_jobs(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        raw_limit = request.query_params.get("limit", "50")
        try:
            limit = max(1, min(200, int(raw_limit)))
        except ValueError:
            limit = 50
        events = service.catalog.job_events(tenant_id, project_id=project_id, limit=limit)
        return JSONResponse({"jobs": [_job_payload(event) for event in events]})


def _api_key(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    explicit = request.headers.get("x-vexic-api-key")
    if explicit is not None and explicit.strip():
        return explicit.strip()
    return None


def _has_control_plane_credential(
    request: Request,
    control_plane_tokens: tuple[str, ...],
) -> bool:
    presented = _api_key(request)
    if presented is None or not control_plane_tokens:
        return False
    matched = False
    for configured in control_plane_tokens:
        matched = hmac.compare_digest(configured, presented) or matched
    return matched


def _control_plane_tokens_from_env() -> tuple[str, ...]:
    return tuple(os.environ.get("VEXIC_CONTROL_PLANE_TOKENS", "").split(","))


def _normalize_control_plane_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        stripped = value.strip()
        if stripped:
            normalized.append(stripped)
    return tuple(normalized)


class _ControlPlaneBadRequest(ValueError):
    pass


def _control_plane_storage_boundary(
    handler: Callable[_ControlPlaneParams, Awaitable[_ControlPlaneResponseT]],
) -> Callable[_ControlPlaneParams, Awaitable[_ControlPlaneResponseT | JSONResponse]]:
    @wraps(handler)
    async def wrapped(
        *args: _ControlPlaneParams.args,
        **kwargs: _ControlPlaneParams.kwargs,
    ) -> _ControlPlaneResponseT | JSONResponse:
        # Broad catch spanning both backends: local sqlite3 raises typed
        # sqlite3.Error subclasses; hosted libSQL raises a bare ValueError with
        # the Hrana/`code:` payload (ADR 0019). The shared classifiers normalize
        # both. Any exception that is NOT a storage constraint/operational error
        # -- including a domain ValueError such as _ControlPlaneBadRequest bound
        # to its own FastAPI exception handler -- re-raises untouched.
        try:
            return await handler(*args, **kwargs)
        except (sqlite3.Error, ValueError) as exc:
            if is_unique_violation(exc):
                _log_control_plane_sqlite_error("integrity", exc, args, kwargs)
                return _error_response(409, "conflict", "Control-plane write conflict.")
            if is_operational_error(exc):
                if is_retryable_operational_error(exc):
                    _log_control_plane_sqlite_error(
                        "retryable_operational",
                        exc,
                        args,
                        kwargs,
                    )
                    return _error_response(
                        503,
                        "storage_unavailable",
                        "Control-plane storage is temporarily unavailable.",
                    )
                _log_control_plane_sqlite_error("operational", exc, args, kwargs)
                return _error_response(500, "internal_error", "Control-plane storage failed.")
            if isinstance(exc, sqlite3.Error):
                _log_control_plane_sqlite_error("sqlite", exc, args, kwargs)
                return _error_response(500, "internal_error", "Control-plane storage failed.")
            raise

    return wrapped


def _log_control_plane_sqlite_error(
    category: str,
    exc: BaseException,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    request = _request_from_handler_call(args, kwargs)
    path = request.url.path if request is not None else "<unknown>"
    correlation_id = _correlation_id(request)
    logger.warning(
        "control-plane sqlite error category=%s exception_type=%s path=%s correlation_id=%s",
        category,
        type(exc).__name__,
        path,
        correlation_id or "<none>",
    )


def _request_from_handler_call(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> Request | None:
    request = kwargs.get("request")
    if isinstance(request, Request):
        return request
    for arg in args:
        if isinstance(arg, Request):
            return arg
    return None


def _correlation_id(request: Request | None) -> str | None:
    if request is None:
        return None
    for header in ("x-request-id", "x-correlation-id"):
        value = request.headers.get(header)
        if value is not None and value.strip():
            return value.strip()
    return None


def _provision_control_tenant(service: HostedMemoryService, clerk_org_id: str) -> str:
    try:
        return service.catalog.provision_customer_account(clerk_org_id)
    except ValueError as exc:
        _classify_control_plane_value_error(exc)


def _resolve_control_tenant(
    service: HostedMemoryService,
    clerk_org_id: str,
) -> str | None:
    try:
        return service.catalog.resolve_customer_tenant(clerk_org_id)
    except ValueError as exc:
        _classify_control_plane_value_error(exc)


def _classify_control_plane_value_error(exc: ValueError) -> NoReturn:
    # On the hosted libSQL/Turso backend a genuine constraint/operational
    # SQL failure surfaces as a bare `ValueError` (ADR 0019). Let it
    # propagate to the storage boundary for 409/503/500 classification
    # instead of mis-wrapping it as an HTTP 400 domain-validation error.
    if is_unique_violation(exc) or is_operational_error(exc):
        raise exc
    raise _ControlPlaneBadRequest(str(exc)) from exc


async def _json_body(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _string_field(
    payload: dict[str, object],
    key: str,
    *,
    default: str,
) -> str:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string.")
    return value.strip() or default


# Sorted so the scope template's capability order is stable across server
# restarts (frozenset iteration order varies with PYTHONHASHSEED).
_CONTROL_PLANE_SCOPE_CAPABILITIES = sorted(
    capability.value for capability in _CONTROL_PLANE_AGENT_CAPABILITIES
)


def _project_payload(project: HostedProjectRecord) -> dict[str, str]:
    return {
        "id": project.project_id,
        "tenantId": project.tenant_id,
        "name": project.name,
        "environment": project.environment,
        "createdAt": project.created_at,
    }


def _key_payload(key: HostedApiKeyRecord) -> dict[str, object]:
    return {
        "id": key.key_id,
        "tenantId": key.tenant_id,
        "projectId": key.project_id,
        "name": key.name,
        "capability": key.capability,
        "agentScope": key.agent_scope,
        "scopeTemplate": _scope_template(key.tenant_id, key.project_id, key.agent_scope),
        "prefix": key.prefix,
        "last4": key.last4,
        "display": key.display,
        "createdAt": key.created_at,
        "createdVia": key.created_via,
        "revokedAt": key.revoked_at,
        "lastUsedAt": key.last_used_at,
    }


def _setup_token_payload(record: HostedSetupTokenRecord) -> dict[str, str]:
    return {
        "id": record.token_id,
        "tenantId": record.tenant_id,
        "projectId": record.project_id,
        "agentScope": record.agent_scope,
        "sessionId": record.session_id,
        "createdAt": record.created_at,
        "expiresAt": record.expires_at,
    }


def _query_flag(request: Request, name: str) -> bool:
    # Setup-token listing defaults to actionable (pending) tokens; consumed and
    # revoked history is opt-in, so an absent flag reads False.
    value = request.query_params.get(name)
    return value is not None and value.strip().lower() in ("1", "true", "yes")


def _setup_token_status(record: HostedSetupTokenRecord, *, now: str) -> str:
    # ISO-Z timestamps are lexically ordered, so this string compare deliberately
    # mirrors the exchange path's own string compare `stored.expires_at <= now`
    # (hosted_local exchange_setup_token, in-memory and the SQL `expires_at > ?`).
    # Matching that exact comparison is the point: a token this reports as
    # `pending` is one exchange would still accept, and one it reports `expired`
    # is one exchange would reject. Parsing to datetimes here would instead make
    # the displayed status disagree with what exchange enforces.
    #
    # "consumed" wins over "revoked": once a token is exchanged, a durable Agent
    # API key exists and revoking the setup token afterward does not retract that
    # access. Reporting a consumed-then-revoked token as "revoked" would imply the
    # setup granted nothing, which is wrong.
    if record.consumed_at is not None:
        return "consumed"
    if record.revoked_at is not None:
        return "revoked"
    if record.expires_at <= now:
        return "expired"
    return "pending"


def _setup_token_list_payload(
    record: HostedSetupTokenRecord, *, now: str
) -> dict[str, str | None]:
    return {
        **_setup_token_payload(record),
        "consumedAt": record.consumed_at,
        "revokedAt": record.revoked_at,
        "status": _setup_token_status(record, now=now),
    }


def _job_payload(event: HostedJobEvent) -> dict[str, object]:
    return {
        "jobId": event.job_id,
        "operation": event.operation,
        "phase": event.phase,
        "status": event.status,
        "recordedAt": event.recorded_at,
    }


def _scope_template(
    tenant_id: str,
    project_id: str,
    agent_scope: str,
) -> dict[str, object]:
    agent_id = None if agent_scope == "shared" else agent_scope
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "principal": {
            "principal_id": agent_scope,
            "principal_type": "agent",
        },
        "trust_boundary": "networked",
        "capabilities": list(_CONTROL_PLANE_SCOPE_CAPABILITIES),
    }


def _usage_period() -> tuple[str, str]:
    now = datetime.now(UTC)
    period_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    return _utc_iso(period_start), _utc_iso(now)


def _days_window(request: Request, *, default_days: int = 30) -> tuple[str, str]:
    raw = request.query_params.get("days", str(default_days))
    try:
        days = max(1, min(90, int(raw)))
    except ValueError:
        days = default_days
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    return _utc_iso(start), _utc_iso(now)


def _utc_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _usage_payload(
    events: list[HostedUsageEvent],
    *,
    period_start: str,
    period_end: str,
    project_id: str | None = None,
) -> dict[str, object]:
    totals = {
        "requests": len(events),
        "writes": sum(1 for event in events if event.operation == "append_transcript"),
        "retrievals": sum(
            1
            for event in events
            if event.operation in {"search_transcript", "search_long_term"}
        ),
        "modelRequests": sum(event.model_requests for event in events),
        "inputTokens": sum(event.input_tokens for event in events),
        "outputTokens": sum(event.output_tokens for event in events),
        "totalTokens": sum(event.total_tokens for event in events),
        "estimatedCostMicros": sum(event.estimated_cost_micros for event in events),
    }
    payload: dict[str, object] = {
        "periodStart": period_start,
        "periodEnd": period_end,
        "totals": totals,
        "caps": {},
    }
    if project_id is not None:
        payload["projectId"] = project_id
    return payload


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers=headers,
    )
