from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from vexic import CONTRACT_VERSION
from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.service import LocalMemoryService

MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_QUERY_CHARS = 1_000
MAX_LIMIT = 20


@dataclass(frozen=True)
class McpServerConfig:
    db_path: str
    tenant_id: str
    session_id: str = "default"
    project_id: str | None = None
    user_id: str | None = None
    principal_id: str = "vexic-local-mcp"
    forbidden_secret_values: tuple[str, ...] = ()

    def service(self) -> LocalMemoryService:
        return LocalMemoryService(
            db_path=self.db_path,
            tenant_id=self.tenant_id,
            forbidden_secret_values=self.forbidden_secret_values,
        )

    def scope(self) -> MemoryScope:
        return MemoryScope(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            user_id=self.user_id,
            session_id=self.session_id,
            principal=Principal(
                principal_id=self.principal_id,
                principal_type=PrincipalType.AGENT,
            ),
            trust_boundary=TrustBoundary.LOCAL_TRUSTED,
            capabilities={MemoryCapability.SEARCH},
        )


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_transcript",
        "title": "Search Transcript",
        "description": (
            "Read-only search over the configured session transcript. "
            "Does not expose verbatim history expansion or write memory."
        ),
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
        "name": "search_long_term",
        "title": "Search Long-Term Memory",
        "description": (
            "Read-only search over durable long-term facts, with tentative "
            "candidate notes only when no durable facts match."
        ),
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


def _query(arguments: dict[str, Any]) -> str:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string.")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be {MAX_QUERY_CHARS} characters or fewer.")
    return query


def _limit(arguments: dict[str, Any]) -> int:
    limit = arguments.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer.")
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}.")
    return limit


def _check_egress(config: McpServerConfig, values: list[str]) -> None:
    assert_no_forbidden_secret_values(config.forbidden_secret_values, *values)


async def _search_transcript(
    arguments: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any]:
    result = await config.service().search_transcript(
        SearchTranscriptRequest(
            scope=config.scope(),
            query=_query(arguments),
            limit=_limit(arguments),
        )
    )
    _check_egress(config, [hit.body for hit in result.hits])
    return _tool_text({"hits": [hit.model_dump(mode="json") for hit in result.hits]})


async def _search_long_term(
    arguments: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any]:
    result = await config.service().search_long_term(
        SearchLongTermRequest(
            scope=config.scope(),
            query=_query(arguments),
            limit=_limit(arguments),
        )
    )
    _check_egress(
        config,
        [fact.fact_text for fact in result.facts]
        + [note.fact_text for note in result.candidate_notes],
    )
    return _tool_text(
        {
            "facts": [fact.model_dump(mode="json") for fact in result.facts],
            "candidate_notes": [
                note.model_dump(mode="json") for note in result.candidate_notes
            ],
        }
    )


async def _call_tool(
    params: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _tool_error("arguments must be an object.")

    try:
        if name == "search_transcript":
            return await _search_transcript(arguments, config)
        if name == "search_long_term":
            return await _search_long_term(arguments, config)
        return _tool_error(f"unknown tool: {name}")
    except Exception as exc:
        return _tool_error(str(exc))


async def handle_jsonrpc_message(
    message: dict[str, Any],
    config: McpServerConfig,
) -> dict[str, Any] | None:
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
                "protocolVersion": requested_version or MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "vexic-local-memory",
                    "title": "Vexic Local Memory",
                    "version": CONTRACT_VERSION,
                },
                "instructions": (
                    "Read-only Vexic memory. Use search_transcript for the "
                    "configured session and search_long_term for durable facts. "
                    "No transcript append, verbatim history expansion, export, "
                    "delete, rebuild, or admin tools are available."
                ),
            },
        )
    if method == "ping":
        return _response(message_id, {})
    if method == "shutdown":
        return _response(message_id, {})
    if method == "tools/list":
        return _response(message_id, {"tools": list(TOOLS)})
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
    config.service().init_schema()
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            response = await handle_jsonrpc_message(message, config)
        except Exception as exc:
            response = _error(None, -32700, f"parse error: {exc}")
        if response is not None:
            _write_message(stdout, response)
        stderr.flush()


def _parse_args(argv: list[str] | None) -> McpServerConfig:
    parser = argparse.ArgumentParser(description="Run the Vexic local stdio MCP server.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--session-id", default="default")
    parser.add_argument("--project-id")
    parser.add_argument("--user-id")
    parser.add_argument("--principal-id", default="vexic-local-mcp")
    parser.add_argument("--forbidden-value", action="append", default=[])
    args = parser.parse_args(argv)
    return McpServerConfig(
        db_path=args.db_path,
        tenant_id=args.tenant_id,
        session_id=args.session_id,
        project_id=args.project_id,
        user_id=args.user_id,
        principal_id=args.principal_id,
        forbidden_secret_values=tuple(args.forbidden_value),
    )


def main(argv: list[str] | None = None) -> int:
    asyncio.run(run_stdio(_parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
