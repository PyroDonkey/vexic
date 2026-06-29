from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from vexic.contract import SourceTranscriptMessage


@dataclass(frozen=True)
class HostedIngestConfig:
    base_url: str
    api_key: str
    project_id: str
    session_id: str
    agent_id: str | None
    timeout_seconds: float = 10.0


def post_source_messages(
    config: HostedIngestConfig,
    *,
    messages: list[SourceTranscriptMessage],
    forbidden_values: tuple[str, ...],
) -> dict[str, object]:
    payload = {
        "messages": [message.model_dump(mode="json") for message in messages],
        "redaction": {"forbidden_values": list(forbidden_values)},
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "X-Vexic-Project-Id": config.project_id,
        "X-Vexic-Session-Id": config.session_id,
    }
    if config.agent_id is not None:
        headers["X-Vexic-Agent-Id"] = config.agent_id

    request = Request(
        urljoin(config.base_url.rstrip("/") + "/", "v1/ingest_source_transcript"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"hosted ingest failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"hosted ingest failed: {type(exc.reason).__name__}") from exc
