from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from vexic import CONTRACT_VERSION
from vexic.contract import (
    AppendTranscriptRequest,
    DreamPhase,
    ExpandHistoryResult,
    ExpandHistoryRequest,
    MemoryCapability,
    MemoryRequest,
    MemoryResult,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    RunDreamPhaseRequest,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import (
    HostedBackgroundJobRunner,
    HostedInMemoryRateLimiter,
    HostedMemoryService,
    HostedRateLimitExceeded,
)
from vexic.mcp_http import register_mcp_routes
from vexic.ports import DreamPhasePorts, HostPortNotConfigured
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


def _api_key_from_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required in the environment.")
    return value


def _secret_env_values(names: list[str]) -> dict[str, str] | None:
    secrets: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name)
        if value is None:
            raise ValueError(f"{name} is required in the environment.")
        secrets[name] = value
    return secrets or None


def _load_dream_phase_adapter(path: Path) -> ModuleType:
    if not path.exists():
        raise ValueError(f"dream phase adapter not found: {path}")
    module_name = f"vexic_hosted_adapter_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"could not load dream phase adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for name in (
        "embed_texts",
        "build_extraction_agent",
        "build_rem_agent",
        "build_contradiction_agent",
    ):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"dream phase adapter must define callable {name}.")
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


def _run_dream_phase_command(args: argparse.Namespace) -> int:
    root = _root_arg(args.root)
    catalog = HostedTenantCatalog(root)
    service = HostedMemoryService(
        catalog,
        HostedApiKeyStore(root),
        telemetry=catalog,
        rate_limiter=HostedInMemoryRateLimiter(),
        dream_phase_ports=_dream_phase_ports(args),
    )
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(
            HostedBackgroundJobRunner(service).run_dream_phase(
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
                "job_events": [
                    _event_dict(event) for event in catalog.job_events(args.tenant_id)
                ],
                "usage_events": [
                    _event_dict(event) for event in catalog.usage_events(args.tenant_id)
                ],
            },
            sort_keys=True,
        )
    )
    return 0


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

    args = parser.parse_args(argv)

    try:
        if args.command == "run-dream-phase":
            return _run_dream_phase_command(args)

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
