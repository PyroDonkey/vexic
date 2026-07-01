from __future__ import annotations

import argparse
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from vexic import CONTRACT_VERSION
from vexic.contract import (
    ExpandHistoryResult,
    ExpandHistoryRequest,
    MemoryCapability,
    MemoryRequest,
    MemoryResult,
    SearchLongTermRequest,
    SearchTranscriptRequest,
)
from vexic.hosted import (
    HostedAuthContext,
    HostedInMemoryRateLimiter,
    HostedMemoryService,
    HostedRateLimitExceeded,
    add_run_dream_phase_subcommand,
    register_hosted_write_routes,
    resolve_storage_backend,
    run_dream_phase_command,
)
from vexic.mcp_http import register_mcp_routes
from vexic.mcp_http import _scope_from_headers as _read_scope_from_headers
from vexic.ports import HostPortNotConfigured
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


class _TursoTargetResolver:
    """Injection seam (Callable Protocol) for hosted-storage-target resolution.

    Defaults to ``adapters.turso_adapter`` -- the only place ``src/vexic`` is
    permitted to obtain Turso secrets (ADR 0019) -- so this default is imported
    lazily, and callers/tests can pass a fake with no real credentials.
    """

    def control_plane_target(self, env: dict[str, str]):
        from adapters.turso_adapter import control_plane_target

        return control_plane_target(env)

    def customer_memory_target(self, env: dict[str, str]):
        from adapters.turso_adapter import customer_memory_target

        return customer_memory_target(env)


MAX_BODY_BYTES = 1_000_000
MAX_QUERY_CHARS = 1_000
MAX_LIMIT = 20
MAX_EXPAND_HISTORY_MESSAGES = 100
MAX_EXPAND_HISTORY_CHARS = 20_000

_RequestT = TypeVar("_RequestT", bound=MemoryRequest)
_ResultT = TypeVar("_ResultT", bound=MemoryResult)
_SearchRequestT = TypeVar("_SearchRequestT", SearchTranscriptRequest, SearchLongTermRequest)


class _HeaderBoundSearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    limit: int = 5


def create_app(
    service: HostedMemoryService | None = None,
    *,
    mcp_forbidden_secret_values: tuple[str, ...] = (),
) -> FastAPI:
    service = service or create_service_from_env()
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "contract_version": CONTRACT_VERSION}

    register_hosted_write_routes(
        app,
        service,
        api_key_from_request=_api_key,
        handle_payload=_handle_payload,
        error_response=_error_response,
    )

    @app.post("/v1/search_transcript")
    async def search_transcript(request: Request, payload: dict[str, Any]) -> JSONResponse:
        return await _handle_search(
            request,
            payload,
            service,
            SearchTranscriptRequest,
            service.search_transcript,
        )

    @app.post("/v1/search_long_term")
    async def search_long_term(request: Request, payload: dict[str, Any]) -> JSONResponse:
        return await _handle_search(
            request,
            payload,
            service,
            SearchLongTermRequest,
            service.search_long_term,
        )

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


def create_service_from_env(
    *,
    turso_target_resolver: _TursoTargetResolver | None = None,
) -> HostedMemoryService:
    """Build the hosted memory service, per-store, from ``VEXIC_STORAGE_BACKEND``.

    Default (``local``, unset) preserves the exact prior behavior: a
    filesystem-rooted ``HostedTenantCatalog``/``HostedApiKeyStore`` under
    ``VEXIC_HOSTED_ROOT``. ``VEXIC_STORAGE_BACKEND=turso`` selects the
    P2 dogfood customer-memory override (superseded/removed by Task 11's
    catalog per-tenant target model): the control-plane
    (``HostedTenantCatalog``/``HostedApiKeyStore``, i.e. auth + tenant
    lookup) STAYS LOCAL/filesystem-rooted exactly as in the ``local``
    branch, but the customer-memory ``StorageTarget`` is resolved via
    ``turso_target_resolver`` (defaulting to ``adapters.turso_adapter``,
    the only place secrets are read) and injected into
    ``HostedMemoryService`` as ``customer_memory_target_override``. This
    seam exists so tests can inject a fake resolver with no real
    credentials. Turso-backed catalog/key-store construction (moving the
    control-plane itself off the filesystem) lands with per-tenant
    provisioning (Task 10/11).
    """
    backend = resolve_storage_backend(os.environ)
    root = Path(os.environ.get("VEXIC_HOSTED_ROOT", ".hosted-memory"))
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    customer_memory_target_override = None
    if backend == "turso":
        resolver = turso_target_resolver or _TursoTargetResolver()
        # Resolved for parity/validation with Task 7's seam even though the
        # control-plane itself stays local until Task 10/11.
        resolver.control_plane_target(os.environ)
        customer_memory_target_override = resolver.customer_memory_target(os.environ)
    return HostedMemoryService(
        catalog,
        keys,
        telemetry=catalog,
        rate_limiter=HostedInMemoryRateLimiter(),
        customer_memory_target_override=customer_memory_target_override,
    )


async def _handle(
    request: Request,
    payload: _RequestT,
    call: Callable[[str, _RequestT], Awaitable[_ResultT]],
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    return await _handle_payload(api_key, payload, call)


async def _handle_search(
    request: Request,
    body: dict[str, Any],
    service: HostedMemoryService,
    request_type: type[_SearchRequestT],
    call: Callable[[str, _SearchRequestT], Awaitable[_ResultT]],
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    try:
        if "scope" in body:
            payload = request_type.model_validate(body)
        else:
            auth = _authenticate_for_header_scope(service, api_key)
            search = _HeaderBoundSearchBody.model_validate(body)
            payload = request_type(
                scope=_read_scope_from_headers(request, auth),
                query=search.query,
                limit=search.limit,
            )
    except ValidationError:
        return _error_response(
            422,
            "invalid_request",
            "Request body does not match the Vexic contract.",
        )
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _error_response(400, "invalid_request", str(exc))
    except Exception:
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return await _handle_payload(api_key, payload, call)


def _authenticate_for_header_scope(
    service: HostedMemoryService,
    api_key: str,
) -> HostedAuthContext:
    authenticate = service.api_keys.authenticate
    return authenticate(api_key)


async def _handle_payload(
    api_key: str,
    payload: _RequestT,
    call: Callable[[str, _RequestT], Awaitable[_ResultT]],
) -> JSONResponse:
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


def _cap_error(payload: MemoryRequest) -> JSONResponse | None:
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
