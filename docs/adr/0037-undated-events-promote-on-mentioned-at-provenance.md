# Undated events promote on deterministic mention-time provenance

Status: accepted

## Context

Invariant 11 requires Tier 3 `category="event"` facts to carry `occurred_at`,
and the COA-410 fix made Deep selection skip undated event candidates so one
cannot deadlock the dream cycle. Together they turned undated events into a
permanent Tier 2 sink: in the COA-349 eval databases, 371 of 416 (89%) active
unpromoted candidates were events with `occurred_at` NULL. Knowledge updates
sat stuck in staging while stale Tier 3 facts stayed authoritative (the
`852ce960` eval row: a "$400k mortgage" update never promoted, so the retired
"$350k" figure kept answering).

Two independent audits refuted the obvious shortcut of backfilling mention
time into `occurred_at`: mention time is not event time ("we went to
Yellowstone last month" said in April describes a March event). Fabricating
event time to satisfy the invariant is exactly what Invariant 11 forbids.

Separately, the retrieval windowing fallback for rows without `occurred_at`
was `created_at` - the dream-run wall clock. For replayed or imported
transcripts that is the wrong signal entirely: a fact extracted today from a
year-old conversation windowed as if it entered memory today.

## Decision

**Add `mentioned_at` - a deterministic, derived provenance date - to both
`memory_candidates` and `long_term_memory`, and let events promote on it when
`occurred_at` is unknown.**

Semantics and mechanics:

- `mentioned_at` is the earliest UTC calendar date of the row's source
  messages: parse each cited `messages.timestamp`, take the minimum, emit a
  date-only ISO string (`YYYY-MM-DD`). It is a pure function of
  `source_message_ids` over the append-only Tier 1 log - never model output,
  never fabricated.
- Derivation is fail-soft (`_earliest_date_from_timestamps` in
  `src/vexic/storage/schema.py`): host-supplied message timestamps are stored
  unvalidated, so blank or unparseable values are skipped and a row whose
  sources are all missing or unparseable derives NULL. A raise here would
  abort a Light batch (against the ADR 0031 fail-soft posture) or brick
  `init_db` via the ensure backfill.
- Computed at candidate insert; recomputed over the merged source-id union on
  merge, where a NULL recompute never clobbers a known date
  (`COALESCE(NULLIF(?, ''), mentioned_at)` - the inverse order from
  `occurred_at`, whose existing extracted value always wins).
- Legacy rows heal via a batched backfill inside the schema ensure functions
  (the `last_seen_at` precedent): rows with `mentioned_at` NULL are derived
  from message timestamps on the next `init_db`. No separate migration.
- The promotion guard (Invariant 11, `src/vexic/storage/promotion.py`) now
  accepts an event with `occurred_at` **or** `mentioned_at`, and still fails
  loud with neither. `occurred_at` stays event-time-only; the columns are
  never cross-assigned.
- Deep selection keeps a residual skip for events with neither date - the
  COA-410 no-deadlock property - now covering only legacy rows not yet healed
  and rows whose sources are missing or unparseable.
- The retrieval windowing fallback becomes a three-rung ladder at every
  `as_of`/`event_after`/`event_before` site on both tiers, and in the
  event-timeline sort:
  `COALESCE(NULLIF(occurred_at, ''), NULLIF(mentioned_at, ''), created_at)`.
- The contract `LongTermFact` gains an optional `mentioned_at` field; the
  extraction model (`FactCandidate` in `src/vexic/models.py`) deliberately
  does not - the extractor cannot be asked for a value the system derives.

Options rejected: promoting undated events with no date at all (loses the
temporal signal windowing needs and weakens the invariant for nothing), and
reclassifying mislabeled events as the primary fix (only ~15% of the sinked
population is mislabeled; genuine undated events would stay stuck, and no
category-mutation machinery exists).

## Consequences

- Undated events escape the Tier 2 sink with honest provenance dating; an
  `852ce960`-shaped update reaches Tier 3 and supersession retires the stale
  fact (pinned end-to-end in `tests/test_memory_reliability.py`,
  `KnowledgeUpdateSupersessionTests`).
- **Retroactive-dating semantic shift, all categories.** The windowing ladder
  carries no category predicate: any row with resolvable mention time now
  windows by it instead of ingest-time `created_at`. "Memory state at T"
  becomes "mentioned at or before T". This is deliberate - mention time is
  the honest upper bound for when knowledge entered the log, and `created_at`
  was simply wrong for replayed transcripts.
- Same-day boundary loosening: a date-only `mentioned_at` passes any `<=`
  cutoff on its own day, per the documented partial-precision comparison
  convention. Callers already had to pass bounds in a comparable shape.
- Legacy databases heal on the next `init_db`; long-lived hosted containers
  heal on restart (the init memo runs the ensure once per process). Rows
  written by an older writer during a rolling-deploy overlap stay NULL until
  then - transient, and the residual Deep filter keeps it deadlock-free.
- Underivable rows (sources purged, timestamps garbage) keep `mentioned_at`
  NULL and are rescanned by the backfill on each init - a benign no-op read.
- Additive contract field, no `CONTRACT_VERSION` bump (the `occurred_at`
  precedent). `export_scope` payloads omit `occurred_at` today and equally
  omit `mentioned_at`; stated here deliberately rather than inherited
  silently.
- Invariant 11 is amended in `AGENTS.md`, not bypassed: events must carry
  `occurred_at` or, failing that, `mentioned_at`; fabricating either remains
  forbidden.
