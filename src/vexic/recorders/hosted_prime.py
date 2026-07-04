from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from vexic.contract import PRIME_CONTEXT_HEADER
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.url_policy import require_http_url

LONG_TERM_PRIME_QUERY = "preference fact goal decision project context remember"
TRANSCRIPT_PRIME_QUERY = "remember"
DEFAULT_PRIME_MAX_CHARS = 6_000


@dataclass(frozen=True)
class HostedPrimeConfig:
    base_url: str
    api_key: str
    project_id: str
    session_id: str
    agent_id: str | None
    timeout_seconds: float = 15.0


def fetch_fresh_context(
    config: HostedPrimeConfig,
    *,
    token_budget: int,
) -> dict[str, object] | None:
    try:
        return _post_search(
            config,
            "fresh_context",
            {"token_budget": token_budget},
        )
    except RuntimeError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return None


def fetch_prime_context(
    config: HostedPrimeConfig,
    *,
    max_chars: int = DEFAULT_PRIME_MAX_CHARS,
    long_term_limit: int = 5,
    transcript_limit: int = 5,
) -> str:
    fresh_context = fetch_fresh_context(config, token_budget=max_chars // 4)
    recap_text = None
    if fresh_context is not None:
        recap_text = _str(fresh_context.get("text"))
    long_term = _safe_post_search(
        config,
        "search_long_term",
        {"query": LONG_TERM_PRIME_QUERY, "limit": long_term_limit},
    )
    transcript = _safe_post_search(
        config,
        "search_transcript",
        {"query": TRANSCRIPT_PRIME_QUERY, "limit": transcript_limit},
    )
    context = build_prime_context(
        long_term, transcript, recap_text=recap_text, max_chars=max_chars
    )
    try:
        assert_no_forbidden_secret_values((config.api_key,), context)
    except ValueError:
        raise RuntimeError("hosted prime failed: forbidden secret in response") from None
    return context


def _safe_post_search(
    config: HostedPrimeConfig,
    operation: str,
    payload: dict[str, object],
) -> dict[str, object]:
    try:
        return _post_search(config, operation, payload)
    except RuntimeError:
        return {}


def _post_search(
    config: HostedPrimeConfig,
    operation: str,
    payload: dict[str, object],
) -> dict[str, object]:
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
        urljoin(base_url + "/", f"v1/{operation}"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"hosted prime failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"hosted prime failed: {type(exc.reason).__name__}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("hosted prime failed: invalid response")
    return body


def build_prime_context(
    long_term: dict[str, object],
    transcript: dict[str, object],
    *,
    recap_text: str | None = None,
    max_chars: int,
) -> str:
    lines: list[str] = [PRIME_CONTEXT_HEADER]
    facts = _items(long_term.get("facts"))
    notes = _items(long_term.get("candidate_notes"))
    hits = _items(transcript.get("hits"))

    if recap_text:
        lines.append("Prior conversation recap:")
        lines.append(recap_text)

    if facts or notes:
        lines.append("Long-term memory:")
        for fact in facts:
            text = _str(fact.get("fact_text"))
            if text:
                lines.append(f"- {text}")
        for note in notes:
            text = _str(note.get("fact_text"))
            if text:
                lines.append(f"- tentative: {text}")

    if hits:
        lines.append("Recent transcript memory:")
        for hit in hits:
            body = _str(hit.get("body"))
            if body:
                lines.append(f"- {body}")

    if len(lines) == 1:
        return ""
    lines.append(
        "Use this memory silently, as if you simply remember it — don't mention "
        "memory systems or where facts came from unless asked. If vexic memory "
        "search tools are available, use them to look up more preferences, "
        "facts, and past conversation when relevant."
    )
    return _cap("\n".join(lines), max_chars)


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _cap(text: str, max_chars: int) -> str:
    if max_chars < 1:
        return ""
    if len(text) <= max_chars:
        return text
    suffix = "\n[truncated]"
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[: max_chars - len(suffix)].rstrip() + suffix
