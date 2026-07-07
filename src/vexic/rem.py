"""REM phase: score active Tier 2 candidates with a local embedding-centrality
heuristic and write boost signals only.

REM is deterministic and makes no model calls: each candidate's ``rem_boost``
is the mean cosine similarity to its top-k nearest embedded peers, computed
from the embeddings the Light phase already stored. The boost semantics the
Deep phase consumes are unchanged -- a value in [0, 1] per candidate.
"""

import asyncio

from vexic.error_reporting import format_error_detail
from vexic.storage import RemCandidate, commit_rem_cycle, load_rem_candidates
from vexic.timeutil import utc_now_iso
from vexic.usage import UsageSummary

REM_TOP_K = 3


def compute_centrality_boosts(
    candidates: list[RemCandidate],
    *,
    top_k: int = REM_TOP_K,
) -> dict[int, float]:
    """Deterministic embedding-centrality boost for every candidate.

    Each candidate with a stored embedding scores the mean cosine similarity to
    its ``top_k`` most similar embedded peers (fewer when fewer peers exist --
    no zero-padding), clamped to [0, 1]. Embeddings are L2-normalized at write
    time, so cosine similarity is a plain dot product. Candidates without an
    embedding score 0.0 and never count as anyone's neighbor; writing the zero
    also resets any stale boost from an earlier cycle.
    """
    if top_k <= 0:
        raise ValueError("top_k must be greater than 0.")

    boosts = {candidate.candidate_id: 0.0 for candidate in candidates}
    embedded = [
        (candidate.candidate_id, candidate.embedding)
        for candidate in candidates
        if candidate.embedding is not None
    ]
    for index, (candidate_id, embedding) in enumerate(embedded):
        similarities = sorted(
            (
                sum(a * b for a, b in zip(embedding, other, strict=True))
                for other_index, (_, other) in enumerate(embedded)
                if other_index != index
            ),
            reverse=True,
        )
        if not similarities:
            continue
        top = similarities[:top_k]
        mean = sum(top) / len(top)
        boosts[candidate_id] = min(1.0, max(0.0, mean))

    return boosts


async def run_rem_phase(
    db_path: str,
    *,
    agent_id: str | None = None,
    top_k: int = REM_TOP_K,
    forbidden_secret_values: tuple[str, ...] = (),
) -> UsageSummary:
    # forbidden_secret_values stays threaded because commit_rem_cycle guards
    # error_detail persistence with it; REM itself sends nothing to a model,
    # so there is no prompt egress to redact.
    started_at = utc_now_iso()
    try:
        candidates = load_rem_candidates(db_path, agent_id=agent_id)
        if not candidates:
            commit_rem_cycle(
                db_path,
                {},
                agent_id=agent_id,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status="ok",
                forbidden_secret_values=forbidden_secret_values,
            )
            print("REM phase: no eligible candidates. No-op.")
            return UsageSummary()

        boosts = compute_centrality_boosts(candidates, top_k=top_k)
        missing_embeddings = sum(
            1 for candidate in candidates if candidate.embedding is None
        )
        stats = commit_rem_cycle(
            db_path,
            boosts,
            agent_id=agent_id,
            started_at=started_at,
            finished_at=utc_now_iso(),
            status="ok",
            forbidden_secret_values=forbidden_secret_values,
        )
        print(f"REM phase: {stats.boosted} candidates boosted.")
        if missing_embeddings > 0:
            print(
                f"REM phase: {missing_embeddings} candidates lack embeddings; "
                "their boosts were reset to 0.0."
            )
        return UsageSummary()
    except Exception as exc:
        try:
            commit_rem_cycle(
                db_path,
                {},
                agent_id=agent_id,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status="error",
                error_detail=format_error_detail(exc),
                forbidden_secret_values=forbidden_secret_values,
            )
        except Exception:
            # Best-effort status write; the original error is surfaced below.
            pass
        print(
            f"REM phase: ERROR -- {type(exc).__name__}. "
            "Boosts unchanged; Deep phase also skipped this cycle."
        )
        raise


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the REM memory boost phase once.")
    parser.add_argument("--db", required=True, help="Path to a Vexic SQLite memory database.")
    parser.add_argument("--agent-id", help="Optional agent memory scope. Omit for shared scope.")
    args = parser.parse_args()

    asyncio.run(run_rem_phase(args.db, agent_id=args.agent_id))


if __name__ == "__main__":
    _main()
