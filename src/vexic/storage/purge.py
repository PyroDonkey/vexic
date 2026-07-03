"""Physical purge of a tombstoned scope (ADR 0022).

The one storage path allowed to DELETE canonical rows. Everything runs inside
a single explicit transaction: an explicit ``BEGIN`` (not the bare
``with conn:``) because managed libSQL auto-commits each statement as its own
micro-transaction otherwise (see ``storage.connection.StorageConnection``).

Scope matching follows ADR 0007: ``agent_id`` is exact (``IS`` semantics, so a
NULL target purges shared-scope rows only, never a wildcard), and a NULL
target session selects every session inside the tenant database. Derived
content follows the source-intersection rule: any candidate, fact, or dedup
event whose ``source_message_ids`` touches a purged message is purged with
it, and a fact promoted from a purged candidate is purged even when its own
listed sources survive.
"""

from __future__ import annotations

import json
from contextlib import closing

from vexic.storage.connection import connect
from vexic.storage.vectors import select_vector_backend


# Id lists are passed as a single JSON array bound parameter and expanded with
# json_each, so purge never builds an ``IN (?, ?, ...)`` clause whose bound
# parameter count could blow past SQLite's SQLITE_MAX_VARIABLE_NUMBER limit
# (999 by default) on realistic scopes.
_IN_JSON = "SELECT value FROM json_each(?)"


def _collect_message_ids(
    conn: object,
    target_session_id: str | None,
    target_agent_id: str | None,
) -> list[int]:
    rows = conn.execute(
        """
        SELECT id FROM messages
        WHERE (? IS NULL OR session_id = ?) AND agent_id IS ?
        """,
        (target_session_id, target_session_id, target_agent_id),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _collect_derived_ids(
    conn: object,
    table: str,
    target_session_id: str | None,
    target_agent_id: str | None,
    message_ids: list[int],
) -> list[int]:
    if table not in ("memory_candidates", "long_term_memory"):
        raise ValueError(f"Unsupported derived table: {table!r}")
    if target_session_id is None:
        # A NULL target session is the whole-scope purge: every derived row
        # in the exact agent scope goes, deliberately without source
        # filtering. Session-granular requests must set target_session_id.
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE agent_id IS ?",
            (target_agent_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]
    if not message_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT id FROM {table}
        WHERE agent_id IS ?
            AND EXISTS (
                SELECT 1 FROM json_each({table}.source_message_ids)
                WHERE json_each.value IN ({_IN_JSON})
            )
        """,
        (target_agent_id, json.dumps(message_ids)),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _delete_by_ids(conn: object, sql_template: str, ids: list[int]) -> int:
    if not ids:
        return 0
    cursor = conn.execute(sql_template.format(ids=_IN_JSON), (json.dumps(ids),))
    return int(cursor.rowcount)


def _delete_session_scoped(
    conn: object,
    table: str,
    target_session_id: str | None,
    target_agent_id: str | None,
) -> int:
    if table not in ("retrieval_events", "candidate_retrieval_events", "session_summaries"):
        raise ValueError(f"Unsupported session-scoped table: {table!r}")
    cursor = conn.execute(
        f"""
        DELETE FROM {table}
        WHERE (? IS NULL OR session_id = ?) AND agent_id IS ?
        """,
        (target_session_id, target_session_id, target_agent_id),
    )
    return int(cursor.rowcount)


def _scrub_dream_run_error_detail(conn: object, target_agent_id: str | None) -> int:
    # dream_runs rows carry pipeline watermarks and must survive; only the
    # diagnostic column can hold content (legacy rows predating the
    # content-free error_detail change), so it is wiped rather than deleted.
    cursor = conn.execute(
        """
        UPDATE dream_runs SET error_detail = NULL
        WHERE agent_id IS ? AND error_detail IS NOT NULL
        """,
        (target_agent_id,),
    )
    return int(cursor.rowcount)


def purge_scope_rows(
    db_path: object,
    *,
    target_session_id: str | None,
    target_agent_id: str | None,
    tombstone_ids: list[int],
    purged_at: str,
    dry_run: bool = False,
) -> dict[str, int]:
    """Physically delete every content-bearing row of the tombstoned scope.

    Returns per-table affected-row counts. These are deleted-row counts for
    every table except ``dream_runs_error_detail_scrubbed``, which counts rows
    whose ``error_detail`` column was blanked in place (the row survives).
    ``dry_run`` executes the identical
    transaction and rolls it back, so the counts are exact projections. On
    success the matching tombstones flip ``physical_purge_deferred`` to 0 and
    record ``purged_at`` plus the counts JSON in the same transaction.
    """
    with closing(connect(db_path)) as conn:
        # The embedding stores are vec0 virtual tables locally; deleting from
        # them requires the sqlite-vec extension on this connection.
        select_vector_backend(conn).prepare(conn)
        conn.execute("BEGIN")
        try:
            message_ids = _collect_message_ids(
                conn, target_session_id, target_agent_id
            )
            candidate_ids = _collect_derived_ids(
                conn, "memory_candidates", target_session_id, target_agent_id, message_ids
            )
            fact_ids = _collect_derived_ids(
                conn, "long_term_memory", target_session_id, target_agent_id, message_ids
            )
            if candidate_ids:
                promoted_rows = conn.execute(
                    f"""
                    SELECT id FROM long_term_memory
                    WHERE promoted_from_candidate_id IN ({_IN_JSON})
                    """,
                    (json.dumps(candidate_ids),),
                ).fetchall()
                fact_ids = sorted({*fact_ids, *(int(row[0]) for row in promoted_rows)})

            counts: dict[str, int] = {}
            counts["memory_candidate_embeddings"] = _delete_by_ids(
                conn,
                "DELETE FROM memory_candidate_embeddings WHERE candidate_id IN ({ids})",
                candidate_ids,
            )
            counts["long_term_memory_embeddings"] = _delete_by_ids(
                conn,
                "DELETE FROM long_term_memory_embeddings WHERE fact_id IN ({ids})",
                fact_ids,
            )
            # Dedup events reference candidates AND raw message ids: a
            # discarded duplicate never became a candidate row, so the
            # incoming_source_message_ids sweep is what closes that loophole.
            dedup_deleted = 0
            if candidate_ids:
                cursor = conn.execute(
                    f"""
                    DELETE FROM memory_dedup_events
                    WHERE candidate_id IN ({_IN_JSON})
                        OR matched_candidate_id IN ({_IN_JSON})
                    """,
                    (json.dumps(candidate_ids), json.dumps(candidate_ids)),
                )
                dedup_deleted += int(cursor.rowcount)
            if message_ids:
                cursor = conn.execute(
                    f"""
                    DELETE FROM memory_dedup_events
                    WHERE EXISTS (
                        SELECT 1 FROM json_each(memory_dedup_events.incoming_source_message_ids)
                        WHERE json_each.value IN ({_IN_JSON})
                    )
                    """,
                    (json.dumps(message_ids),),
                )
                dedup_deleted += int(cursor.rowcount)
            counts["memory_dedup_events"] = dedup_deleted
            counts["promotion_labels"] = _delete_by_ids(
                conn,
                "DELETE FROM promotion_labels WHERE candidate_id IN ({ids})",
                candidate_ids,
            )
            # Base-row deletes keep the external-content FTS shadows
            # consistent through their AFTER DELETE triggers.
            counts["long_term_memory"] = _delete_by_ids(
                conn,
                "DELETE FROM long_term_memory WHERE id IN ({ids})",
                fact_ids,
            )
            counts["memory_candidates"] = _delete_by_ids(
                conn,
                "DELETE FROM memory_candidates WHERE id IN ({ids})",
                candidate_ids,
            )
            for table in (
                "retrieval_events",
                "candidate_retrieval_events",
                "session_summaries",
            ):
                counts[table] = _delete_session_scoped(
                    conn, table, target_session_id, target_agent_id
                )
            counts["source_transcript_ledger"] = _delete_by_ids(
                conn,
                "DELETE FROM source_transcript_ledger WHERE message_id IN ({ids})",
                message_ids,
            )
            # messages_fts has no triggers (rebuilt or maintained explicitly
            # by the transcript writers), so the shadow rows go by hand.
            counts["messages_fts"] = _delete_by_ids(
                conn,
                "DELETE FROM messages_fts WHERE message_id IN ({ids})",
                message_ids,
            )
            counts["messages"] = _delete_by_ids(
                conn,
                "DELETE FROM messages WHERE id IN ({ids})",
                message_ids,
            )
            counts["dream_runs_error_detail_scrubbed"] = _scrub_dream_run_error_detail(
                conn, target_agent_id
            )
            if tombstone_ids:
                conn.execute(
                    f"""
                    UPDATE scope_tombstones
                    SET physical_purge_deferred = 0,
                        purged_at = ?,
                        purged_counts = ?
                    WHERE id IN ({_IN_JSON})
                    """,
                    (purged_at, json.dumps(counts, sort_keys=True), json.dumps(tombstone_ids)),
                )
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
            return counts
        except Exception:
            conn.rollback()
            raise
