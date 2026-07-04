from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic_ai.messages import ModelMessage

from vexic.ports import ContentCodec
from vexic.storage.transcript import (
    TranscriptHit,
    _decode_stored,
    _encode_stored,
    _message_token_estimate,
    _trim_unpaired_tool_messages,
    load_messages_in_id_range,
    message_search_text,
    single_message_adapter,
    strip_prompt_payloads,
)
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.text_utils import estimate_tokens
from vexic.text_utils import TAU_SOFT
from vexic.usage import UsageSummary
from vexic.storage.connection import connect

SessionSummaryKind = Literal["leaf", "condensed"]


@dataclass(frozen=True)
class SessionSummary:
    id: int
    session_id: str
    kind: SessionSummaryKind
    first_message_id: int
    last_message_id: int
    summary_text: str
    token_estimate: int
    replaces_summary_ids: tuple[int, ...]
    created_at: str = ""
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_micros: int = 0


@dataclass(frozen=True)
class _ActiveContextRow:
    id: int
    timestamp: datetime | None
    message: ModelMessage


def _parse_replaces_summary_ids(raw: str) -> tuple[int, ...]:
    values = json.loads(raw)
    if not isinstance(values, list):
        return ()
    return tuple(int(value) for value in values)


def _summary_from_row(
    row: Sequence[object],
    content_codec: ContentCodec | None = None,
) -> SessionSummary:
    return SessionSummary(
        id=int(row[0]),
        session_id=str(row[1]),
        kind=str(row[2]),  # type: ignore[arg-type]
        first_message_id=int(row[3]),
        last_message_id=int(row[4]),
        summary_text=_decode_stored(content_codec, str(row[5])),
        token_estimate=int(row[6]),
        replaces_summary_ids=_parse_replaces_summary_ids(str(row[7])),
        model_requests=int(row[8]),
        input_tokens=int(row[9]),
        output_tokens=int(row[10]),
        total_tokens=int(row[11]),
        estimated_cost_micros=int(row[12]),
        created_at=str(row[13]) if len(row) > 13 and row[13] is not None else "",
    )


def record_session_summary(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
    kind: SessionSummaryKind,
    first_message_id: int,
    last_message_id: int,
    summary_text: str,
    replaces_summary_ids: list[int] | tuple[int, ...] = (),
    usage: UsageSummary = UsageSummary(),
    forbidden_secret_values: tuple[str, ...] = (),
    content_codec: ContentCodec | None = None,
    created_at: str | None = None,
) -> int:
    assert_no_forbidden_secret_values(forbidden_secret_values, summary_text)
    token_estimate = estimate_tokens(summary_text)
    stored_summary_text = _encode_stored(content_codec, summary_text)
    replaces_json = json.dumps(list(replaces_summary_ids))
    columns = (
        "session_id, agent_id, kind, first_message_id, last_message_id, "
        "summary_text, token_estimate, replaces_summary_ids, "
        "model_requests, input_tokens, output_tokens, total_tokens, "
        "estimated_cost_micros"
    )
    values: tuple[object, ...] = (
        session_id,
        agent_id,
        kind,
        first_message_id,
        last_message_id,
        stored_summary_text,
        token_estimate,
        replaces_json,
        usage.model_requests,
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        usage.estimated_cost_micros,
    )
    if created_at is not None:
        columns = f"{columns}, created_at"
        values = (*values, created_at)
    placeholders = ", ".join("?" for _ in values)
    with closing(connect(db_path)) as conn:
        with conn:
            cursor = conn.execute(
                f"""
                INSERT INTO session_summaries
                    ({columns})
                VALUES ({placeholders})
                """,
                values,
            )
            return int(cursor.lastrowid)


def count_session_summaries_since(
    db_path: str,
    *,
    agent_id: str | None = None,
    created_at_floor: str,
) -> int:
    """Count `session_summaries` rows for `agent_id` created on/after
    `created_at_floor` (a ``"YYYY-MM-DD HH:MM:SS"`` string in the same format
    SQLite's ``CURRENT_TIMESTAMP`` emits -- a plain TEXT-affinity comparison,
    so callers must pass floors in this same space-separated shape or legacy
    rows will silently sort incorrectly. Scope is tenant(+agent)-wide across
    all sessions, matching the daily span budget's honest scope (no
    project_id column on this table).
    """
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM session_summaries
            WHERE agent_id IS ?
                AND created_at >= ?
            """,
            (agent_id, created_at_floor),
        ).fetchone()
    return int(row[0]) if row is not None else 0


def fetch_session_summary_frontier(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
    content_codec: ContentCodec | None = None,
) -> list[SessionSummary]:
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, kind, first_message_id, last_message_id,
                   summary_text, token_estimate, replaces_summary_ids,
                   model_requests, input_tokens, output_tokens, total_tokens,
                   estimated_cost_micros, created_at
            FROM session_summaries
            WHERE session_id = ?
                AND agent_id IS ?
            ORDER BY first_message_id ASC, last_message_id ASC, id ASC
            """,
            (session_id, agent_id),
        ).fetchall()

    summaries = [_summary_from_row(row, content_codec) for row in rows]
    replaced = {
        summary_id
        for summary in summaries
        for summary_id in summary.replaces_summary_ids
    }
    return [summary for summary in summaries if summary.id not in replaced]


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _message_rows(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
) -> list[tuple[int, datetime | None]]:
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp
            FROM messages
            WHERE session_id = ?
                AND agent_id IS ?
            ORDER BY id ASC
            """,
            (session_id, agent_id),
        ).fetchall()
    return [(int(row[0]), _parse_timestamp(row[1])) for row in rows]


def _first_message_id(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
) -> int | None:
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM messages
            WHERE session_id = ?
                AND agent_id IS ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (session_id, agent_id),
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _boundary_timezone(timezone_name: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc


def _latest_boundary_message_id(
    rows: list[tuple[int, datetime | None]],
    *,
    timezone_name: str,
    now_utc: datetime | None,
) -> int | None:
    if not rows:
        return None

    boundary_id: int | None = None
    previous_id: int | None = None
    previous_ts: datetime | None = None
    for message_id, timestamp in rows:
        if previous_id is not None and previous_ts is not None and timestamp is not None:
            if (timestamp - previous_ts).total_seconds() > 2 * 60 * 60:
                boundary_id = previous_id
        previous_id = message_id
        previous_ts = timestamp

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(_boundary_timezone(timezone_name))
    local_three = datetime.combine(
        local_now.date(),
        time(hour=3),
        tzinfo=local_now.tzinfo,
    )
    if local_now < local_three:
        local_three = local_three - timedelta(days=1)
    cutoff_utc = local_three.astimezone(timezone.utc)
    pre_cutoff_ids = [
        message_id
        for message_id, timestamp in rows
        if timestamp is not None and timestamp < cutoff_utc
    ]
    if pre_cutoff_ids:
        daily_boundary_id = max(pre_cutoff_ids)
        boundary_id = (
            daily_boundary_id
            if boundary_id is None
            else max(boundary_id, daily_boundary_id)
        )

    return boundary_id


def _frontier_covers(
    frontier: list[SessionSummary],
    *,
    first_message_id: int,
    last_message_id: int,
) -> bool:
    expected = first_message_id
    for summary in frontier:
        if summary.last_message_id < expected:
            continue
        if summary.first_message_id > expected:
            return False
        expected = summary.last_message_id + 1
        if expected > last_message_id:
            return True
    return expected > last_message_id


def _frontier_covered_prefix(
    frontier: list[SessionSummary],
    *,
    first_message_id: int,
) -> int:
    covered_until = first_message_id - 1
    for summary in frontier:
        if summary.last_message_id <= covered_until:
            continue
        if summary.first_message_id > covered_until + 1:
            break
        covered_until = max(covered_until, summary.last_message_id)
    return covered_until


def estimate_session_tokens(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
    content_codec: ContentCodec | None = None,
) -> int:
    return _estimate_session_tokens_from_id(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
        first_message_id=None,
        content_codec=content_codec,
    )


def _estimate_session_tokens_from_id(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None,
    first_message_id: int | None,
    content_codec: ContentCodec | None = None,
) -> int:
    total = 0
    where_clause = "session_id = ? AND agent_id IS ?"
    params: tuple[object, ...] = (session_id, agent_id)
    if first_message_id is not None:
        where_clause = "session_id = ? AND agent_id IS ? AND id >= ?"
        params = (session_id, agent_id, first_message_id)

    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT message_json
            FROM messages
            WHERE {where_clause}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        for row in rows:
            msg = strip_prompt_payloads(
                single_message_adapter.validate_python(
                    json.loads(_decode_stored(content_codec, row[0]))
                )
            )
            total += _message_token_estimate(msg)
    return total


def find_session_compaction_span(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
    timezone_name: str,
    now_utc: datetime | None = None,
    tau_soft: int = TAU_SOFT,
    content_codec: ContentCodec | None = None,
) -> tuple[int, int] | None:
    if session_id.startswith("onboarding:"):
        return None
    rows = _message_rows(db_path, session_id=session_id, agent_id=agent_id)
    if not rows:
        return None

    first_message_id = rows[0][0]
    last_message_id = rows[-1][0]
    frontier = fetch_session_summary_frontier(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
        content_codec=content_codec,
    )
    covered_until = _frontier_covered_prefix(
        frontier,
        first_message_id=first_message_id,
    )
    next_uncovered = covered_until + 1
    if next_uncovered > last_message_id:
        return None

    boundary_id = _latest_boundary_message_id(
        rows,
        timezone_name=timezone_name,
        now_utc=now_utc,
    )
    if boundary_id is not None and boundary_id >= next_uncovered:
        return next_uncovered, boundary_id

    uncovered_tokens = _estimate_session_tokens_from_id(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
        first_message_id=next_uncovered,
        content_codec=content_codec,
    )
    if uncovered_tokens > tau_soft:
        return next_uncovered, last_message_id
    return None


def render_compaction_source(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
    first_message_id: int,
    last_message_id: int,
    content_codec: ContentCodec | None = None,
) -> str:
    hits = load_messages_in_id_range(
        db_path,
        first_message_id,
        last_message_id,
        session_id=session_id,
        agent_id=agent_id,
        content_codec=content_codec,
    )
    return "\n---\n".join(hit.body for hit in hits)


def list_compactable_session_ids(
    db_path: str,
    *,
    agent_id: str | None = None,
) -> list[str]:
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT session_id
            FROM messages
            WHERE session_id NOT LIKE 'onboarding:%'
                AND agent_id IS ?
            ORDER BY session_id ASC
            """,
            (agent_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _load_messages_after_id_by_token_budget(
    db_path: str,
    *,
    token_budget: int,
    session_id: str,
    agent_id: str | None,
    after_id: int,
    content_codec: ContentCodec | None = None,
) -> list[ModelMessage]:
    rows = _load_active_context_rows_by_token_budget(
        db_path,
        token_budget=token_budget,
        session_id=session_id,
        agent_id=agent_id,
        after_id=after_id,
        content_codec=content_codec,
    )
    return _trim_unpaired_tool_messages([row.message for row in rows])


def _load_active_context_rows_by_token_budget(
    db_path: str,
    *,
    token_budget: int,
    session_id: str,
    agent_id: str | None = None,
    after_id: int | None = None,
    content_codec: ContentCodec | None = None,
) -> list[_ActiveContextRow]:
    if token_budget < 0:
        raise ValueError("token_budget must be greater than or equal to 0.")
    if token_budget == 0:
        return []

    selected: list[_ActiveContextRow] = []
    total = 0
    where_clause = "session_id = ? AND agent_id IS ?"
    params: tuple[object, ...] = (session_id, agent_id)
    if after_id is not None:
        where_clause = "session_id = ? AND agent_id IS ? AND id > ?"
        params = (session_id, agent_id, after_id)

    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT id, timestamp, message_json
            FROM messages
            WHERE {where_clause}
            ORDER BY id DESC
            """,
            params,
        ).fetchall()
        for row in rows:
            msg = strip_prompt_payloads(
                single_message_adapter.validate_python(
                    json.loads(_decode_stored(content_codec, row[2]))
                )
            )
            estimate = _message_token_estimate(msg)
            if selected and total + estimate > token_budget:
                break
            selected.append(
                _ActiveContextRow(
                    id=int(row[0]),
                    timestamp=_parse_timestamp(row[1]),
                    message=msg,
                )
            )
            total += estimate

    selected.reverse()
    return selected


def load_active_context_messages(
    db_path: str,
    *,
    token_budget: int,
    session_id: str = "default",
    agent_id: str | None = None,
    timezone_name: str = "UTC",
    now_utc: datetime | None = None,
    content_codec: ContentCodec | None = None,
) -> list[ModelMessage]:
    tail_rows = _load_active_context_rows_by_token_budget(
        db_path,
        token_budget=token_budget,
        session_id=session_id,
        agent_id=agent_id,
        content_codec=content_codec,
    )
    if not tail_rows:
        return []

    rows = [(row.id, row.timestamp) for row in tail_rows]
    boundary_id = _latest_boundary_message_id(
        rows,
        timezone_name=timezone_name,
        now_utc=now_utc,
    )
    if boundary_id is None:
        return _trim_unpaired_tool_messages([row.message for row in tail_rows])

    first_message_id = _first_message_id(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
    )
    if first_message_id is None:
        return []
    frontier = fetch_session_summary_frontier(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
        content_codec=content_codec,
    )
    if not _frontier_covers(
        frontier,
        first_message_id=first_message_id,
        last_message_id=boundary_id,
    ):
        return _trim_unpaired_tool_messages([row.message for row in tail_rows])

    return _load_messages_after_id_by_token_budget(
        db_path,
        token_budget=token_budget,
        session_id=session_id,
        agent_id=agent_id,
        after_id=boundary_id,
        content_codec=content_codec,
    )


def render_recap_blocks(
    frontier: list[SessionSummary],
    *,
    forbidden_secret_values: tuple[str, ...] = (),
) -> list[str]:
    """Render each frontier summary as an `expand_history`-compatible block.

    The `[Recap of messages N-M -- verbatim via expand_history]` header format
    is load-bearing: drill-down tooling parses it to recover the message range
    to hand to `expand_history`. Keep `render_session_recap` and
    `fresh_context` sharing this one renderer instead of duplicating the
    format string.
    """
    blocks: list[str] = []
    for summary in frontier:
        block = (
            f"[Recap of messages {summary.first_message_id}-{summary.last_message_id} "
            "-- verbatim via expand_history]\n"
            f"{summary.summary_text}"
        )
        assert_no_forbidden_secret_values(forbidden_secret_values, block)
        blocks.append(block)
    return blocks


def render_session_recap(
    db_path: str,
    *,
    session_id: str,
    agent_id: str | None = None,
    forbidden_secret_values: tuple[str, ...] = (),
    content_codec: ContentCodec | None = None,
) -> str:
    if session_id.startswith("onboarding:"):
        return ""
    if not os.path.exists(db_path):
        return ""
    try:
        frontier = fetch_session_summary_frontier(
            db_path,
            session_id=session_id,
            agent_id=agent_id,
            content_codec=content_codec,
        )
    except sqlite3.Error:
        return ""
    if not frontier:
        return ""

    blocks = render_recap_blocks(
        frontier,
        forbidden_secret_values=forbidden_secret_values,
    )
    return "\n\n".join(blocks)


# A floor on the raw-tail budget so a very large frontier recap never fully
# starves the recent-messages tail in `load_fresh_context_rows`.
_MIN_TAIL_TOKEN_BUDGET = 200


def load_fresh_context_rows(
    db_path: str,
    *,
    token_budget: int,
    session_id: str,
    agent_id: str | None = None,
    content_codec: ContentCodec | None = None,
) -> tuple[list[SessionSummary], list[TranscriptHit]]:
    """Assemble the fresh-context frontier + token-budgeted raw tail.

    Returns the summary recap frontier (oldest-first) alongside the raw
    message tail strictly after the frontier's covered prefix, budgeted so
    the combined recap + tail fits within `token_budget`.

    The frontier is a ceiling, not a bonus on top of the budget: if the
    frontier alone would leave less than `_MIN_TAIL_TOKEN_BUDGET` for the
    tail, the oldest frontier entries are dropped (oldest-first) until the
    remainder fits within `token_budget - _MIN_TAIL_TOKEN_BUDGET`. If no
    frontier entry fits even alone, the frontier is dropped entirely and the
    tail is budgeted over the full `token_budget`, matching the no-summaries
    fallback below. With no summaries (or an emptied-out frontier), the tail
    is budgeted over the full `token_budget` from the start of the session
    (boundary 0).
    """
    frontier = fetch_session_summary_frontier(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
        content_codec=content_codec,
    )

    first_message_id = _first_message_id(db_path, session_id=session_id, agent_id=agent_id)
    if first_message_id is None:
        return frontier, []

    if frontier:
        frontier_tokens = sum(
            estimate_tokens(summary.summary_text) for summary in frontier
        )
        frontier_ceiling = token_budget - _MIN_TAIL_TOKEN_BUDGET
        while frontier and frontier_tokens > frontier_ceiling:
            oldest = frontier.pop(0)
            frontier_tokens -= estimate_tokens(oldest.summary_text)

    if frontier:
        covered_until = _frontier_covered_prefix(
            frontier,
            first_message_id=first_message_id,
        )
        tail_budget = token_budget - frontier_tokens
        after_id: int | None = covered_until
    else:
        tail_budget = token_budget
        after_id = None

    tail_rows = _load_active_context_rows_by_token_budget(
        db_path,
        token_budget=tail_budget,
        session_id=session_id,
        agent_id=agent_id,
        after_id=after_id,
        content_codec=content_codec,
    )
    tail_rows = _trim_unpaired_tool_rows(tail_rows)

    hits: list[TranscriptHit] = []
    for row in tail_rows:
        body = message_search_text(row.message)
        if not body:
            continue
        hits.append(
            TranscriptHit(
                message_id=row.id,
                timestamp=row.timestamp.isoformat() if row.timestamp else None,
                body=body,
            )
        )
    return frontier, hits


def _trim_unpaired_tool_rows(
    rows: list[_ActiveContextRow],
) -> list[_ActiveContextRow]:
    messages = _trim_unpaired_tool_messages([row.message for row in rows])
    kept_ids = {id(message) for message in messages}
    return [row for row in rows if id(row.message) in kept_ids]
