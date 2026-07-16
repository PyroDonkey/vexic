# Light drops miscited candidates instead of failing the batch

Status: accepted

## Context

Memory invariant 5 requires every durable fact to carry real provenance:
`source_message_ids` that refer to messages actually in the rendered window.
Light enforced this with `validate_candidate_source_ids`, which raised
`ValueError` on the first candidate citing an out-of-window id (or citing
none). The raise aborted the whole Light run.

That policy made a single model slip catastrophic. A failing phase stops its
chain (ADR 0030), so one miscited candidate would discard every good candidate
in the batch, hold the watermark, and prevent REM and Deep from running at all.

### Correction: the production failures were not this

This ADR originally attributed six hosted Light `ValueError` failures
(2026-07-10, 2026-07-12) to the guard. **That attribution was wrong**, and it is
recorded here rather than quietly deleted because the reasoning error is the
instructive part.

The guard raises `ValueError`; the failures were `ValueError`; the type matched
and the cause was assumed. But `ValueError` is ambiguous in this codebase: the
managed libSQL backend raises a *bare* `ValueError` for every server-side SQL
error (ADR 0019). The real cause was concurrent dreaming across overlapping
containers during a rolling deploy, addressed in ADR 0032. Two pieces of
evidence were available the whole time and went unread: not one of the failures
wrote a `dream_runs` error row (the error-recording write was colliding too,
which a validation raise would never do), and every failure landed inside a
deploy window.

The decision below stands on the policy's blast radius, which is real and
independent. It does not stand on that incident.

## Decision

Enforce invariant 5 **per candidate, not per batch**.
`keep_candidates_with_valid_source_ids` returns the candidates whose
`source_message_ids` sit inside the rendered window, plus a count of those
dropped. Light persists the survivors, commits the watermark, and continues the
chain.

- Provenance stays airtight. A candidate with empty or out-of-window
  `source_message_ids` still never reaches Tier 2, so no fact can be promoted
  to Tier 3 without real provenance. The invariant is unchanged; only the
  blast radius of enforcing it is.
- A run where every candidate is dropped still completes and advances the
  watermark. Holding it would re-extract the same window on every tick, paying
  the model repeatedly for a batch that can never land, and would halt the
  chain behind it.
- The dropped count is reported content-free
  (`Light phase: 50 messages -> 6 extracted candidates (1 dropped:
  source_message_ids missing or outside the window).`). Neither `fact_text` nor the
  offending message ids reach shared logs or `dream_runs.error_detail`; a
  miscited id is itself tenant-derived.

## Consequences

- One model miscitation costs one candidate, not a night of dreaming. Light,
  REM, and Deep complete, and Tier 3 keeps advancing.
- Silent-drop risk is real and accepted: an extraction model that miscites
  systematically would now quietly persist fewer candidates rather than failing
  loudly.
  The dropped count in the phase log is the signal; a persistently nonzero
  count means the extraction prompt or model needs attention.

  **Amendment (2026-07-16): the drop count is durable.** A stdout-only signal
  is not queryable and nothing can alert on it -- the same silence class that
  hid the dreaming incident behind ADR 0032. Every Light cycle now persists
  the count in `dream_runs.candidates_dropped` (count only, still content-free),
  and a run that extracted candidates but kept none records
  `status = 'partial'` instead of `'ok'` and surfaces `partial` through
  `RunDreamPhaseResult`. Watermark reads treat `'partial'` like `'ok'`
  (`get_watermark` and the commit-time compare-and-set), so an all-dropped run
  still advances the watermark exactly as decided above.
- `validate_candidate_source_ids` is gone. It was module-internal to
  `vexic.pipeline` (never part of the `MemoryService` contract), so the public
  contract surface is unchanged.
