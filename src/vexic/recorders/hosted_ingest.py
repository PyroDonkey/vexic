from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from vexic.contract import SourceTranscriptMessage
from vexic.redaction import assert_no_forbidden_secret_values_in_payload
from vexic.url_policy import require_http_url


@dataclass(frozen=True)
class HostedIngestConfig:
    base_url: str
    api_key: str
    project_id: str
    session_id: str
    agent_id: str | None
    timeout_seconds: float = 30.0


def post_source_messages(
    config: HostedIngestConfig,
    *,
    messages: list[SourceTranscriptMessage],
    forbidden_values: tuple[str, ...],
) -> dict[str, object]:
    messages_payload = [message.model_dump(mode="json") for message in messages]
    assert_no_forbidden_secret_values_in_payload(
        forbidden_values,
        {"messages": messages_payload},
    )
    payload = {
        "messages": messages_payload,
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

    base_url = require_http_url("base_url", config.base_url)
    request = Request(
        urljoin(base_url + "/", "v1/ingest_source_transcript"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = _error_body_detail(exc)
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(f"hosted ingest failed: HTTP {exc.code}{suffix}") from exc
    except URLError as exc:
        raise RuntimeError(f"hosted ingest failed: {type(exc.reason).__name__}") from exc


_ERROR_DETAIL_MAX_CHARS = 300
_ERROR_BODY_MAX_BYTES = 64 * 1024


def _error_body_detail(exc: HTTPError) -> str | None:
    """Extract detail from a Vexic hosted error body, else None.

    Only the server's structured error envelope is surfaced -- an HTML or
    unparseable body is dropped so the raised message never echoes arbitrary
    proxy output. The read is bounded so a huge body from a misbehaving proxy
    is never buffered whole; anything over the cap cannot be the hosted
    envelope and is dropped. Client-fault 4xx responses surface
    `code: message` because the message carries the actionable detail;
    5xx responses surface only the stable error code so server-side text
    never reaches the recorder's status output.
    """
    try:
        raw = exc.read(_ERROR_BODY_MAX_BYTES + 1)
        if len(raw) > _ERROR_BODY_MAX_BYTES:
            return None
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not code:
        return None
    if exc.code >= 500 or not isinstance(message, str) or not message:
        detail = code
    else:
        detail = f"{code}: {message}"
    return detail[:_ERROR_DETAIL_MAX_CHARS]
