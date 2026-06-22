import json
import sqlite3
import unicodedata
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import TypeAdapter
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from vexic.storage.schema import _assert_no_forbidden_secret_values, _fts_match_query
from vexic.text_utils import estimate_tokens

# Tier 1 — the append-only transcript. Owns message (de)serialization, the
# messages_fts shadow that init_db builds, and the read/write/search surface.
# Sacred store: never UPDATE/DELETE rows in `messages`.

messages_adapter = TypeAdapter(list[ModelMessage])
single_message_adapter = TypeAdapter(ModelMessage)


def strip_prompt_payloads(msg: ModelMessage) -> ModelMessage:
    if not isinstance(msg, ModelRequest):
        return msg

    parts = [part for part in msg.parts if not isinstance(part, SystemPromptPart)]
    if len(parts) == len(msg.parts) and msg.instructions is None:
        return msg

    return replace(msg, parts=parts, instructions=None)


def message_search_text(msg: ModelMessage) -> str:
    lines: list[str] = []
    if isinstance(msg, ModelRequest):
        for part in msg.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                lines.append(f"User: {part.content}")
    elif isinstance(msg, ModelResponse):
        for part in msg.parts:
            if isinstance(part, TextPart):
                lines.append(f"Assistant: {part.content}")
    return "\n".join(lines)


def _tool_call_ids(msg: ModelMessage) -> set[str]:
    if not isinstance(msg, ModelResponse):
        return set()
    return {
        part.tool_call_id
        for part in msg.parts
        if isinstance(part, ToolCallPart)
    }


def _tool_return_ids(msg: ModelMessage) -> set[str]:
    if not isinstance(msg, ModelRequest):
        return set()
    return {
        part.tool_call_id
        for part in msg.parts
        if isinstance(part, ToolReturnPart)
    }


def _trim_unpaired_tool_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
    trimmed = messages
    while True:
        next_trimmed = _trim_unpaired_tool_messages_once(trimmed)
        if len(next_trimmed) == len(trimmed):
            return next_trimmed
        trimmed = next_trimmed


def _trim_unpaired_tool_messages_once(messages: list[ModelMessage]) -> list[ModelMessage]:
    call_ids: set[str] = set()
    returned_ids: set[str] = set()
    for msg in messages:
        call_ids.update(_tool_call_ids(msg))
        returned_ids.update(_tool_return_ids(msg))

    return [
        msg
        for msg in messages
        if (
            (not _tool_return_ids(msg) or _tool_return_ids(msg).issubset(call_ids))
            and (not _tool_call_ids(msg) or _tool_call_ids(msg).issubset(returned_ids))
        )
    ]


# The MATCH sanitizer moved to the schema spine so the Tier 3 keyword path
# reuses it; kept under the old private name for this module's call sites.
_fts_query = _fts_match_query


def _messages_fts_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(messages_fts)").fetchall()
    return [row[1] for row in rows]


def _ensure_messages_fts(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
    ).fetchone()
    needs_rebuild = False

    if exists and _messages_fts_columns(conn) != ["message_id", "session_id", "agent_id", "body"]:
        conn.execute("DROP TRIGGER IF EXISTS messages_after_insert")
        conn.execute("DROP TABLE IF EXISTS messages_fts")
        exists = None
        needs_rebuild = True

    if not exists:
        conn.execute(
            """
            CREATE VIRTUAL TABLE messages_fts
            USING fts5(message_id UNINDEXED, session_id UNINDEXED, agent_id UNINDEXED, body)
            """
        )
        needs_rebuild = True

    if needs_rebuild:
        _rebuild_messages_fts(conn)


def _rebuild_messages_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM messages_fts")
    rows = conn.execute(
        "SELECT id, session_id, agent_id, message_json FROM messages ORDER BY id ASC"
    ).fetchall()
    for message_id, session_id, agent_id, message_json in rows:
        msg = single_message_adapter.validate_python(json.loads(message_json))
        body = message_search_text(msg)
        if body:
            conn.execute(
                """
                INSERT INTO messages_fts (message_id, session_id, agent_id, body)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, session_id, agent_id, body),
            )


def rebuild_messages_fts(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            _rebuild_messages_fts(conn)


def load_messages(
    db_path: str,
    limit: int | None = None,
    *,
    session_id: str = "default",
    agent_id: str | None = None,
) -> list[ModelMessage]:
    with closing(sqlite3.connect(db_path)) as conn:
        if limit is None:
            rows = conn.execute(
                """
                SELECT message_json
                FROM messages
                WHERE session_id = ?
                    AND agent_id IS ?
                ORDER BY id ASC
                """,
                (session_id, agent_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT message_json
                FROM (
                    SELECT id, message_json
                    FROM messages
                    WHERE session_id = ?
                        AND agent_id IS ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (session_id, agent_id, limit),
            ).fetchall()
        json_list = [json.loads(row[0]) for row in rows]
        messages = [
            strip_prompt_payloads(msg)
            for msg in messages_adapter.validate_python(json_list)
        ]
        return _trim_unpaired_tool_messages(messages)


def _message_token_estimate(msg: ModelMessage) -> int:
    sanitized = strip_prompt_payloads(msg)
    return estimate_tokens(single_message_adapter.dump_json(sanitized).decode())


def load_messages_by_token_budget(
    db_path: str,
    token_budget: int,
    *,
    session_id: str = "default",
    agent_id: str | None = None,
) -> list[ModelMessage]:
    if token_budget < 0:
        raise ValueError("token_budget must be greater than or equal to 0.")
    if token_budget == 0:
        return []

    selected: list[ModelMessage] = []
    total = 0
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT message_json
            FROM messages
            WHERE session_id = ?
                AND agent_id IS ?
            ORDER BY id DESC
            """,
            (session_id, agent_id),
        )
        for row in rows:
            msg = strip_prompt_payloads(
                single_message_adapter.validate_python(json.loads(row[0]))
            )
            estimate = _message_token_estimate(msg)
            if selected and total + estimate > token_budget:
                break
            selected.append(msg)
            total += estimate

    selected.reverse()
    return _trim_unpaired_tool_messages(selected)


def save_messages(
    db_path: str,
    messages: list[ModelMessage],
    *,
    session_id: str = "default",
    agent_id: str | None = None,
    forbidden_secret_values: Iterable[str] = (),
    timestamp: str | None = None,
) -> list[int]:
    message_ids: list[int] = []
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            for msg in messages:
                sanitized_msg = strip_prompt_payloads(msg)
                msg_json = single_message_adapter.dump_json(sanitized_msg).decode()
                body = message_search_text(sanitized_msg)
                _assert_no_forbidden_secret_values(
                    forbidden_secret_values,
                    msg_json,
                    body,
                )
                if timestamp is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO messages (session_id, agent_id, message_json)
                        VALUES (?, ?, ?)
                        """,
                        (session_id, agent_id, msg_json),
                    )
                else:
                    cursor = conn.execute(
                        """
                        INSERT INTO messages (session_id, agent_id, timestamp, message_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, agent_id, timestamp, msg_json),
                    )
                if body:
                    conn.execute(
                        """
                        INSERT INTO messages_fts (message_id, session_id, agent_id, body)
                        VALUES (?, ?, ?, ?)
                        """,
                        (cursor.lastrowid, session_id, agent_id, body),
                    )
                if cursor.lastrowid is not None:
                    message_ids.append(int(cursor.lastrowid))
    return message_ids


@dataclass(frozen=True)
class SourceTranscriptInput:
    source_host: str
    source_session_id: str
    source_message_id: str
    message_json: str


@dataclass(frozen=True)
class SourceTranscriptIngestResult:
    source_host: str
    source_session_id: str
    source_message_id: str
    status: Literal["inserted", "skipped", "rejected"]
    message_id: int | None = None
    reason: str | None = None
    warning: str | None = None


def _source_duplicate_warning(existing_message_json: object, msg_json: str) -> str | None:
    try:
        existing = json.loads(str(existing_message_json))
        incoming = json.loads(msg_json)
    except (json.JSONDecodeError, TypeError):
        return "source key already ingested; existing content unreadable"
    if existing != incoming:
        return "source key already ingested with different content"
    return None


def _normalize_source_host(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _normalize_source_id(value: str) -> str:
    # Source host names are case-insensitive; host-owned session/message IDs are not.
    return unicodedata.normalize("NFC", value.strip())


def _part_kind(part: object) -> str:
    return str(getattr(part, "part_kind", type(part).__name__))


def _polluted_transcript_reason(msg: ModelMessage) -> str | None:
    if isinstance(msg, ModelRequest):
        if msg.instructions is not None:
            return "dynamic instructions are not transcript text"
        if msg.run_id is not None or msg.metadata:
            return "request metadata is not transcript text"
        for part in msg.parts:
            if isinstance(part, UserPromptPart):
                if not isinstance(part.content, str):
                    return "non-text user content is not transcript text"
                continue
            return f"{_part_kind(part)} is not transcript text"
    elif isinstance(msg, ModelResponse):
        if (
            msg.model_name is not None
            or msg.provider_name is not None
            or msg.provider_url is not None
            or msg.provider_details is not None
            or msg.provider_response_id is not None
            or msg.finish_reason is not None
            or msg.run_id is not None
            or msg.metadata
            or msg.usage != type(msg.usage)()
        ):
            return "response metadata is not transcript text"
        for part in msg.parts:
            if isinstance(part, TextPart):
                if part.id is not None or part.provider_name is not None or part.provider_details:
                    return "text metadata is not transcript text"
                continue
            return f"{_part_kind(part)} is not transcript text"
    else:
        return "only user and assistant transcript messages can be ingested"

    if not message_search_text(msg):
        return "message has no transcript text"
    return None


def ingest_source_messages(
    db_path: str,
    inputs: list[SourceTranscriptInput],
    *,
    session_id: str = "default",
    agent_id: str | None = None,
    forbidden_secret_values: Iterable[str] = (),
) -> list[SourceTranscriptIngestResult]:
    results: list[SourceTranscriptIngestResult] = []
    forbidden_values = tuple(forbidden_secret_values)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            for item in inputs:
                source_host = _normalize_source_host(item.source_host)
                source_session_id = _normalize_source_id(item.source_session_id)
                source_message_id = _normalize_source_id(item.source_message_id)
                if not source_host or not source_session_id or not source_message_id:
                    results.append(
                        SourceTranscriptIngestResult(
                            source_host=source_host,
                            source_session_id=source_session_id,
                            source_message_id=source_message_id,
                            status="rejected",
                            reason="source identifiers must not be blank",
                        )
                    )
                    continue
                try:
                    message = single_message_adapter.validate_json(item.message_json)
                except ValueError:
                    results.append(
                        SourceTranscriptIngestResult(
                            source_host=source_host,
                            source_session_id=source_session_id,
                            source_message_id=source_message_id,
                            status="rejected",
                            reason="invalid message_json",
                        )
                    )
                    continue
                msg_json = single_message_adapter.dump_json(message).decode()
                body = message_search_text(message)
                reason = _polluted_transcript_reason(message)
                if reason is None:
                    try:
                        _assert_no_forbidden_secret_values(
                            forbidden_values,
                            source_host,
                            source_session_id,
                            source_message_id,
                            msg_json,
                            body,
                        )
                    except ValueError as exc:
                        reason = str(exc)
                if reason is not None:
                    results.append(
                        SourceTranscriptIngestResult(
                            source_host=source_host,
                            source_session_id=source_session_id,
                            source_message_id=source_message_id,
                            status="rejected",
                            reason=reason,
                        )
                    )
                    continue

                existing = conn.execute(
                    """
                    SELECT l.message_id, m.message_json
                    FROM source_transcript_ledger AS l
                    JOIN messages AS m ON m.id = l.message_id
                    WHERE l.source_host = ?
                        AND l.source_session_id = ?
                        AND l.source_message_id = ?
                        AND l.agent_id IS ?
                    """,
                    (source_host, source_session_id, source_message_id, agent_id),
                ).fetchone()
                if existing is not None:
                    warning = _source_duplicate_warning(existing[1], msg_json)
                    results.append(
                        SourceTranscriptIngestResult(
                            source_host=source_host,
                            source_session_id=source_session_id,
                            source_message_id=source_message_id,
                            status="skipped",
                            message_id=int(existing[0]),
                            warning=warning,
                        )
                    )
                    continue

                conn.execute("SAVEPOINT source_ingest_row")
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO messages (session_id, agent_id, message_json)
                        VALUES (?, ?, ?)
                        """,
                        (session_id, agent_id, msg_json),
                    )
                    message_id = int(cursor.lastrowid)
                    conn.execute(
                        """
                        INSERT INTO messages_fts (message_id, session_id, agent_id, body)
                        VALUES (?, ?, ?, ?)
                        """,
                        (message_id, session_id, agent_id, body),
                    )
                    conn.execute(
                        """
                        INSERT INTO source_transcript_ledger
                            (source_host, source_session_id, source_message_id, agent_id, message_id)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (source_host, source_session_id, source_message_id, agent_id, message_id),
                    )
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK TO SAVEPOINT source_ingest_row")
                    conn.execute("RELEASE SAVEPOINT source_ingest_row")
                    existing = conn.execute(
                        """
                        SELECT l.message_id, m.message_json
                        FROM source_transcript_ledger AS l
                        JOIN messages AS m ON m.id = l.message_id
                        WHERE l.source_host = ?
                            AND l.source_session_id = ?
                            AND l.source_message_id = ?
                            AND l.agent_id IS ?
                        """,
                        (source_host, source_session_id, source_message_id, agent_id),
                    ).fetchone()
                    if existing is None:
                        raise
                    warning = _source_duplicate_warning(existing[1], msg_json)
                    results.append(
                        SourceTranscriptIngestResult(
                            source_host=source_host,
                            source_session_id=source_session_id,
                            source_message_id=source_message_id,
                            status="skipped",
                            message_id=int(existing[0]),
                            warning=warning,
                        )
                    )
                    continue
                conn.execute("RELEASE SAVEPOINT source_ingest_row")
                results.append(
                    SourceTranscriptIngestResult(
                        source_host=source_host,
                        source_session_id=source_session_id,
                        source_message_id=source_message_id,
                        status="inserted",
                        message_id=message_id,
                    )
                )
    return results


# Provenance-rich Tier 1 search hit, mirroring LongTermFact's glass-box shape:
# the snippet plus the messages.id pointer back into the transcript, so callers
# can cite or later expand the exact source rows instead of bare text.
@dataclass(frozen=True)
class TranscriptHit:
    message_id: int
    timestamp: str | None
    body: str


class TranscriptRangeTooLarge(ValueError):
    def __init__(self, *, row_count: int, max_rows: int) -> None:
        self.row_count = row_count
        self.max_rows = max_rows
        super().__init__(
            f"Transcript range returned {row_count} rows; cap is {max_rows}."
        )


def search_messages(
    db_path: str,
    query: str,
    *,
    session_id: str = "default",
    agent_id: str | None = None,
    limit: int = 5,
) -> list[TranscriptHit]:
    if limit < 1:
        raise ValueError("limit must be at least 1.")

    safe_query = _fts_query(query)
    if safe_query is None:
        return []

    with closing(sqlite3.connect(db_path)) as conn:
        try:
            rows = conn.execute(
                """
                SELECT messages.id, messages.timestamp, messages_fts.body
                FROM messages_fts
                JOIN messages ON messages.id = messages_fts.message_id
                WHERE messages_fts MATCH ?
                    AND messages_fts.session_id = ?
                    AND messages_fts.agent_id IS ?
                    AND messages.agent_id IS ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, session_id, agent_id, agent_id, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        return [
            TranscriptHit(
                message_id=int(row[0]),
                timestamp=row[1],
                body=str(row[2]),
            )
            for row in rows
        ]


def load_messages_in_id_range(
    db_path: str,
    first_message_id: int,
    last_message_id: int,
    *,
    session_id: str = "default",
    agent_id: str | None = None,
    max_rows: int | None = None,
) -> list[TranscriptHit]:
    with closing(sqlite3.connect(db_path)) as conn:
        params: list[object] = [session_id, agent_id, first_message_id, last_message_id]
        limit_clause = ""
        if max_rows is not None:
            limit_clause = "LIMIT ?"
            params.append(max_rows + 1)
        rows = conn.execute(
            f"""
            SELECT id, timestamp, message_json
            FROM messages
            WHERE session_id = ?
                AND agent_id IS ?
                AND id BETWEEN ? AND ?
            ORDER BY id ASC
            {limit_clause}
            """,
            params,
        ).fetchall()

    if max_rows is not None and len(rows) > max_rows:
        raise TranscriptRangeTooLarge(row_count=len(rows), max_rows=max_rows)

    hits: list[TranscriptHit] = []
    for row in rows:
        msg = strip_prompt_payloads(
            single_message_adapter.validate_python(json.loads(row[2]))
        )
        body = message_search_text(msg)
        if not body:
            continue
        hits.append(
            TranscriptHit(
                message_id=int(row[0]),
                timestamp=row[1],
                body=body,
            )
        )
    return hits


def load_messages_since(
    db_path: str,
    after_id: int,
    limit: int | None = None,
    *,
    exclude_session_prefixes: tuple[str, ...] = (),
) -> list[tuple[int, ModelMessage]]:
    with closing(sqlite3.connect(db_path)) as conn:
        filters = ["id > ?"]
        params: list[object] = [after_id]
        for prefix in exclude_session_prefixes:
            filters.append("session_id NOT LIKE ?")
            params.append(f"{prefix}%")
        where_clause = " AND ".join(filters)

        if limit is None:
            rows = conn.execute(
                f"SELECT id, message_json FROM messages WHERE {where_clause} ORDER BY id ASC",
                params,
            ).fetchall()
        else:
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT id, message_json
                FROM messages
                WHERE {where_clause}
                ORDER BY id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [
            (
                row[0],
                strip_prompt_payloads(single_message_adapter.validate_python(json.loads(row[1]))),
            )
            for row in rows
        ]


def get_watermark(db_path: str) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT MAX(last_processed_message_id) FROM dream_runs WHERE status = 'ok'"
        ).fetchone()
        return row[0] if row[0] is not None else 0
