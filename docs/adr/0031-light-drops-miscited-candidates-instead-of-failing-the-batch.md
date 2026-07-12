# Light drops miscited candidates instead of failing the batch

Status: accepted

## Context

Memory invariant 5 requires every durable fact to carry real provenance:
`source_message_ids` that refer to messages actually in the rendered window.
Light enforced this with `validate_candidate_source_ids`, which raised
`ValueError` on the first candidate citing an out-of-window id (or citing
none). The raise aborted the whole Light run.

That policy made a single model slip catastrophic. A failing phase stops its
chain (ADR 0030), so one miscited candidate discarded every good candidate in
the batch, held the watermark, and prevented REM and Deep from running at all.

It fired in production five times in three days (2026-07-10 and 2026-07-12).
The hosted dogfood tenant's shared scope accumulated 15 staged candidates and
promoted **zero** facts: `long_term_memory` sat empty while Light aborted on
each attempt and the chain never reached Deep. The failure was invisible in
operator logs beyond a content-free `ValueError` type name, because the guard
deliberately refuses to echo candidate text.

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
  source_message_ids outside the window).`). Neither `fact_text` nor the
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
- `validate_candidate_source_ids` is gone. It was module-internal to
  `vexic.pipeline` (never part of the `MemoryService` contract), so the public
  contract surface is unchanged.
