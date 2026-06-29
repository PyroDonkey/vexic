from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TextIO, TypeVar

from vexic.contract import (
    ExpandHistoryResult,
    ExpandHistoryRequest,
    SearchLongTermResult,
    SearchLongTermRequest,
    SearchTranscriptResult,
    SearchTranscriptRequest,
)
from vexic.mcp_stdio import McpServerConfig

_ResultT = TypeVar("_ResultT")


def _required_config_string(config: dict[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"recorder config {name} must be a non-empty string.")
    return value.strip()


def run_recorder_config_proxy(
    path: Path,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    raw_config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_config, dict):
        raise ValueError("recorder config must be a JSON object.")
    base_url = _required_config_string(raw_config, "base_url").rstrip("/")
    api_key = _required_config_string(raw_config, "api_key")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-Vexic-Project-Id": _required_config_string(raw_config, "project_id"),
        "X-Vexic-Session-Id": _required_config_string(raw_config, "session_id"),
    }
    agent_id = raw_config.get("agent_id")
    if isinstance(agent_id, str) and agent_id.strip():
        headers["X-Vexic-Agent-Id"] = agent_id.strip()

    for line in stdin:
        if not line.strip():
            continue
        request = urllib.request.Request(
            f"{base_url}/mcp",
            data=line.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status == 202:
                continue
            stdout.write(response.read().decode("utf-8") + "\n")
            stdout.flush()
        stderr.flush()
    return 0


def create_hosted_http_memory_service(
    config: McpServerConfig,
) -> HostedHttpMemoryServiceClient:
    if config.api_base_url is None:
        raise ValueError("api_base_url is required for hosted MCP.")
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise ValueError(f"{config.api_key_env} environment variable is required.")
    return HostedHttpMemoryServiceClient(config.api_base_url, api_key)


class HostedHttpMemoryServiceClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def search_transcript(
        self,
        request: SearchTranscriptRequest,
    ) -> SearchTranscriptResult:
        return await self._post("search_transcript", request, SearchTranscriptResult)

    async def expand_history(
        self,
        request: ExpandHistoryRequest,
        *,
        max_rows: int | None = None,
    ) -> ExpandHistoryResult:
        return await self._post("expand_history", request, ExpandHistoryResult)

    async def search_long_term(
        self,
        request: SearchLongTermRequest,
    ) -> SearchLongTermResult:
        return await self._post("search_long_term", request, SearchLongTermResult)

    async def _post(
        self,
        operation: str,
        payload: object,
        result_type: type[_ResultT],
    ) -> _ResultT:
        body = json.dumps(payload.model_dump(mode="json")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/{operation}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def send() -> bytes:
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                raise RuntimeError(_hosted_http_error(exc)) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Hosted Vexic API request failed: {exc.reason}") from exc

        raw = await asyncio.to_thread(send)
        return result_type.model_validate_json(raw)


def _hosted_http_error(exc: urllib.error.HTTPError) -> str:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return f"Hosted Vexic API returned HTTP {exc.code}."
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    detail = payload.get("detail")
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return f"Hosted Vexic API returned HTTP {exc.code}."
