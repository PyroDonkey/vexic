from __future__ import annotations

import argparse
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from vexic import CONTRACT_VERSION
from vexic.contract import (
    AppendTranscriptRequest,
    ExpandHistoryResult,
    ExpandHistoryRequest,
    MemoryCapability,
    MemoryRequest,
    MemoryResult,
    SearchLongTermRequest,
    SearchTranscriptRequest,
)
from vexic.hosted import (
    HostedInMemoryRateLimiter,
    HostedMemoryService,
    HostedRateLimitExceeded,
    add_run_dream_phase_subcommand,
    run_dream_phase_command,
)
from vexic.mcp_http import register_mcp_routes
from vexic.ports import HostPortNotConfigured
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


MAX_BODY_BYTES = 1_000_000
MAX_APPEND_MESSAGES = 100
MAX_APPEND_CHARS = 250_000
MAX_QUERY_CHARS = 1_000
MAX_LIMIT = 20
MAX_EXPAND_HISTORY_MESSAGES = 100
MAX_EXPAND_HISTORY_CHARS = 20_000

_RequestT = TypeVar("_RequestT", bound=MemoryRequest)
_ResultT = TypeVar("_ResultT", bound=MemoryResult)


def create_app(
    service: HostedMemoryService | None = None,
    *,
    mcp_forbidden_secret_values: tuple[str, ...] = (),
    control_plane_tokens: tuple[str, ...] | None = None,
) -> FastAPI:
    service = service or create_service_from_env()
    if control_plane_tokens is None:
        control_plane_tokens = _control_plane_tokens_from_env()
    control_plane_tokens = _normalize_control_plane_tokens(control_plane_tokens)
    app = FastAPI(title="Vexic Hosted Memory", version=CONTRACT_VERSION)
    register_mcp_routes(
        app,
        service,
        forbidden_secret_values=mcp_forbidden_secret_values,
    )

    @app.middleware("http")
    async def cap_body(request: Request, call_next: Callable[[Request], Awaitable[object]]):
        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    body_size = int(content_length)
                except ValueError:
                    return _error_response(400, "invalid_request", "Invalid Content-Length header.")
                if body_size < 0:
                    return _error_response(400, "invalid_request", "Invalid Content-Length header.")
                if body_size > MAX_BODY_BYTES:
                    return _error_response(413, "request_too_large", "Request body is too large.")
            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                return _error_response(413, "request_too_large", "Request body is too large.")
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error_response(422, "invalid_request", "Request body does not match the Vexic contract.")

    @app.exception_handler(_ControlPlaneBadRequest)
    async def control_plane_bad_request(_: Request, exc: _ControlPlaneBadRequest) -> JSONResponse:
        return _error_response(400, "invalid_request", str(exc))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "contract_version": CONTRACT_VERSION}

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/tenant")
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
    async def list_control_plane_projects(
        clerk_org_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        projects = service.catalog.list_control_projects(tenant_id)
        return JSONResponse({"projects": [_project_payload(project) for project in projects]})

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/projects")
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
    async def get_control_plane_project(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        try:
            project = service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        return JSONResponse({"project": _project_payload(project)})

    @app.put("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}")
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
    async def list_control_plane_keys(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        keys = service.api_keys.list_control_plane_keys(
            tenant_id=tenant_id,
            project_id=project_id,
        )
        return JSONResponse({"keys": [_key_payload(key) for key in keys]})

    @app.post("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/keys")
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
    async def revoke_control_plane_key(
        clerk_org_id: str,
        project_id: str,
        key_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _provision_control_tenant(service, clerk_org_id)
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

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/usage")
    async def get_control_plane_tenant_usage(
        clerk_org_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        events = service.catalog.usage_events(tenant_id)
        return JSONResponse({"usage": _usage_payload(events)})

    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/usage")
    async def get_control_plane_project_usage(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _provision_control_tenant(service, clerk_org_id)
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        events = [
            event
            for event in service.catalog.usage_events(tenant_id)
            if event.project_id == project_id
        ]
        return JSONResponse({"usage": _usage_payload(events, project_id=project_id)})

    @app.post("/v1/append_transcript")
    async def append_transcript(request: Request, payload: AppendTranscriptRequest) -> JSONResponse:
        return await _handle(request, payload, service.append_transcript)

    @app.post("/v1/search_transcript")
    async def search_transcript(request: Request, payload: SearchTranscriptRequest) -> JSONResponse:
        return await _handle(request, payload, service.search_transcript)

    @app.post("/v1/search_long_term")
    async def search_long_term(request: Request, payload: SearchLongTermRequest) -> JSONResponse:
        return await _handle(request, payload, service.search_long_term)

    @app.post("/v1/expand_history")
    async def expand_history(request: Request, payload: ExpandHistoryRequest) -> JSONResponse:
        return await _handle(
            request,
            payload,
            lambda api_key, body: service.expand_history(
                api_key,
                body,
                max_rows=MAX_EXPAND_HISTORY_MESSAGES,
            ),
        )

    return app


def create_service_from_env() -> HostedMemoryService:
    root = Path(os.environ.get("VEXIC_HOSTED_ROOT", ".hosted-memory"))
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    return HostedMemoryService(
        catalog,
        keys,
        telemetry=catalog,
        rate_limiter=HostedInMemoryRateLimiter(),
    )


async def _handle(
    request: Request,
    payload: _RequestT,
    call: Callable[[str, _RequestT], Awaitable[_ResultT]],
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    cap_error = _cap_error(payload)
    if cap_error is not None:
        return cap_error
    try:
        result = await call(api_key, payload)
        if isinstance(result, ExpandHistoryResult) and result.truncated and not result.text:
            return _too_large("expand_history range is capped.")
        result = _cap_result(result)
    except HostedRateLimitExceeded as exc:
        return _error_response(
            429,
            "rate_limited",
            "Hosted rate limit exceeded.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except HostPortNotConfigured:
        return _error_response(503, "host_port_not_configured", "Required host port is not configured.")
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _error_response(400, "invalid_request", str(exc))
    except Exception:
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return JSONResponse(result.model_dump(mode="json"))


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
        if not stripped:
            return ()
        normalized.append(stripped)
    return tuple(normalized)


class _ControlPlaneBadRequest(ValueError):
    pass


def _provision_control_tenant(service: HostedMemoryService, clerk_org_id: str) -> str:
    try:
        return service.catalog.provision_customer_account(clerk_org_id)
    except ValueError as exc:
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


def _project_payload(project) -> dict[str, str]:
    return {
        "id": project.project_id,
        "name": project.name,
        "environment": project.environment,
        "createdAt": project.created_at,
    }


def _key_payload(key) -> dict[str, str | None]:
    return {
        "id": key.key_id,
        "projectId": key.project_id,
        "name": key.name,
        "capability": key.capability,
        "agentScope": key.agent_scope,
        "prefix": key.prefix,
        "last4": key.last4,
        "display": key.display,
        "createdAt": key.created_at,
        "revokedAt": key.revoked_at,
    }


def _usage_payload(events, *, project_id: str | None = None) -> dict[str, object]:
    now = datetime.now(UTC)
    period_start = datetime(now.year, now.month, 1, tzinfo=UTC)
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
        "periodStart": period_start.isoformat().replace("+00:00", "Z"),
        "periodEnd": now.isoformat().replace("+00:00", "Z"),
        "totals": totals,
        "caps": {},
    }
    if project_id is not None:
        payload["projectId"] = project_id
    return payload


def _cap_error(payload: MemoryRequest) -> JSONResponse | None:
    if isinstance(payload, AppendTranscriptRequest):
        if len(payload.messages_json) > MAX_APPEND_MESSAGES:
            return _too_large("append_transcript message count is capped.")
        if sum(len(message) for message in payload.messages_json) > MAX_APPEND_CHARS:
            return _too_large("append_transcript payload is capped.")
    if isinstance(payload, (SearchTranscriptRequest, SearchLongTermRequest)):
        if len(payload.query) > MAX_QUERY_CHARS:
            return _too_large("query is capped.")
        if payload.limit < 1 or payload.limit > MAX_LIMIT:
            return _error_response(400, "invalid_request", f"limit must be between 1 and {MAX_LIMIT}.")
    if isinstance(payload, ExpandHistoryRequest):
        if payload.first_message_id > payload.last_message_id:
            return _error_response(
                400,
                "invalid_request",
                "first_message_id must be less than or equal to last_message_id.",
            )
    return None


def _cap_result(result: _ResultT) -> _ResultT:
    if hasattr(result, "text") and len(result.text) > MAX_EXPAND_HISTORY_CHARS:
        return result.model_copy(
            update={"text": result.text[:MAX_EXPAND_HISTORY_CHARS], "truncated": True}
        )
    return result


def _too_large(message: str) -> JSONResponse:
    return _error_response(400, "request_too_large", message)


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


def _root_arg(value: str | None) -> Path:
    return Path(value or os.environ.get("VEXIC_HOSTED_ROOT", ".hosted-memory"))


def _capabilities(values: list[str]) -> set[MemoryCapability]:
    if not values:
        return {MemoryCapability.WRITE, MemoryCapability.SEARCH}
    return {MemoryCapability(value) for value in values}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the Vexic hosted alpha adapter.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    issue = subcommands.add_parser("issue-key")
    issue.add_argument("--root")
    issue.add_argument("--tenant-id", required=True)
    issue.add_argument("--project-id", action="append", default=[])
    issue.add_argument("--principal-id", required=True)
    issue.add_argument("--agent-id", action="append", default=[])
    issue.add_argument("--capability", action="append", default=[])

    revoke = subcommands.add_parser("revoke-key")
    revoke.add_argument("--root")
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--revoked-by")

    add_run_dream_phase_subcommand(subcommands)

    args = parser.parse_args(argv)

    try:
        if args.command == "run-dream-phase":
            return run_dream_phase_command(args)

        root = _root_arg(args.root)
        keys = HostedApiKeyStore(root)

        if args.command == "issue-key":
            catalog = HostedTenantCatalog(root)
            catalog.provision_tenant(args.tenant_id, project_ids=set(args.project_id))
            api_key = keys.create_key(
                tenant_id=args.tenant_id,
                principal_id=args.principal_id,
                capabilities=_capabilities(args.capability),
                project_ids=set(args.project_id),
                agent_ids=set(args.agent_id),
            )
            print(
                json.dumps(
                    {
                        "key_id": api_key.key_id,
                        "raw_key": api_key.raw_key,
                        "tenant_id": args.tenant_id,
                        "project_ids": args.project_id,
                    },
                    sort_keys=True,
                )
            )
            return 0

        keys.revoke_key(args.key_id, revoked_by=args.revoked_by)
        print(json.dumps({"key_id": args.key_id, "revoked": True}, sort_keys=True))
        return 0
    except (HostPortNotConfigured, PermissionError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    except Exception as exc:
        parser.exit(1, f"hosted command failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
