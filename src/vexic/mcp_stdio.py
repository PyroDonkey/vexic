"""Local read-only MCP server over stdio.

Speaks JSON-RPC (MCP protocol framing, one JSON message per line) and
exposes the read-only recall tools -- ``recall_conversation_history``,
``recall_user_memory``, and optionally ``expand_history`` -- backed by a
``LocalMemoryService`` or a hosted HTTP client. Input sizes are clamped and
all egress passes the forbidden-secret-values guard.

Entry point: ``python -m vexic.mcp_stdio`` or ``scripts/vexic-mcp-stdio.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from vexic import CONTRACT_VERSION
from vexic.contract import (
    ExpandHistoryResult,
    ExpandHistoryRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    SearchLongTermResult,
    SearchLongTermRequest,
    SearchTranscriptResult,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.mcp_presentation import (
    EXPAND_HISTORY,
    EXPAND_HISTORY_DESCRIPTION,
    RECALL_CONVERSATION_HISTORY,
    RECALL_CONVERSATION_HISTORY_DESCRIPTION,
    RECALL_USER_MEMORY,
    RECALL_USER_MEMORY_DESCRIPTION,
    TOOL_ANNOTATIONS,
    render_long_term,
    render_transcript_hits,
    server_instructions,
)
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.service import LocalMemoryService

MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_QUERY_CHARS = 1_000
MAX_LIMIT = 20
MAX_EXPAND_HISTORY_MESSAGES = 100
MAX_EXPAND_HISTORY_CHARS = 20_000


class McpMemoryService(Protocol):
    """The subset of ``MemoryService`` the MCP server needs (read-only ops)."""

    async def search_transcript(
        self,
        request: SearchTranscriptRequest,
    ) -> SearchTranscriptResult: ...

    async def expand_history(
        self,
        request: ExpandHistoryRequest,
        *,
        max_rows: int | None = None,
    ) -> ExpandHistoryResult: ...

    async def search_long_term(
        self,
        request: SearchLongTermRequest,
    ) -> SearchLongTermResult: ...


@dataclass(frozen=True)
class McpServerConfig:
    """Server configuration: scope identity, backing store (local ``db_path``
    or hosted ``api_base_url``), redaction values, and tool toggles."""

    tenant_id: str
    db_path: str | None = None
    session_id: str = "default"
    project_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    principal_id: str = "vexic-local-mcp"
    forbidden_secret_values: tuple[str, ...] = ()
    enable_expand_history: bool = False
    api_base_url: str | None = None
    api_key_env: str = "VEXIC_API_KEY"
    service_factory: Callable[["McpServerConfig"], McpMemoryService] | None = None

    def service(self) -> McpMemoryService:
        if self.api_base_url is not None:
            if self.service_factory is None:
                raise ValueError("hosted API service factory is required.")
            return self.service_factory(self)
        if self.db_path is None:
            raise ValueError("db_path is required for local MCP.")
        return LocalMemoryService(
            db_path=self.db_path,
            tenant_id=self.tenant_id,
            forbidden_secret_values=self.forbidden_secret_values,
        )

    def scope(self) -> MemoryScope:
        capabilities = {MemoryCapability.SEARCH}
        if self.enable_expand_history:
            capabilities.add(MemoryCapability.EXPAND_HISTORY)
        return MemoryScope(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            user_id=self.user_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            principal=Principal(
                principal_id=self.principal_id,
                principal_type=PrincipalType.AGENT,
            ),
            trust_boundary=(
                TrustBoundary.NETWORKED
                if self.api_base_url is not None
                else TrustBoundary.LOCAL_TRUSTED
            ),
            capabilities=capabilities,
        )


BASE_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": RECALL_CONVERSATION_HISTORY,
        "title": "Recall Conversation History",
        "description": RECALL_CONVERSATION_HISTORY_DESCRIPTION,
        "annotations": TOOL_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": RECALL_USER_MEMORY,
        "title": "Recall User Memory",
        "description": RECALL_USER_MEMORY_DESCRIPTION,
        "annotations": TOOL_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
)

EXPAND_HISTORY_TOOL: dict[str, Any] = {
    "name": EXPAND_HISTORY,
    "title": "Expand History",
    "description": EXPAND_HISTORY_DESCRIPTION,
    "annotations": TOOL_ANNOTATIONS,
    "inputSchema": {
        "type": "object",
        "properties": {
            "first_message_id": {"type": "integer", "minimum": 1},
            "last_message_id": {"type": "integer", "minimum": 1},
        },
        "required": ["first_message_id", "last_message_id"],
        "additionalProperties": False,
    },
}


def _tools(config: McpServerConfig) -> tuple[dict[str, Any], ...]:
    if config.enable_expand_history:
        return (*BASE_TOOLS, EXPAND_HISTORY_TOOL)
    return BASE_TOOLS


def _response(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _tool_text(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
        "isError": is_error,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return _tool_text({"error": message}, is_error=True)


def _tool_prose(text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }


def _instructions(config: McpServerConfig) -> str:
    return server_instructions(config.enable_expand_history)


def _query(arguments: dict[str, Any]) -> str:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string.")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be {MAX_QUERY_CHARS} characters or fewer.")
    return query


def _reject_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra_args = set(arguments) - allowed
    if extra_args:
        raise ValueError(f"unexpected argument: {sorted(extra_args)[0]}")


def _limit(arguments: dict[str, Any]) -> int:
    limit = arguments.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer.")
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}.")
    return limit


def _message_id(arguments: dict[str, Any], name: str) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer.")
    if value < 1:
        raise ValueError(f"{name} must be positive.")
    return value


def _expand_range(arguments: dict[str, Any]) -> tuple[int, int]:
    extra_args = set(arguments) - {"first_message_id", "last_message_id"}
    if extra_args:
        raise ValueError(f"unexpected argument: {sorted(extra_args)[0]}")
    first_message_id = _message_id(arguments, "first_message_id")
    last_message_id = _message_id(arguments, "last_message_id")
    if first_message_id > last_message_id:
        raise ValueError(
            "first_message_id must be less than or equal to last_message_id."
        )
    return first_message_id, last_message_id


def _check_egress(config: McpServerConfig, values: list[str]) -> None:
    assert_no_forbidden_secret_values(config.forbidden_secret_values, *values)


async def _search_transcript(
    arguments: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "limit"})
    result = await config.service().search_transcript(
        SearchTranscriptRequest(
            scope=config.scope(),
            query=_query(arguments),
            limit=_limit(arguments),
        )
    )
    text = render_transcript_hits(
        result.hits,
        include_message_ids=config.enable_expand_history,
    )
    assert_no_forbidden_secret_values(config.forbidden_secret_values, text)
    return _tool_prose(text)


async def _expand_history(
    arguments: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any]:
    first_message_id, last_message_id = _expand_range(arguments)
    result = await config.service().expand_history(
        ExpandHistoryRequest(
            scope=config.scope(),
            first_message_id=first_message_id,
            last_message_id=last_message_id,
            redaction=RedactionContext(forbidden_values=config.forbidden_secret_values),
        ),
        max_rows=MAX_EXPAND_HISTORY_MESSAGES,
    )
    if result.truncated and not result.text:
        raise ValueError(
            f"expand_history ranges are capped at {MAX_EXPAND_HISTORY_MESSAGES} messages."
        )
    text = result.text
    truncated = result.truncated
    if len(text) > MAX_EXPAND_HISTORY_CHARS:
        text = text[:MAX_EXPAND_HISTORY_CHARS]
        truncated = True
    _check_egress(config, [text])
    return _tool_text(
        {
            "egress_kind": result.egress_kind.value,
            "text": text,
            "truncated": truncated,
        }
    )


async def _search_long_term(
    arguments: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "limit"})
    result = await config.service().search_long_term(
        SearchLongTermRequest(
            scope=config.scope(),
            query=_query(arguments),
            limit=_limit(arguments),
        )
    )
    text = render_long_term(result.facts, result.candidate_notes)
    assert_no_forbidden_secret_values(config.forbidden_secret_values, text)
    return _tool_prose(text)


async def _call_tool(
    params: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _tool_error("arguments must be an object.")

    try:
        if name == RECALL_CONVERSATION_HISTORY:
            return await _search_transcript(arguments, config)
        if name == RECALL_USER_MEMORY:
            return await _search_long_term(arguments, config)
        if name == EXPAND_HISTORY and config.enable_expand_history:
            return await _expand_history(arguments, config)
        return _tool_error(f"unknown tool: {name}")
    except Exception as exc:
        return _tool_error(str(exc))


async def handle_jsonrpc_message(
    message: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message; returns the response, or None for notifications."""
    message_id = message.get("id")
    method = message.get("method")
    is_notification = "id" not in message

    if not isinstance(method, str):
        return None if is_notification else _error(message_id, -32600, "invalid request")
    if is_notification:
        return None

    if method == "initialize":
        requested_version = (message.get("params") or {}).get("protocolVersion")
        return _response(
            message_id,
            {
                "protocolVersion": (
                    requested_version
                    if requested_version == MCP_PROTOCOL_VERSION
                    else MCP_PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "vexic-local-memory",
                    "title": "Vexic Local Memory",
                    "version": CONTRACT_VERSION,
                },
                "instructions": _instructions(config),
            },
        )
    if method == "ping":
        return _response(message_id, {})
    if method == "shutdown":
        return _response(message_id, {})
    if method == "tools/list":
        return _response(message_id, {"tools": list(_tools(config))})
    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _response(message_id, _tool_error("params must be an object."))
        return _response(message_id, await _call_tool(params, config))

    return _error(message_id, -32601, f"method not found: {method}")


def _write_message(stdout: TextIO, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


async def run_stdio(
    config: McpServerConfig,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> None:
    """Serve MCP over the given stdio streams until stdin closes."""
    service = config.service()
    init_schema = getattr(service, "init_schema", None)
    if callable(init_schema):
        init_schema()
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error(
                None, -32700, f"parse error at line {exc.lineno} column {exc.colno}"
            )
        else:
            if not isinstance(message, dict):
                response = _error(None, -32700, "parse error: message must be an object")
            else:
                try:
                    response = await handle_jsonrpc_message(message, config)
                except Exception as exc:
                    # Exception text can quote request payloads (for example
                    # pydantic validation errors embed input values); return
                    # the type only.
                    response = _error(
                        None, -32700, f"request failed: {type(exc).__name__}"
                    )
        if response is not None:
            _write_message(stdout, response)
        stderr.flush()


def _parse_args(
    argv: list[str] | None,
    *,
    service_factory: Callable[[McpServerConfig], McpMemoryService] | None = None,
) -> McpServerConfig:
    parser = argparse.ArgumentParser(description="Run the Vexic local stdio MCP server.")
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--db-path")
    transport.add_argument("--api-base-url")
    parser.add_argument("--api-key-env", default="VEXIC_API_KEY")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--session-id", default="default")
    parser.add_argument("--project-id")
    parser.add_argument("--user-id")
    parser.add_argument("--agent-id")
    parser.add_argument("--principal-id", default="vexic-local-mcp")
    parser.add_argument("--forbidden-value", action="append", default=[])
    parser.add_argument("--enable-expand-history", action="store_true")
    args = parser.parse_args(argv)
    return McpServerConfig(
        db_path=args.db_path,
        tenant_id=args.tenant_id,
        session_id=args.session_id,
        project_id=args.project_id,
        user_id=args.user_id,
        agent_id=args.agent_id,
        principal_id=args.principal_id,
        forbidden_secret_values=tuple(args.forbidden_value),
        enable_expand_history=args.enable_expand_history,
        api_base_url=args.api_base_url,
        api_key_env=args.api_key_env,
        service_factory=service_factory,
    )


def main(
    argv: list[str] | None = None,
    *,
    service_factory: Callable[[McpServerConfig], McpMemoryService] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """CLI entry point: parse args, build the config, serve stdio, return exit code."""
    raw_argv = sys.argv[1:] if argv is None else argv
    # Decode/encode stdio as UTF-8 regardless of the platform locale. On Windows
    # the default sys.std* streams use cp1252, which silently mojibakes
    # non-ASCII JSON-RPC payloads on read and can fail to encode non-ASCII
    # responses on write. Resolve at call time so the streams stay injectable
    # for tests.
    stdio_wrappers = []
    if stdin is None:
        stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="")
        stdio_wrappers.append(stdin)
    if stdout is None:
        stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="")
        stdio_wrappers.append(stdout)
    if stderr is None:
        stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", newline="")
        stdio_wrappers.append(stderr)
    try:
        asyncio.run(
            run_stdio(
                _parse_args(raw_argv, service_factory=service_factory),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        )
    finally:
        for stream in stdio_wrappers:
            stream.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
