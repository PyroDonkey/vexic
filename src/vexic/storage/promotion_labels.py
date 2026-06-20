import sqlite3
from collections.abc import Iterable
from contextlib import closing
from typing import Literal

from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage.schema import init_db

# Promotion labels (COA-93): human promote/reject judgments over Tier 2
# candidates, accumulating toward the ~100-pair eval corpus that will tune
# Deep scoring weights. Read-mostly eval data — never touched by dream cycles.

PromotionLabelValue = Literal["promote", "reject"]
_VALID_LABELS = ("promote", "reject")


def record_promotion_label(
    db_path: str,
    candidate_id: int,
    *,
    label: PromotionLabelValue,
    reason: str | None,
    forbidden_secret_values: Iterable[str] = (),
) -> int:
    """Record one human promotion judgment, snapshotting the candidate's text
    so the label survives later candidate changes. Fails loud on an unknown
    candidate or label value — a mislabeled eval row is worse than no row.

    The snapshotted `fact_text` and operator-entered `reason` are persisted to
    SQLite, so both are scrubbed against the tenant's loaded secret values
    before the INSERT (COA-116). Callers are responsible for passing the loaded
    tenant secret values as `forbidden_secret_values`; omitting them means there
    are no values to scrub."""
    if label not in _VALID_LABELS:
        raise ValueError(f"Label must be one of {_VALID_LABELS}, got {label!r}.")
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            row = conn.execute(
                "SELECT fact_text FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"No memory candidate with id {candidate_id}.")
            fact_text = str(row[0])
            assert_no_forbidden_secret_values(
                forbidden_secret_values, fact_text, reason or ""
            )
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO promotion_labels (
                    candidate_id, fact_text, label, reason
                )
                VALUES (?, ?, ?, ?)
                """,
                (candidate_id, fact_text, label, reason),
            )
            return int(cursor.lastrowid)


def count_promotion_labels(db_path: str) -> int:
    """Progress toward the ~100-pair eval threshold."""
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM promotion_labels").fetchone()[0])
