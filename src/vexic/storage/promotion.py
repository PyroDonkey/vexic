import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass

from vexic.embeddings import EMBEDDING_DIM
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage.candidates import (
    _load_source_message_ids,
    claim_candidate_for_promotion,
    link_candidate_to_promoted_fact,
    read_candidate_for_promotion,
    retire_candidate_for_fact,
)
from vexic.storage.longterm import (
    insert_long_term_fact,
    long_term_fact_exists_for_candidate,
    retire_long_term_fact,
)
from vexic.storage.schema import (
    DreamStatus,
    _ensure_vector_memory_schema,
    init_db,
)

# Cross-tier promotion. The single module allowed to span Tier 2 and Tier 3: it
# reads a candidate, claims it, writes the durable fact, and retires a
# contradicted neighbor — all in one connection so the transaction is atomic.
# The tier modules never import each other; this module owns the seam between
# them, and owns the promotion idempotency contract.


@dataclass(frozen=True)
class PromotionDecision:
    # One Deep-phase promotion. The candidate graduates to Tier 3; if it
    # contradicts an existing Tier 3 fact, that neighbor is retired in the
    # same transaction and linked to the new fact.
    candidate_id: int
    embedding: list[float]
    retired_fact_id: int | None = None
    retired_candidate_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CandidateRetirementDecision:
    # A selected Deep candidate lost a confidence-weighted conflict against an
    # existing Tier 3 fact. Promotion owns this write because it links a Tier 2
    # lifecycle change to a Tier 3 fact id.
    candidate_id: int
    retired_by_fact_id: int


@dataclass(frozen=True)
class DeepStats:
    promotions: int = 0
    retirements: int = 0


def _promoted_fact_id_for_candidate(conn: sqlite3.Connection, candidate_id: int) -> int:
    row = conn.execute(
        "SELECT promoted_fact_id FROM memory_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"Candidate {candidate_id} has no linked promoted fact.")
    return int(row[0])


def _retire_candidate(
    conn: sqlite3.Connection,
    decision: CandidateRetirementDecision,
) -> bool:
    row = read_candidate_for_promotion(conn, decision.candidate_id)
    if row is None:
        raise ValueError(f"Missing memory candidate {decision.candidate_id}.")

    promoted = bool(row[9])
    retired = bool(row[10])
    stale = bool(row[11])
    if promoted:
        if long_term_fact_exists_for_candidate(conn, decision.candidate_id):
            return False
        raise ValueError(
            f"Candidate {decision.candidate_id} is flagged promoted but has no Tier 3 "
            "fact; refusing to skip a corrupt promotion state."
        )
    if retired or stale:
        return False
    return retire_candidate_for_fact(
        conn,
        decision.candidate_id,
        decision.retired_by_fact_id,
    )


def _promote_candidate(
    conn: sqlite3.Connection,
    decision: PromotionDecision,
    *,
    forbidden_secret_values: Iterable[str] = (),
) -> tuple[bool, bool]:
    # Returns (promoted, retired). The promotion module owns the idempotency
    # contract here:
    #   * already-promoted candidate WITH a linked Tier 3 fact -> skip
    #     (False, False). This covers both a benign sequential re-run AND a
    #     concurrent loser that read the candidate after the winner committed
    #     promoted=1; both are indistinguishable from the row alone, so we skip
    #     rather than raise — a benign race must never abort the surrounding
    #     batch. A promoted flag with NO durable fact is corruption (unreachable
    #     by the atomic claim+insert), so it still fails loud below.
    #   * atomic-claim loss (read saw promoted=0, but a concurrent winner claimed
    #     in the read->write window) -> skip (False, False), no Tier 3 write.
    #   * genuinely-invalid input (missing / retired / stale / empty provenance)
    #     -> raise. These are caller errors, not races, and fail the cycle loud.
    if len(decision.embedding) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim embedding; got {len(decision.embedding)}.")

    row = read_candidate_for_promotion(conn, decision.candidate_id)
    if row is None:
        raise ValueError(f"Missing memory candidate {decision.candidate_id}.")
    (
        fact_text,
        subject,
        category,
        importance,
        confidence,
        source_ids_json,
        editable,
        retrieved_count,
        used_count,
        promoted,
        retired,
        stale,
    ) = row

    if retired or stale:
        raise ValueError(
            f"Candidate {decision.candidate_id} is not eligible for promotion "
            "(retired or stale)."
        )

    if promoted:
        if long_term_fact_exists_for_candidate(conn, decision.candidate_id):
            return (False, False)
        raise ValueError(
            f"Candidate {decision.candidate_id} is flagged promoted but has no Tier 3 "
            "fact; refusing to skip a corrupt promotion state."
        )

    source_message_ids = sorted(set(_load_source_message_ids(source_ids_json)))
    if not source_message_ids:
        # Invariant 5: no Tier 3 fact without a chain back to the raw log.
        raise ValueError(
            f"Refusing to promote candidate {decision.candidate_id} with empty source_message_ids."
        )
    assert_no_forbidden_secret_values(
        forbidden_secret_values,
        str(fact_text),
        str(subject),
        str(category),
        str(source_ids_json),
    )

    # Atomic claim BEFORE any Tier 3 write. The Python eligibility read above
    # fails loud on a clearly ineligible candidate; this claim settles the race
    # for the window between read and write. promoted_fact_id is filled after the
    # insert below.
    if not claim_candidate_for_promotion(conn, decision.candidate_id):
        return (False, False)

    fact_id = insert_long_term_fact(
        conn,
        fact_text=fact_text,
        subject=subject,
        category=category,
        importance=importance,
        confidence=confidence,
        source_message_ids=source_message_ids,
        promoted_from_candidate_id=decision.candidate_id,
        retrieved_count=retrieved_count,
        used_count=used_count,
        editable=editable,
        embedding=decision.embedding,
    )
    link_candidate_to_promoted_fact(conn, decision.candidate_id, fact_id)

    retired_flag = False
    if decision.retired_fact_id is not None:
        retired_flag = retire_long_term_fact(
            conn,
            fact_id=decision.retired_fact_id,
            superseded_by_fact_id=fact_id,
        )
    return (True, retired_flag)


def commit_deep_cycle(
    db_path: str,
    decisions: list[PromotionDecision | CandidateRetirementDecision],
    *,
    started_at: str,
    finished_at: str | None,
    status: DreamStatus = "ok",
    error_detail: str | None = None,
    model_requests: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost_micros: int = 0,
    forbidden_secret_values: Iterable[str] = (),
) -> DeepStats:
    init_db(db_path)
    if status == "error" and decisions:
        raise ValueError("Error deep cycles must not include promotions.")
    assert_no_forbidden_secret_values(forbidden_secret_values, error_detail or "")

    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            promotions = 0
            retirements = 0
            if decisions:
                _ensure_vector_memory_schema(conn)
                for decision in decisions:
                    if isinstance(decision, CandidateRetirementDecision):
                        if _retire_candidate(conn, decision):
                            retirements += 1
                        continue

                    promoted, retired = _promote_candidate(
                        conn,
                        decision,
                        forbidden_secret_values=forbidden_secret_values,
                    )
                    if promoted:
                        promotions += 1
                        if decision.retired_candidate_ids:
                            fact_id = _promoted_fact_id_for_candidate(
                                conn,
                                decision.candidate_id,
                            )
                            for candidate_id in decision.retired_candidate_ids:
                                if _retire_candidate(
                                    conn,
                                    CandidateRetirementDecision(
                                        candidate_id=candidate_id,
                                        retired_by_fact_id=fact_id,
                                    ),
                                ):
                                    retirements += 1
                    if retired:
                        retirements += 1

            conn.execute(
                """
                INSERT INTO dream_runs
                    (started_at, finished_at, status, messages_processed,
                     last_processed_message_id, promotions, retirements, error_detail,
                     model_requests, input_tokens, output_tokens, total_tokens,
                     estimated_cost_micros)
                VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    finished_at,
                    status,
                    promotions,
                    retirements,
                    error_detail,
                    model_requests,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    estimated_cost_micros,
                ),
            )
    return DeepStats(promotions=promotions, retirements=retirements)
