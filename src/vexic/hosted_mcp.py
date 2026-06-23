from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import TypeVar

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
