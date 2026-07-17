from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from vexic.contract import PRIME_CONTEXT_HEADER
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.url_policy import require_http_url

LONG_TERM_PRIME_QUERY = "preference fact goal decision project context remember"
TRANSCRIPT_PRIME_QUERY = "remember"
DEFAULT_PRIME_MAX_CHARS = 6_000

PRIME_FRAMING = (
    "Memory snapshot from prior sessions — use it silently.\n"
    "More facts and conversation history exist beyond this snapshot; the "
    "vexic recall tools reach them."
)
PRIME_FOOTER = (
    "Use this memory silently, as if you simply remember it — don't mention "
    "memory systems or where facts came from unless asked. If vexic memory "
    "search tools are available, use them to look up more preferences, "
    "facts, and past conversation when relevant."
)

PRIME_ITEM_CAP = 400
PRIME_RECAP_CAP = 500


def _cap_item(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "…"


@dataclass(frozen=True)
class HostedPrimeConfig:
    base_url: str
    api_key: str
    project_id: str
    session_id: str
    agent_id: str | None
    timeout_seconds: float = 15.0


DREAM_TRIGGER_TIMEOUT_SECONDS = 5.0


def post_trigger_dream_phase(config: HostedPrimeConfig) -> dict[str, object]:
    """POST ``/v1/trigger_dream_phase`` with a hard 5s timeout.

    Used by the recorder's detached ``trigger-dream`` subcommand. Callers are
    responsible for fail-open behavior (this raises RuntimeError on any
    transport/HTTP failure, same as the other hosted prime calls).
    """
    trigger_config = config
    if config.timeout_seconds != DREAM_TRIGGER_TIMEOUT_SECONDS:
        trigger_config = HostedPrimeConfig(
            base_url=config.base_url,
            api_key=config.api_key,
            project_id=config.project_id,
            session_id=config.session_id,
            agent_id=config.agent_id,
            timeout_seconds=DREAM_TRIGGER_TIMEOUT_SECONDS,
        )
    return _post_search(trigger_config, "trigger_dream_phase", {"phase": "summarize"})


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
    # The recap is one of several prime sections (long-term facts, transcript
    # hits, and the trailing footer also need room). Cap the token_budget we
    # request AND the char budget we accept for the recap to ~1/4 of
    # max_chars, so a hosted endpoint that ignores token_budget (or whose
    # token estimate undershoots ours) can't crowd out the other sections.
    fresh_context = fetch_fresh_context(config, token_budget=max_chars // 16)
    recap_text = None
    if fresh_context is not None:
        recap_text = _str(fresh_context.get("text"))
        if recap_text is not None:
            recap_text = _cap_item(recap_text, max_chars // 4)
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
    except (OSError, HTTPException, ValueError) as exc:
        # urllib wraps connect-phase failures in URLError, but read-phase
        # failures escape raw: response.read() raises bare TimeoutError /
        # ssl.SSLError / IncompleteRead, and json/decode raise ValueError
        # subclasses. Downstream degradation filters on RuntimeError only,
        # so anything else here would discard the entire prime.
        raise RuntimeError(f"hosted prime failed: {type(exc).__name__}") from exc
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
    lines: list[str] = [PRIME_CONTEXT_HEADER, PRIME_FRAMING]
    facts = _items(long_term.get("facts"))
    notes = _items(long_term.get("candidate_notes"))
    hits = _items(transcript.get("hits"))

    if recap_text:
        lines.append("Prior conversation recap:")
        lines.append(_cap_item(recap_text, PRIME_RECAP_CAP))

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
                lines.append(f"- {_cap_item(body, PRIME_ITEM_CAP)}")

    if len(lines) == 2:
        return ""
    content = "\n".join(lines)
    footer_block = "\n" + PRIME_FOOTER
    if max_chars >= 2 * len(footer_block):
        # Reserve footer space only when the budget can hold the footer plus
        # at least an equal share of content. Below that threshold the
        # legacy end-cap below deliberately prioritizes memory content over
        # the footer.
        return _cap(content, max_chars - len(footer_block)) + footer_block
    return _cap(content + footer_block, max_chars)


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
