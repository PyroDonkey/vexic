"""Age-based retention for content-bearing retrieval telemetry (ADR 0022).

``retrieval_events.query``/``rewritten_query`` and
``candidate_retrieval_events.query`` hold raw user query text. The rows
themselves are the durable source for ``retrieved_count``/``used_count`` and
for replayable retrieval behavior, so retention blanks the query text in
place instead of deleting rows: counters, verdicts, and timing survive, the
content does not. ``query`` is NOT NULL, so blanked means empty string;
``rewritten_query`` is nullable and is nulled.

Hosts choose the window (the hosted adapter defaults to 90 days); the local
core retains by default because the data sits in the user's own database.
"""

from __future__ import annotations

from contextlib import closing

from vexic.storage.connection import connect


def expire_retrieval_queries(db_path: object, *, older_than: str) -> dict[str, int]:
    """Blank query text on telemetry rows retrieved before ``older_than``.

    ``older_than`` is an ISO-8601 timestamp compared against ``retrieved_at``.
    Returns per-table counts of rows whose text was blanked. Idempotent:
    already-blanked rows do not count again. On managed libSQL the two
    UPDATEs auto-commit separately; a crash between them leaves a partially
    applied but harmless state that the next run completes.
    """
    counts: dict[str, int] = {}
    with closing(connect(db_path)) as conn:
        with conn:
            cursor = conn.execute(
                """
                UPDATE retrieval_events
                SET query = '', rewritten_query = NULL
                WHERE retrieved_at < ?
                    AND (query != '' OR rewritten_query IS NOT NULL)
                """,
                (older_than,),
            )
            counts["retrieval_events"] = int(cursor.rowcount)
            cursor = conn.execute(
                """
                UPDATE candidate_retrieval_events
                SET query = ''
                WHERE retrieved_at < ? AND query != ''
                """,
                (older_than,),
            )
            counts["candidate_retrieval_events"] = int(cursor.rowcount)
    return counts
