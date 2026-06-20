from __future__ import annotations

from typing import Any

from vexic.formatting import UNVERIFIED_NOTES_PREAMBLE, format_candidate_note
from vexic.ports import HostPortNotConfigured
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import (
    LongTermFact,
    TranscriptHit,
    TranscriptRangeTooLarge,
    load_messages_in_id_range,
    search_messages,
)
from vexic.subagents.retrieval import (
    retrieve_candidate_fallback,
    retrieve_long_term_facts,
)

EXPAND_HISTORY_RANGE_TOKEN_CAP = 2_000
EXPAND_HISTORY_MAX_RANGE_WIDTH = EXPAND_HISTORY_RANGE_TOKEN_CAP
EXPAND_HISTORY_RETURN_CHAR_CAP = 8_000
EXPAND_HISTORY_TRUNCATION_MARKER = "\n\n...[truncated]"


def _deps(ctx_or_deps: Any) -> Any:
    return getattr(ctx_or_deps, "deps", ctx_or_deps)


def _usage(ctx_or_deps: Any) -> Any:
    return getattr(ctx_or_deps, "usage", None)


def _format_hit(hit: TranscriptHit) -> str:
    if hit.timestamp is None:
        return f"[message {hit.message_id}] {hit.body}"
    return f"[message {hit.message_id} @ {hit.timestamp}] {hit.body}"


def _estimated_tokens_for_length(char_count: int) -> int:
    return (char_count + 3) // 4


def _too_large_message(estimate: int) -> str:
    return (
        f"Requested message range is too large "
        f"({estimate} estimated tokens; cap {EXPAND_HISTORY_RANGE_TOKEN_CAP}). "
        "Please narrow the message id range and try again."
    )


def _too_many_rows_message(row_count: int, max_rows: int) -> str:
    return (
        f"Requested message range is too large "
        f"({row_count} rows; cap {max_rows}). "
        "Please narrow the message id range and try again."
    )


def _format_history_hits_within_token_cap(
    hits: list[TranscriptHit],
) -> str | None:
    parts: list[str] = []
    char_count = 0
    for hit in hits:
        formatted = _format_hit(hit)
        piece = formatted if not parts else f"\n---\n{formatted}"
        next_char_count = char_count + len(piece)
        if (
            _estimated_tokens_for_length(next_char_count)
            > EXPAND_HISTORY_RANGE_TOKEN_CAP
        ):
            return None
        parts.append(piece)
        char_count = next_char_count
    return "".join(parts)


def _truncate_expand_history(text: str) -> str:
    if len(text) <= EXPAND_HISTORY_RETURN_CHAR_CAP:
        return text
    return text[:EXPAND_HISTORY_RETURN_CHAR_CAP] + EXPAND_HISTORY_TRUNCATION_MARKER


def search_memory(ctx_or_deps: Any, query: str) -> str:
    deps = _deps(ctx_or_deps)
    results = search_messages(deps.db_path, query, session_id=deps.session_id)

    if not results:
        return "No relevant memories found."

    rendered = "\n---\n".join(_format_hit(hit) for hit in results)
    assert_no_forbidden_secret_values(deps.secrets.values(), rendered)
    return rendered


def expand_history(
    ctx_or_deps: Any,
    first_message_id: int,
    last_message_id: int,
) -> str:
    deps = _deps(ctx_or_deps)
    if first_message_id < 1 or last_message_id < 1:
        return "message ids must be positive integers."
    if first_message_id > last_message_id:
        return "first_message_id must be less than or equal to last_message_id."
    range_width = last_message_id - first_message_id + 1
    if range_width > EXPAND_HISTORY_MAX_RANGE_WIDTH:
        return (
            f"Requested message range is too large "
            f"({range_width} message ids; cap {EXPAND_HISTORY_MAX_RANGE_WIDTH}). "
            "Please narrow the message id range and try again."
        )

    try:
        hits = load_messages_in_id_range(
            deps.db_path,
            first_message_id,
            last_message_id,
            session_id=deps.session_id,
            max_rows=EXPAND_HISTORY_MAX_RANGE_WIDTH,
        )
    except TranscriptRangeTooLarge as exc:
        return _too_many_rows_message(exc.row_count, exc.max_rows)
    if not hits:
        return "No messages found in that range for the current session."

    rendered = _format_history_hits_within_token_cap(hits)
    if rendered is None:
        return _too_large_message(EXPAND_HISTORY_RANGE_TOKEN_CAP + 1)
    try:
        assert_no_forbidden_secret_values(deps.secrets.values(), rendered)
    except ValueError:
        return (
            "expand_history refused to return that range because it contains a "
            "loaded secret value. Narrow the range or ask the operator to inspect it."
        )
    final_text = _truncate_expand_history(rendered)
    try:
        assert_no_forbidden_secret_values(deps.secrets.values(), final_text)
    except ValueError:
        return (
            "expand_history refused to return that range because it contains a "
            "loaded secret value. Narrow the range or ask the operator to inspect it."
        )
    return final_text


def _format_fact(fact: LongTermFact) -> str:
    sources = ", ".join(str(message_id) for message_id in fact.source_message_ids)
    return (
        f"[fact {fact.fact_id}] {fact.fact_text}\n"
        f"(category: {fact.category}, confidence: {fact.confidence:.2f}, "
        f"source messages: {sources})"
    )


async def search_long_term(ctx_or_deps: Any, query: str) -> str:
    deps = _deps(ctx_or_deps)
    authority = getattr(deps, "authority", None)
    try:
        facts = await retrieve_long_term_facts(
            deps.db_path,
            query,
            session_id=deps.session_id,
            model_group=authority.model_group if authority is not None else None,
            secrets=deps.secrets,
            usage=_usage(ctx_or_deps),
            sink=deps.retrieved_facts_this_turn,
            embed=getattr(deps, "embed", None),
        )

        if facts:
            rendered = "\n---\n".join(_format_fact(fact) for fact in facts)
            assert_no_forbidden_secret_values(deps.secrets.values(), rendered)
            return rendered

        notes = await retrieve_candidate_fallback(
            deps.db_path,
            query,
            session_id=deps.session_id,
            secrets=deps.secrets,
            embed=getattr(deps, "embed", None),
        )
    except HostPortNotConfigured as exc:
        return str(exc)

    if not notes:
        return "No long-term memories found."

    rendered = (
        f"{UNVERIFIED_NOTES_PREAMBLE}\n\n"
        + "\n---\n".join(format_candidate_note(note) for note in notes)
    )
    assert_no_forbidden_secret_values(deps.secrets.values(), rendered)
    return rendered
