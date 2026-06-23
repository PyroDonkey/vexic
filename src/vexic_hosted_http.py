from __future__ import annotations

import argparse
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from vexic import CONTRACT_VERSION
from vexic.contract import (
    AppendTranscriptRequest,
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
)
from vexic.ports import HostPortNotConfigured
from vexic_hosted_local import HostedApiKeyStore, HostedTenantCatalog


MAX_BODY_BYTES = 1_000_000
MAX_APPEND_MESSAGES = 100
MAX_APPEND_CHARS = 250_000
MAX_QUERY_CHARS = 1_000
MAX_LIMIT = 20
MAX_EXPAND_HISTORY_MESSAGES = 100
MAX_EXPAND_HISTORY_CHARS = 20_000

_RequestT = TypeVar("_RequestT", bound=MemoryRequest)
_ResultT = TypeVar("_ResultT", bound=MemoryResult)


def create_app(service: HostedMemoryService | None = None) -> FastAPI:
    service = service or create_service_from_env()
    app = FastAPI(title="Vexic Hosted Memory", version=CONTRACT_VERSION)

    @app.middleware("http")
    async def cap_body(request: Request, call_next: Callable[[Request], Awaitable[object]]):
        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if content_length is not None and int(content_length) > MAX_BODY_BYTES:
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
        return await _handle(request, payload, service.expand_history)

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
        if "API key" in str(exc):
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
        # ponytail: ID span cap is the cheap alpha guard; adapter-level max_rows is the upgrade.
        if payload.last_message_id - payload.first_message_id + 1 > MAX_EXPAND_HISTORY_MESSAGES:
            return _too_large("expand_history range is capped.")
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

    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
