from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from vexic import CONTRACT_VERSION
from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import HostedAuthContext, HostedMemoryService, HostedRateLimitExceeded
from vexic.mcp_presentation import (
    RECALL_CONVERSATION_HISTORY,
    RECALL_USER_MEMORY,
    render_long_term,
    render_transcript_hits,
    server_instructions,
)
from vexic.mcp_stdio import (
    BASE_TOOLS,
    MCP_PROTOCOL_VERSION,
    _limit,
    _query,
    _reject_extra,
    _tool_error,
    _tool_prose,
)
from vexic.ports import HostPortNotConfigured
from vexic.redaction import assert_no_forbidden_secret_values


class JsonRpcRequest(BaseModel):
    """Structural validation of an inbound JSON-RPC message envelope.

    Extra keys (``result``/``error`` on client response messages) are retained
    so the handler can distinguish requests, notifications, and responses
    without re-inspecting the raw dict.
    """

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"]
    method: str | None = None
    params: Any = None
    id: Any = None

    @property
    def is_notification(self) -> bool:
        return "id" not in self.model_fields_set

    @property
    def is_response_message(self) -> bool:
        extra = self.model_extra or {}
        return self.method is None and ("result" in extra or "error" in extra)


def register_mcp_routes(
    app: FastAPI,
    service: HostedMemoryService,
    *,
    forbidden_secret_values: tuple[str, ...] = (),
) -> None:
    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        if request.url.query:
            return _http_error(400, "invalid_request", "MCP requests must not include query strings.")
        origin_error = _origin_error(request)
        if origin_error is not None:
            return origin_error
        protocol_error = _protocol_error(request)
        if protocol_error is not None:
            return protocol_error
        api_key = _bearer_api_key(request)
        if api_key is None:
            return _http_error(401, "unauthorized", "Missing hosted API key.")
        try:
            auth = service.api_keys.authenticate(api_key)
        except PermissionError:
            return _http_error(401, "unauthorized", "Invalid hosted API key.")
        except Exception:
            return _http_error(500, "internal_error", "Hosted memory request failed.")

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(_jsonrpc_error(None, -32700, "parse error"))
        try:
            rpc = JsonRpcRequest.model_validate(body)
        except ValidationError:
            message_id = body.get("id") if isinstance(body, dict) else None
            return JSONResponse(_jsonrpc_error(message_id, -32600, "invalid request"))
        if rpc.is_response_message:
            return Response(status_code=202)
        if rpc.method is None:
            return JSONResponse(_jsonrpc_error(rpc.id, -32600, "invalid request"))
        if rpc.is_notification:
            return Response(status_code=202)
        try:
            response = await _handle_message(
                rpc,
                request,
                service,
                api_key,
                auth,
                forbidden_secret_values,
            )
        except HostedRateLimitExceeded as exc:
            return _http_error(
                429,
                "rate_limited",
                "Hosted rate limit exceeded.",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            )
        if response is None:
            return JSONResponse(_jsonrpc_error(rpc.id, -32601, "method not found"))
        return JSONResponse(response)


def _bearer_api_key(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is None:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def _origin_error(request: Request) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if origin is None:
        return None
    allowed = {
        value.strip()
        for value in os.environ.get("VEXIC_MCP_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    if origin in allowed:
        return None
    return _http_error(403, "origin_forbidden", "Origin is not allowed for MCP.")


def _protocol_error(request: Request) -> JSONResponse | None:
    protocol_version = request.headers.get("mcp-protocol-version")
    if protocol_version is None or protocol_version == MCP_PROTOCOL_VERSION:
        return None
    return _http_error(
        400,
        "unsupported_protocol_version",
        "Unsupported MCP protocol version.",
    )


async def _handle_message(
    rpc: JsonRpcRequest,
    request: Request,
    service: HostedMemoryService,
    api_key: str,
    auth: HostedAuthContext,
    forbidden_secret_values: tuple[str, ...],
) -> dict[str, Any] | None:
    if rpc.method == "tools/list":
        return _response(rpc.id, {"tools": list(BASE_TOOLS)})
    if rpc.method == "ping":
        return _response(rpc.id, {})
    if rpc.method == "tools/call":
        params = rpc.params or {}
        if not isinstance(params, dict):
            return _response(rpc.id, _tool_error("params must be an object."))
        return _response(
            rpc.id,
            await _call_tool(
                params,
                request,
                service,
                api_key,
                auth,
                forbidden_secret_values,
            ),
        )
    if rpc.method != "initialize":
        return None
    params = rpc.params or {}
    requested_version = params.get("protocolVersion") if isinstance(params, dict) else None
    return _response(
        rpc.id,
        {
            "protocolVersion": (
                requested_version if requested_version == MCP_PROTOCOL_VERSION else MCP_PROTOCOL_VERSION
            ),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "vexic-remote-memory",
                "title": "Vexic Remote Memory",
                "version": CONTRACT_VERSION,
            },
            "instructions": server_instructions(False),
        },
    )


def _response(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


async def _call_tool(
    params: dict[str, Any],
    request: Request,
    service: HostedMemoryService,
    api_key: str,
    auth: HostedAuthContext,
    forbidden_secret_values: tuple[str, ...],
) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _tool_error("arguments must be an object.")
    try:
        if name == RECALL_CONVERSATION_HISTORY:
            _reject_extra(arguments, {"query", "limit"})
            result = await service.search_transcript(
                api_key,
                SearchTranscriptRequest(
                    scope=_scope_from_headers(request, auth),
                    query=_query(arguments),
                    limit=_limit(arguments),
                ),
            )
            text = render_transcript_hits(result.hits)
            assert_no_forbidden_secret_values(forbidden_secret_values, text)
            return _tool_prose(text)
        if name == RECALL_USER_MEMORY:
            _reject_extra(arguments, {"query", "limit", "as_of"})
            result = await service.search_long_term(
                api_key,
                SearchLongTermRequest(
                    scope=_scope_from_headers(request, auth),
                    query=_query(arguments),
                    limit=_limit(arguments),
                    as_of=arguments.get("as_of"),
                ),
            )
            text = render_long_term(result.facts, result.candidate_notes)
            assert_no_forbidden_secret_values(forbidden_secret_values, text)
            return _tool_prose(text)
        return _tool_error(f"unknown tool: {name}")
    except HostedRateLimitExceeded:
        raise
    except (HostPortNotConfigured, PermissionError, ValueError) as exc:
        # Deliberate operator/caller-facing messages; safe to echo.
        return _tool_error(str(exc))
    except Exception:
        return _tool_error("Hosted memory request failed.")


def _scope_from_headers(request: Request, auth: HostedAuthContext) -> MemoryScope:
    session_id = request.headers.get("x-vexic-session-id")
    if session_id is None or not session_id.strip():
        raise ValueError("X-Vexic-Session-Id header is required.")
    return MemoryScope(
        tenant_id=auth.tenant_id,
        project_id=request.headers.get("x-vexic-project-id"),
        session_id=session_id,
        agent_id=request.headers.get("x-vexic-agent-id"),
        principal=auth.principal,
        trust_boundary=TrustBoundary.NETWORKED,
        capabilities={MemoryCapability.SEARCH},
    )


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _http_error(
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
