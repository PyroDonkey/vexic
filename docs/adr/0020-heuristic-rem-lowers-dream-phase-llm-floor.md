# Heuristic REM lowers the dream-phase LLM floor

Status: accepted

## Context

ADR 0016 lowered the dream-phase LLM floor with the optional local embedding
adapter and deferrable Deep contradiction, but the REM boost phase stayed
LLM-backed: every cycle rendered a per-batch prompt, asked a host-supplied
boost agent for structured `RemBoost` output, and produced nondeterministic
boosts, one model call per cycle even for trivial candidate sets.

`rem_boost` is a soft additive signal in [0, 1] consumed only by the Deep
phase's self-normalizing top-N promotion scoring, so it tolerates
approximation. And every active candidate already has (or should have) an
L2-normalized 384-dimensional embedding written by the Light phase, which
means a useful similarity structure over the candidate set exists locally
before REM runs.

## Decision

REM becomes a local deterministic embedding-centrality heuristic
(`vexic.rem.compute_centrality_boosts`). Each candidate's boost is the mean
cosine similarity to its top-3 most similar embedded peers in the same active
same-scope candidate set (fewer when fewer peers exist), clamped to [0, 1].
Embeddings are unit vectors, so similarity is a plain dot product. Candidates
without a stored embedding score 0.0 and never count as anyone's neighbor;
the loader uses a LEFT JOIN so those candidates are still written, resetting
any stale boost from an earlier cycle. Scope handling stays in the loader's
`agent_id` filter.

The REM agent port (`DreamPhasePorts.rem_agent_factory`), the
`RemBoost`/`RemBoostPlan` models, the adapter `build_rem_agent` factory, and
the adapter-validation requirement for it are deleted. Adapters now supply
three required symbols: `embed_texts`, `build_extraction_agent`, and
`build_contradiction_agent`.

The dream-phase LLM floor is now: Light extraction (required) plus the Deep
contradiction judge (optional and deferrable per ADR 0016; on hosted it is
wired and rides the same shared model group). The hosted default model for
the remaining LLM legs is `deepseek/deepseek-v4-pro` -- both the adapter code
default and the documented Railway `VEXIC_LIVE_MODEL` value.

The service-level `run_dream_phase` ports gate is unchanged: REM through the
service still requires configured dream-phase ports and fails closed with
`HostPortNotConfigured` without them, even though REM consumes none of them.
The fail-closed posture is kept deliberately.

## Consequences

- REM model spend drops to zero. REM `dream_runs`/usage rows report zero
  model usage, so dashboards summing per-phase tokens see REM=0.
- REM is deterministic and offline-testable; the same candidate set always
  yields the same boosts.
- Adapters shrink to three required symbols.
- Boost meaning shifts from "LLM-judged importance" to "embedding
  centrality": tight clusters (including cross-category near-duplicates that
  survive commit-time dedup) max out while isolated candidates score low.
  This shift is pinned in tests and accepted.
- bge-small anisotropy gives isolated-but-embedded candidates a mid-range
  floor rather than 0. That is a common-mode offset that cancels in
  within-cycle top-N ranking.
- Candidates without embeddings score 0.0, which is cosmetic for promotion
  since Deep's loader inner-joins embeddings anyway.
- Compute is O(n^2 * 384) in pure Python per scope per cycle, fine at Tier 2
  scale.

## Deferred

- Vectorized or SQL-side similarity for large scopes.
- Smarter clustering (e.g. community detection) or
  retrieval-telemetry-informed boosts.
- Per-cycle boost rescaling -- revisit if LongMemEval judged-recall regresses
  versus the pre-change baseline or cycle boost variance collapses.
- Reintroducing a model-backed REM port if eval evidence demands it.
- Relaxing the service ports gate for REM-only runs.
