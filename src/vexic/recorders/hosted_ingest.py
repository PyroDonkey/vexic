from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from vexic.contract import SourceTranscriptMessage
from vexic.redaction import assert_no_forbidden_secret_values_in_payload
from vexic.url_policy import require_http_url

# Bounded in-process retry of transient transport faults. A hosted 5xx, a
# non-HTTP connectivity fault, and a lost/garbled response (a POST that
# succeeded but whose reply could not be read or parsed) are all retried; each
# retry is a fresh POST of the same request, and the hosted source ledger
# dedupes any row that a retried-then-succeeded attempt double-delivers.
_TRANSPORT_RETRY_ATTEMPTS = 3
_TRANSPORT_RETRY_BACKOFF_SECONDS = 0.5

# The only 4xx codes treated as transient: 429 (rate limited) and 408 (request
# timeout). An explicit allowlist, not "all non-auth 4xx" — an unexpected new
# 4xx (e.g. 413) signals a config/batching bug rather than transience and must
# stay loud.
_RETRYABLE_STATUS_CODES = frozenset({408, 429})


def _backoff_delay(attempt: int) -> float:
    # Jittered so overlapping async Stop hooks do not retry in lockstep
    # against a struggling server; the multiplier form keeps the delay
    # attempt-proportional and never zero.
    return _TRANSPORT_RETRY_BACKOFF_SECONDS * attempt * random.uniform(0.5, 1.5)

# Read/parse-phase failures on a POST that already reached the server. The POST
# may have committed server-side, so re-POSTing is safe only because the ledger
# dedupes; that is the same ambiguity fail-open + dedupe is built to absorb.
_RESPONSE_TRANSPORT_ERRORS = (
    OSError,  # includes URLError, socket timeout, connection reset
    IncompleteRead,
    json.JSONDecodeError,
    UnicodeDecodeError,
)


class HostedIngestTransportError(RuntimeError):
    """Transient transport-layer ingest failure that is safe to fail open.

    Raised for a hosted 5xx (`HTTPError` with `code >= 500`), an allowlisted
    transient 4xx (`_RETRYABLE_STATUS_CODES`: 429 rate limit, 408 request
    timeout), a non-HTTP connectivity fault (a `URLError` that is not an
    `HTTPError`: DNS, timeout, connection refused), or a lost/garbled response
    (the POST reached the server but reading or JSON-parsing its reply
    failed). Every other 4xx keeps the plain `RuntimeError` so auth/config
    faults (rotated key, wrong project) stay loud and surface on sync installs
    instead of silently killing recording. Subclasses `RuntimeError` so
    existing callers that catch `RuntimeError` still handle it.
    """


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
    # HTTPError subclasses URLError, so it is caught first: a hosted 5xx or an
    # allowlisted transient 4xx (429/408) is a transient transport fault; every
    # other 4xx is a caller/auth fault. The transport class (5xx, 429/408,
    # non-HTTP URLError, and read/parse failures on a POST that reached the
    # server) is retried and, once exhausted, re-raised as
    # HostedIngestTransportError so the caller can fail open.
    for attempt in range(1, _TRANSPORT_RETRY_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code < 500 and exc.code not in _RETRYABLE_STATUS_CODES:
                detail = _error_body_detail(exc)
                suffix = f" ({detail})" if detail else ""
                raise RuntimeError(
                    f"hosted ingest failed: HTTP {exc.code}{suffix}"
                ) from exc
            if attempt < _TRANSPORT_RETRY_ATTEMPTS:
                time.sleep(_backoff_delay(attempt))
                continue
            # Only the finally-raised error reads its body; retried attempts
            # drop theirs so a per-attempt proxy body is never buffered.
            detail = _error_body_detail(exc)
            suffix = f" ({detail})" if detail else ""
            raise HostedIngestTransportError(
                f"hosted ingest failed: HTTP {exc.code}{suffix}"
            ) from exc
        except _RESPONSE_TRANSPORT_ERRORS as exc:
            if attempt < _TRANSPORT_RETRY_ATTEMPTS:
                time.sleep(_backoff_delay(attempt))
                continue
            raise HostedIngestTransportError(
                f"hosted ingest failed: {_transport_reason(exc)}"
            ) from exc
    # Unreachable: the loop either returns or raises on the final attempt.
    raise AssertionError("hosted ingest retry loop exited without returning")


def _transport_reason(exc: BaseException) -> str:
    """Sanitized reason name for a transport fault, never the response body.

    A `URLError` carries its underlying cause in `.reason`; everything else
    (read timeout, incomplete read, JSON/decode failure) is named by its own
    type so a garbled response body can never reach the recorder's output.
    """
    if isinstance(exc, URLError) and not isinstance(exc, HTTPError):
        return type(exc.reason).__name__
    return type(exc).__name__


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
