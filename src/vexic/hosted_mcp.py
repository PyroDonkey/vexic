from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TextIO, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from vexic.contract import (
    ExpandHistoryResult,
    ExpandHistoryRequest,
    SearchLongTermResult,
    SearchLongTermRequest,
    SearchTranscriptResult,
    SearchTranscriptRequest,
)
from vexic.mcp_stdio import McpServerConfig
from vexic.url_policy import require_http_url

_ResultT = TypeVar("_ResultT")


class _RecorderProxyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str
    project_id: str
    session_id: str
    agent_id: str | None = None
    status_path: Path | None = None

    @field_validator("base_url", "api_key", "project_id", "session_id", mode="before")
    @classmethod
    def _required_string(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def _base_url_is_http(cls, value: str) -> str:
        return require_http_url("base_url", value)

    @field_validator("agent_id", mode="before")
    @classmethod
    def _optional_string(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value.strip() or None


def _load_recorder_proxy_config(path: Path) -> _RecorderProxyConfig:
    try:
        return _RecorderProxyConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ValueError(f"invalid recorder config: {exc}") from exc


def _jsonrpc_id(line: str) -> Any:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload.get("id") if isinstance(payload, dict) else None


def _write_jsonrpc_error(stdout: TextIO, message_id: Any, message: str) -> None:
    stdout.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32000, "message": message},
            },
            sort_keys=True,
        )
        + "\n"
    )
    stdout.flush()


def _forward_http_error(
    exc: urllib.error.HTTPError,
    *,
    stdout: TextIO,
    message_id: Any,
) -> None:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        _write_jsonrpc_error(stdout, message_id, f"Hosted MCP returned HTTP {exc.code}.")
        return
    stdout.write(raw + ("\n" if not raw.endswith("\n") else ""))
    stdout.flush()


def run_recorder_config_proxy(
    path: Path,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    path = Path(os.path.expandvars(str(path))).expanduser()
    config = _load_recorder_proxy_config(path)
    base_url = config.base_url
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-Vexic-Project-Id": config.project_id,
        "X-Vexic-Session-Id": config.session_id,
    }
    if config.agent_id is not None:
        headers["X-Vexic-Agent-Id"] = config.agent_id

    for line in stdin:
        if not line.strip():
            continue
        message_id = _jsonrpc_id(line)
        request = urllib.request.Request(
            f"{base_url}/mcp",
            data=line.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 202:
                    continue
                stdout.write(response.read().decode("utf-8") + "\n")
                stdout.flush()
        except urllib.error.HTTPError as exc:
            _forward_http_error(exc, stdout=stdout, message_id=message_id)
        except (TimeoutError, urllib.error.URLError):
            _write_jsonrpc_error(stdout, message_id, "Hosted MCP upstream request failed.")
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
        self.base_url = require_http_url("api_base_url", base_url)
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
