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
from vexic.storage.connection import connect

# Cross-tier promotion. The single module allowed to span Tier 2 and Tier 3: it
# reads a candidate, claims it, writes the durable fact, and retires a
# contradicted neighbor — all in one connection so the transaction is atomic.
# The tier modules never import each other; this module owns the seam between
# them, and owns the promotion idempotency contract.


@dataclass(frozen=True)
class PromotionDecision:
    # One Deep-phase promotion. The candidate graduates to Tier 3; every
    # lower-confidence Tier 3 fact it contradicts is retired in the same
    # transaction and linked to the new fact.
    candidate_id: int
    embedding: list[float]
    retired_fact_ids: tuple[int, ...] = ()
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


def _decision_candidate_ids(
    decision: PromotionDecision | CandidateRetirementDecision,
) -> list[int]:
    if isinstance(decision, PromotionDecision):
        return [decision.candidate_id, *decision.retired_candidate_ids]
    return [decision.candidate_id]


def _validate_decision_agent_scope(
    conn: sqlite3.Connection,
    decision: PromotionDecision | CandidateRetirementDecision,
    *,
    agent_id: str | None,
) -> None:
    for candidate_id in _decision_candidate_ids(decision):
        row = conn.execute(
            "SELECT agent_id FROM memory_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Missing memory candidate {candidate_id}.")
        if row[0] != agent_id:
            raise ValueError(
                f"Candidate {candidate_id} is outside the requested agent scope."
            )

    if isinstance(decision, PromotionDecision):
        retiring_fact_ids: tuple[int, ...] = decision.retired_fact_ids
    else:
        retiring_fact_ids = (
            (decision.retired_by_fact_id,)
            if decision.retired_by_fact_id is not None
            else ()
        )
    for fact_id in retiring_fact_ids:
        row = conn.execute(
            "SELECT agent_id FROM long_term_memory WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Missing retiring fact {fact_id}.")
        if row[0] != agent_id:
            raise ValueError(
                f"Retiring fact {fact_id} is outside the requested agent scope."
            )


def _retire_candidate(
    conn: sqlite3.Connection,
    decision: CandidateRetirementDecision,
) -> bool:
    row = read_candidate_for_promotion(conn, decision.candidate_id)
    if row is None:
        raise ValueError(f"Missing memory candidate {decision.candidate_id}.")

    promoted = bool(row[10])
    retired = bool(row[11])
    stale = bool(row[12])
    agent_id = row[6]
    fact_row = conn.execute(
        "SELECT agent_id FROM long_term_memory WHERE id = ?",
        (decision.retired_by_fact_id,),
    ).fetchone()
    if fact_row is None:
        raise ValueError(f"Missing retiring fact {decision.retired_by_fact_id}.")
    if fact_row[0] != agent_id:
        raise ValueError(
            f"Retiring fact {decision.retired_by_fact_id} is outside candidate agent scope."
        )
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
        agent_id=agent_id,
    )


def _promote_candidate(
    conn: sqlite3.Connection,
    decision: PromotionDecision,
    *,
    forbidden_secret_values: Iterable[str] = (),
) -> tuple[bool, int]:
    # Returns (promoted, retired_fact_count). The promotion module owns the idempotency
    # contract here:
    #   * already-promoted candidate WITH a linked Tier 3 fact -> skip
    #     (False, 0). This covers both a benign sequential re-run AND a
    #     concurrent loser that read the candidate after the winner committed
    #     promoted=1; both are indistinguishable from the row alone, so we skip
    #     rather than raise — a benign race must never abort the surrounding
    #     batch. A promoted flag with NO durable fact is corruption (unreachable
    #     by the atomic claim+insert), so it still fails loud below.
    #   * atomic-claim loss (read saw promoted=0, but a concurrent winner claimed
    #     in the read->write window) -> skip (False, 0), no Tier 3 write.
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
        agent_id,
        editable,
        retrieved_count,
        used_count,
        promoted,
        retired,
        stale,
        occurred_at,
    ) = row

    if retired or stale:
        raise ValueError(
            f"Candidate {decision.candidate_id} is not eligible for promotion "
            "(retired or stale)."
        )

    if promoted:
        if long_term_fact_exists_for_candidate(conn, decision.candidate_id):
            return (False, 0)
        raise ValueError(
            f"Candidate {decision.candidate_id} is flagged promoted but has no Tier 3 "
            "fact; refusing to skip a corrupt promotion state."
        )

    if category == "event" and not occurred_at:
        # Invariant 11: category "event" facts must carry occurred_at. Fail
        # loud here rather than write an undated event to Tier 3. Checked after
        # the `promoted` skip above so a legacy already-promoted event candidate
        # (predating this column, occurred_at still NULL) stays a benign
        # idempotent no-op instead of raising on rerun. `not occurred_at` also
        # treats "" as missing, matching the merge-side COALESCE(NULLIF(...))
        # backfill semantics below.
        raise ValueError(
            f"Refusing to promote candidate {decision.candidate_id} with category "
            "'event' and no occurred_at."
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
        return (False, 0)

    fact_id = insert_long_term_fact(
        conn,
        fact_text=fact_text,
        subject=subject,
        category=category,
        importance=importance,
        confidence=confidence,
        source_message_ids=source_message_ids,
        agent_id=agent_id,
        promoted_from_candidate_id=decision.candidate_id,
        retrieved_count=retrieved_count,
        used_count=used_count,
        editable=editable,
        embedding=decision.embedding,
        occurred_at=occurred_at,
    )
    link_candidate_to_promoted_fact(conn, decision.candidate_id, fact_id)

    retired_count = 0
    for retiring_fact_id in decision.retired_fact_ids:
        if retire_long_term_fact(
            conn,
            fact_id=retiring_fact_id,
            superseded_by_fact_id=fact_id,
            agent_id=agent_id,
        ):
            retired_count += 1
    return (True, retired_count)


def commit_deep_cycle(
    db_path: str,
    decisions: list[PromotionDecision | CandidateRetirementDecision],
    *,
    agent_id: str | None = None,
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

    with closing(connect(db_path)) as conn:
        with conn:
            promotions = 0
            retirements = 0
            if decisions:
                for decision in decisions:
                    _validate_decision_agent_scope(
                        conn,
                        decision,
                        agent_id=agent_id,
                    )
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
                    retirements += retired

            conn.execute(
                """
                INSERT INTO dream_runs
                    (started_at, finished_at, status, agent_id, messages_processed,
                     last_processed_message_id, promotions, retirements, error_detail,
                     model_requests, input_tokens, output_tokens, total_tokens,
                     estimated_cost_micros)
                VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    finished_at,
                    status,
                    agent_id,
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
