# Light render carries transient observed time

Status: accepted

## Context

Light extraction renders a window of Tier 1 messages into a plain-text
transcript and asks the extraction model to populate `FactCandidate.fact_text`
and, for temporal facts, `occurred_at`. The prompt gave the model no signal
for *when* each line was said, so the model had no honest basis for resolving
a relative reference ("last Sunday", "a few months ago") and no way to catch
its own fabricated absolute dates. `occurred_at` fabrication -- a plausible
but ungrounded ISO date, most often a far-off year the transcript never
states -- was observed in live extraction (COA-412).

Two things were missing to fix this without weakening any existing invariant:

- A grounded time reference in the prompt itself, so the model can resolve
  relative language deterministically instead of guessing.
- A deterministic backstop that does not depend on the model behaving, since
  a prompt instruction alone is not enforcement.

`load_messages_since` (`src/vexic/storage/transcript.py`) already reads each
message's stored `timestamp` column; it just was not being returned to the
caller. `render_transcript` (`src/vexic/pipeline.py`) already tags every
rendered line with `[message_id=N]` for source-id evidence; that marker was
the natural place to carry the added time reference without inventing a
second annotation channel.

## Decision

**`load_messages_since` returns `(id, timestamp, msg)` triples, and
`render_transcript` labels each rendered line's marker with the message's own
observed date and weekday: `[message_id=N observed=YYYY-MM-DD Day]`.**

- **Day precision only.** `_observed_label` parses `timestamp[:10]` with
  `date.fromisoformat` and emits the date part alone -- no time-of-day.
- **Weekday is computed in code, never by the model.** `_observed_label` looks
  up `observed.weekday()` in a fixed `_WEEKDAY_ABBR` tuple
  (`Mon`..`Sun`) rather than asking the model to do calendar arithmetic, which
  is exactly the kind of task LLMs get wrong silently.
- **Fail-soft, not fail-closed.** A missing or malformed timestamp (empty
  string, non-ISO value) makes `_observed_label` return `""`, so the line
  renders as the unlabeled `[message_id=N]` marker it always had. A dating gap
  degrades the prompt signal for that one line; it never drops the message,
  blocks the batch, or raises. Host-supplied message timestamps are stored
  unvalidated (the same posture as the `mentioned_at` derivation in ADR 0037),
  so this path has to tolerate garbage input by construction.
- **Per-message, not per-session.** `run_light_phase` calls
  `load_messages_since` with `after_id`/`limit` (`LIGHT_PHASE_BATCH_SIZE`)
  ordered by row id across an agent's whole message log, filtered only by
  `agent_id` and an `onboarding:` session-prefix exclusion -- it does not stop
  at a session boundary. A single Light window routinely spans multiple
  sessions recorded on different days, so one window-level "observed" value
  would be wrong for most of the lines in it. Labeling the marker each line
  already carries is the only granularity that stays correct through that
  batching shape.

Extraction is then asked to use the label without misusing it
(`adapters/openrouter_live_adapter.py`, `EXTRACTION_INSTRUCTIONS`):

- Observed time is recording time, not event time. The prompt states this
  explicitly and forbids copying an `observed=` date into `occurred_at` on its
  own -- only a temporal reference stated or resolvable in the transcript text
  earns an `occurred_at` value.
- An absolute in-text date copies at exactly its stated precision (full date,
  year-month, or year); a relative reference resolves against the observed
  date of the line that states it, and only when the resolution is
  unambiguous, at no more precision than the resolution supports.
- No defaulting: no invented day, month, or year, and no defaulting a missing
  component to `01`. Less precision or null wins over a guess.

### Compatibility with Invariant 2 and ADR 0034

The `observed=` label is transient prompt scaffolding, produced fresh on every
Light run from data already in Tier 1 and discarded after the extraction call
returns. It is never written to `messages`, never indexed by FTS or the
vector table, and never appears in replay output -- `render_transcript`'s
output is consumed only as the extraction agent's input string, the same
treatment `[message_id=N]` markers already received before this change.
Memory Invariant 2 (stored transcript is the cleaned, replayable conversation
log; prompt payloads and dynamic instructions do not belong in searchable
transcript text) and ADR 0034 (harness-injected scaffolding is filtered, not
ingested) both hold unchanged: this label never enters the boundary either of
them polices, so it needed no dual guard of its own.

### Deterministic guards on `occurred_at`

A prompt instruction is not enforcement, so `occurred_at` fabrication is also
closed off deterministically, independent of whether the model follows the
prompt.

**Validator (`FactCandidate.occurred_at` in `src/vexic/models.py`).** A
`field_validator` in `mode="before"` normalizes any incoming value to a
partial-precision ISO date string or `None`:

- `YYYY`, `YYYY-MM`, or `YYYY-MM-DD` that names a real calendar date passes
  through unchanged; anything that fails calendar validation (e.g. a
  nonexistent day) becomes `None`.
- A datetime-shaped value (rehydration from persisted rows in
  `src/vexic/storage/candidates.py` can surface a legacy
  `"2026-07-05T00:00:00Z"`-style value) truncates to its date part instead of
  being nulled -- truncation only reduces precision, it never invents a
  component, matching the Memory Invariant 11 rule that partial precision is
  allowed but invented components are not.
- Anything else -- junk text, an unparseable string -- becomes `None`.
- The validator never raises and never drops the candidate. A bad date
  degrades to undated, which is the ADR 0037 Tier 2 sink, not a discarded
  fact.

**Pipeline guard (`apply_occurred_at_guards` in `src/vexic/pipeline.py`),**
run in `run_light_phase` after `keep_candidates_with_valid_source_ids`:

1. **Year plausibility.** `_plausible_years` computes the set of years
   grounded in the current Light window: each source message's observed year
   plus its immediate neighbors (year +/- 1), unioned with every bare 4-digit
   year that appears literally in the rendered transcript text. Any candidate's non-null `occurred_at` whose year
   falls outside that set is dropped to `None`. This check runs once before
   the copy-backfill (against a model-supplied date) and once after (against
   a value the copy-backfill just supplied), because `fact_text` is itself
   model output and can carry the same class of fabricated year -- a
   backfilled date gets no exemption a model-supplied date wouldn't get. Every
   non-null `occurred_at` leaving the function has a year in `plausible`.
2. **Event-only copy-backfill.** For a `category="event"` candidate still
   lacking `occurred_at` after the year-plausibility pass,
   `_single_intext_date` scans `fact_text` for absolute dates (ISO full date,
   ISO year-month, or `MonthName [D,] YYYY`), calendar-validates each match,
   and copies the one surviving date at its stated precision only when
   exactly one absolute date is present in the text. A calendar-invalid match
   (e.g. "February 30, 2023") still counts toward the exactly-one-match rule
   -- it disqualifies the copy by making the count ambiguous -- but is never
   itself returned as a value, so an invalid date can silence a backfill it
   can never produce.

Fabricated or ungrounded years degrade the candidate to undated rather than
blocking promotion or the batch; the guard is a deterministic filter, not
another chance to invent a value.

## Consequences

- Extraction gets a grounded, deterministic time reference on every rendered
  line without any new persisted column or annotation channel; the cost is
  confined to the transient prompt string.
- The two enforcement layers are independent of the model: even if
  `EXTRACTION_INSTRUCTIONS` is edited carelessly later, the validator and
  `apply_occurred_at_guards` still reject a year with no grounding in the
  window and still refuse to copy an ambiguous in-text date.
- `render_transcript`, `rendered_message_ids`, and `load_messages_since` all
  changed their tuple shape (`(id, msg)` to `(id, timestamp, msg)` /
  `(id, timestamp, msg)` rows). Both callers in this repo
  (`src/vexic/pipeline.py`) were updated in the same change; any other caller
  of these functions needs the same shape update.
- `scripts/ablate_light_time_context.py` is the maintained evidence harness
  for this decision: a repeated, opt-in ablation runner that replays the
  *exact* persisted Light windows of one or more LongMemEval Vexic databases
  through the current (`observed=`-labeled, guarded) and prior (unlabeled,
  unguarded) extraction shapes, and reports raw vs. guarded fabrication-rate
  metrics per repeat. Per ADR 0033, this document records the recipe -- the
  script and its `--allow-live` gate -- not a run result; `docs/ai/REVIEW.md`
  flags it do-not-run during review, and its deterministic metric functions
  are pinned by `tests/test_ablate_light_time_context.py`.
