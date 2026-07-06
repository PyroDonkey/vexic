"""Deep phase: score Tier 2 candidates, promote the top-N to Tier 3, and
retire any Tier 3 fact a promotion contradicts.

`rem_boost` is the local embedding-centrality signal written by the REM phase
(docs/adr/0020-heuristic-rem-lowers-dream-phase-llm-floor.md). Retrieval-side
scoring signals (`relevance` and `query_diversity`) are fixed at 0 until those
inputs exist; 0 is the accepted value for unimplemented signals.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import log
from typing import Any

from vexic.error_reporting import format_error_detail
from vexic.ports import AgentFactory, missing_host_port
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import (
    CandidateRetirementDecision,
    LongTermNeighbor,
    PromotionCandidate,
    PromotionDecision,
    commit_deep_cycle,
    init_vector_memory,
    load_promotion_candidates,
    nearest_long_term_facts,
)
from vexic.timeutil import utc_now_iso
from vexic.usage import UsageSummary, summarize_agent_usage

# Learn: half-life ≈ 69 days because 0.99^69 ≈ 0.5. Each day multiplies the
# recency signal by 0.99, so a fact not seen for ~69 days counts about half.
RECENCY_DECAY_BASE = 0.99
DEFAULT_TOP_N = 15
DEFAULT_NEIGHBOR_K = 3
# Safety floor on the judge's self-reported confidence before a contradiction
# retires a Tier 3 fact. Retirement is the only destructive-ish action this
# phase, so a low-confidence "contradicts" must not silently retire a fact.
CONTRADICTION_CONFIDENCE_THRESHOLD = 0.5

__all__ = [
    "PromotionCandidate",
    "build_contradiction_agent",
    "compute_score",
    "select_promotions",
    "run_deep_phase",
]


def build_contradiction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Any:
    raise missing_host_port("Deep contradiction judge")


@dataclass(frozen=True)
class _PendingPromotion:
    candidate: PromotionCandidate
    retired_fact_ids: tuple[int, ...] = ()
    retired_candidate_ids: tuple[int, ...] = ()


def _judge_prompt(candidate: PromotionCandidate, neighbor: LongTermNeighbor) -> str:
    return (
        f"New fact: {candidate.fact_text}\n"
        f"Existing fact: {neighbor.fact_text}\n"
        "Does the new fact contradict the existing fact?"
    )


def _candidate_judge_prompt(
    candidate: PromotionCandidate,
    pending: PromotionCandidate,
) -> str:
    return (
        f"New fact: {candidate.fact_text}\n"
        f"Existing pending fact: {pending.fact_text}\n"
        "Do these facts contradict each other?"
    )


def _forbidden_secret_values(
    secrets: Mapping[str, str] | None,
    extra_values: tuple[str, ...] = (),
) -> list[str]:
    values = [] if secrets is None else list(secrets.values())
    return [*values, *extra_values]


def compute_score(
    *,
    importance: int,
    hit_count: int,
    days_since_last_seen: float,
    max_hit_count: int,
    rem_boost: float = 0.0,
) -> float:
    """Park et al. skeleton with reinforcement signals, all weights = 1.0.

    score = recency + importance_norm + hit_count_norm + rem_boost
    (relevance and query_diversity are 0 this phase.)
    """
    # The schema stores candidate timestamps at second granularity. Quantize
    # the scoring input to keep same-cycle candidates from flipping order over
    # sub-minute write timing noise; meaningful recency remains day-scale.
    recency = RECENCY_DECAY_BASE ** round(days_since_last_seen, 3)
    importance_norm = importance / 10
    # log-scaled and normalized against the busiest candidate this cycle, so the
    # signal self-normalizes instead of drifting with absolute hit counts.
    if max_hit_count > 0:
        hit_count_norm = log(1 + hit_count) / log(1 + max_hit_count)
    else:
        hit_count_norm = 0.0
    return recency + importance_norm + hit_count_norm + rem_boost


def select_promotions(
    candidates: list[PromotionCandidate],
    *,
    now: datetime,
    top_n: int = DEFAULT_TOP_N,
) -> list[PromotionCandidate]:
    """Score every eligible candidate and return the top-N, highest first.

    Top-N self-normalizes against score-scale drift, so there is no absolute
    score floor (see the Deep section of docs/architecture.md).
    """
    if not candidates:
        return []

    max_hit_count = max(c.hit_count for c in candidates)
    scored = [
        (
            compute_score(
                importance=c.importance,
                hit_count=c.hit_count,
                days_since_last_seen=(now - c.last_seen_at).total_seconds() / 86400,
                max_hit_count=max_hit_count,
                rem_boost=c.rem_boost,
            ),
            c,
        )
        for c in candidates
    ]
    # Python's sort is stable; equal-score candidates keep load order
    # (`load_promotion_candidates` orders by id ASC), which is the Deep
    # tie-break for same-cycle conflicts.
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [candidate for _, candidate in scored[:top_n]]


async def _high_confidence_contradiction(agent: Any, prompt: str) -> tuple[bool, UsageSummary]:
    judgment = await agent.run(prompt)
    usage = summarize_agent_usage(judgment)
    return (
        bool(judgment.output.contradicts)
        and float(judgment.output.confidence) >= CONTRADICTION_CONFIDENCE_THRESHOLD,
        usage,
    )


async def run_deep_phase(
    db_path: str,
    model_group: str,
    *,
    agent_id: str | None = None,
    secrets: Mapping[str, str] | None = None,
    top_n: int = DEFAULT_TOP_N,
    neighbor_k: int = DEFAULT_NEIGHBOR_K,
    now: datetime | None = None,
    contradiction_agent_factory: AgentFactory | None = None,
    defer_contradiction: bool = True,
    forbidden_secret_values: tuple[str, ...] = (),
) -> UsageSummary:
    """Score eligible Tier 2 candidates and promote the top-N to Tier 3.

    When a contradiction agent is supplied, contradictions are judged pairwise
    (new fact vs each Tier 3 neighbor). Every high-confidence contradiction is
    checked before deciding whether the candidate beats existing memory; this
    avoids letting nearest-neighbor order hide a later, higher-confidence
    contradiction. When contradiction is deferred, selected candidates promote
    without retiring facts or pending candidates. Like the Light phase, a
    failure records an error dream_run and re-raises, leaving Tier 3 untouched.
    """
    started_at = utc_now_iso()
    forbidden = _forbidden_secret_values(secrets, forbidden_secret_values)
    try:
        init_vector_memory(db_path)
        when = now or datetime.now(timezone.utc)

        candidates = load_promotion_candidates(db_path, agent_id=agent_id)
        selected = select_promotions(candidates, now=when, top_n=top_n)
        if not selected:
            commit_deep_cycle(
                db_path,
                [],
                agent_id=agent_id,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status="ok",
                forbidden_secret_values=forbidden,
            )
            print("Deep phase: no eligible candidates. No-op.")
            return UsageSummary()

        usage = UsageSummary()
        decisions: list[PromotionDecision | CandidateRetirementDecision]
        if contradiction_agent_factory is None and defer_contradiction:
            decisions = [
                PromotionDecision(
                    candidate_id=candidate.candidate_id,
                    embedding=candidate.embedding,
                )
                for candidate in selected
            ]
        else:
            agent_factory = contradiction_agent_factory or build_contradiction_agent
            agent = agent_factory(model_group, secrets=secrets)
            decisions = []
            pending_promotions: list[_PendingPromotion] = []
            for candidate in selected:
                neighbors = nearest_long_term_facts(
                    db_path,
                    candidate.embedding,
                    k=neighbor_k,
                    agent_id=agent_id,
                )
                contradicted_neighbors: list[LongTermNeighbor] = []
                candidate_retired = False
                for neighbor in neighbors:
                    prompt = _judge_prompt(candidate, neighbor)
                    assert_no_forbidden_secret_values(forbidden, prompt)
                    contradicts, judge_usage = await _high_confidence_contradiction(
                        agent,
                        prompt,
                    )
                    usage = usage.plus(judge_usage)
                    if contradicts:
                        contradicted_neighbors.append(neighbor)
                blocking_neighbors = [
                    neighbor
                    for neighbor in contradicted_neighbors
                    if neighbor.confidence >= candidate.confidence
                ]
                if blocking_neighbors:
                    winning_neighbor = max(
                        blocking_neighbors,
                        key=lambda neighbor: (neighbor.confidence, neighbor.similarity),
                    )
                    decisions.append(
                        CandidateRetirementDecision(
                            candidate_id=candidate.candidate_id,
                            retired_by_fact_id=winning_neighbor.fact_id,
                        )
                    )
                    candidate_retired = True
                if candidate_retired:
                    continue
                retired_fact_ids = tuple(
                    neighbor.fact_id for neighbor in contradicted_neighbors
                )

                retired_candidate_ids: list[int] = []
                surviving_pending: list[_PendingPromotion] = []
                for index, pending in enumerate(pending_promotions):
                    prompt = _candidate_judge_prompt(candidate, pending.candidate)
                    assert_no_forbidden_secret_values(forbidden, prompt)
                    contradicts, judge_usage = await _high_confidence_contradiction(
                        agent,
                        prompt,
                    )
                    usage = usage.plus(judge_usage)
                    if not contradicts:
                        surviving_pending.append(pending)
                        continue
                    if candidate.confidence > pending.candidate.confidence:
                        retired_candidate_ids.append(pending.candidate.candidate_id)
                        retired_candidate_ids.extend(pending.retired_candidate_ids)
                        continue
                    surviving_pending.append(
                        _PendingPromotion(
                            candidate=pending.candidate,
                            retired_fact_ids=pending.retired_fact_ids,
                            retired_candidate_ids=(
                                *pending.retired_candidate_ids,
                                candidate.candidate_id,
                            ),
                        )
                    )
                    surviving_pending.extend(pending_promotions[index + 1 :])
                    candidate_retired = True
                    break

                if candidate_retired:
                    pending_promotions = surviving_pending
                    continue
                pending_promotions = [
                    *surviving_pending,
                    _PendingPromotion(
                        candidate=candidate,
                        retired_fact_ids=retired_fact_ids,
                        retired_candidate_ids=tuple(sorted(set(retired_candidate_ids))),
                    ),
                ]

            for pending in pending_promotions:
                decisions.append(
                    PromotionDecision(
                        candidate_id=pending.candidate.candidate_id,
                        embedding=pending.candidate.embedding,
                        retired_fact_ids=pending.retired_fact_ids,
                        retired_candidate_ids=pending.retired_candidate_ids,
                    )
                )

        stats = commit_deep_cycle(
            db_path,
            decisions,
            agent_id=agent_id,
            started_at=started_at,
            finished_at=utc_now_iso(),
            status="ok",
            model_requests=usage.model_requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_micros=usage.estimated_cost_micros,
            forbidden_secret_values=forbidden,
        )
        print(
            f"Deep phase: {stats.promotions} promotions, {stats.retirements} retirements."
        )
        return usage

    except Exception as exc:
        # Best-effort audit. A failure writing the error row must not mask the
        # original exception that actually broke the cycle.
        try:
            commit_deep_cycle(
                db_path,
                [],
                agent_id=agent_id,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status="error",
                error_detail=format_error_detail(exc),
                forbidden_secret_values=forbidden,
            )
        except Exception:
            pass
        print(
            f"Deep phase: ERROR -- {type(exc).__name__}. Tier 3 unchanged; will retry."
        )
        raise


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Deep memory promotion phase once.")
    parser.add_argument("--db", required=True, help="Path to a Vexic SQLite memory database.")
    parser.add_argument("--model-group", required=True, help="Host model group label.")
    parser.add_argument("--agent-id", help="Optional agent memory scope. Omit for shared scope.")
    args = parser.parse_args()

    asyncio.run(run_deep_phase(args.db, args.model_group, agent_id=args.agent_id))


if __name__ == "__main__":
    _main()
