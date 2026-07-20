# Subject keys normalize for dedup and histogram; entity signal deferred

Status: accepted

## Context

A Tier 3 fact carries a `subject` string, set by the extraction model. Two
independent SQL audits over the 25 LongMemEval question databases (COA-349)
found the column degenerate: pooled across 2,653 live facts there were 87
distinct `subject` keys, but `User` (1,478) + `user` (1,063) + `the user` (10)
put **96.2% of all facts in one real-world entity's bucket, case-split into
separate exact-string keys**. Real entities (`Rachel`, `Luna (cat)`,
`AutoCAD LT 2013`) appeared with n=1-3.

Two distinct problems hide under that number:

1. **Case/whitespace fragmentation of the dedup key.** The candidate dedup gate
   `_nearest_candidate` (`src/vexic/storage/candidates.py`) selected the
   merge-eligible set with byte-exact `WHERE c.subject = ?` -- no `lower`,
   `trim`, or `COLLATE`. `User` and `user` were therefore disjoint merge
   buckets, so an incoming fact about the same entity under a different casing
   inserted a fragmented duplicate instead of reinforcing the existing
   candidate. The same raw equality drove the histogram: `_subject_counts`
   (`src/vexic/longmemeval_analysis.py`) did `GROUP BY subject`, so the audit's
   `distinct_subjects` counted spellings, not entities.

2. **No entity signal at all.** `EXTRACTION_INSTRUCTIONS`
   (`adapters/openrouter_live_adapter.py`) gives the model no guidance on the
   `subject` field and repeatedly frames every fact as "about the user," so the
   model emits `User` for nearly everything. `subject: str` is a bare field with
   no validator (`src/vexic/models.py`, `src/vexic/contract/__init__.py`), and
   there is no separate `entity` concept anywhere in the schema or contract.

Problem 1 is a mechanical keying bug with a cheap, deterministic, testable fix.
Problem 2 is an extraction-quality and possibly schema question -- a settled
boundary the maintainer directs. Conflating them would have coupled a safe
change to a larger decision.

## Decision

**Normalize the subject *key* everywhere subject is used to key or group;
store the subject *value* verbatim. Defer the entity-signal work, with a
recorded direction.**

### Normalization (shipped here)

Subject is stored verbatim in Tier 2 and Tier 3 -- the original casing is the
display value and must survive (Memory Invariant 2; the redaction and render
paths read it). Only the *comparison* normalizes, at both places that key on
subject:

- **Dedup gate** (`_nearest_candidate`): the merge-eligibility predicate is
  `WHERE lower(trim(c.subject)) = lower(trim(?))`, a single SQL fragment applied
  to both operands. Case/whitespace variants of one token share one merge
  bucket; the stored/display value is untouched. This is the only equality use
  of subject as a key -- it also covers `backfill_missing_candidate_embeddings`,
  which reuses the same helper. The predicate is deliberately kept in SQL on
  both sides rather than pushed to a Python helper: SQLite `lower(trim())` is
  ASCII-fold / space-strip, and a Python `.strip().lower()` (Unicode-fold /
  all-whitespace) would silently diverge and reintroduce the split.

- **Histogram group key** (`_subject_counts`): variants fold into one bucket
  keyed by `lower(trim(subject))`, labelled by the most frequent raw variant
  (ties broken lexicographically for determinism). `distinct_subjects` now
  counts entities under case/whitespace variation instead of spellings. This
  is what makes the acceptance artifact "post-change histogram no longer
  case-split" true, and it is unit-tested -- it does not depend on any live
  rerun or on the entity-signal decision below.

**Scope of the fix is exactly case + whitespace of the same token.** SQLite
`lower` is ASCII and `trim` strips only spaces; pipeline-written rows are also
already whitespace-collapsed upstream by `_strip_marker_echo`
(`src/vexic/pipeline.py`). So `User`/`user`/`  User ` collapse, but `the user`
vs `user` does **not** (different tokens), and the 96% mega-bucket itself does
**not** shrink -- both are extraction-driven, not keying artifacts.

### Entity signal (deferred, with a direction)

The maintainer's call on entity keying, recorded rather than left open. Options
considered:

- **A -- Extraction prompt subject guidance.** Teach the model to set `subject`
  to the real named entity when a fact is about one, reserving `User` for
  genuinely user-scoped facts. Prompt-layer only, no contract change. This is
  the only path that reduces the real mega-bucket and addresses the `the user`
  synonym. **Not gate-free:** `EXTRACTION_INSTRUCTIONS` is the pinned
  experimental control for the in-flight COA-414 ablation; editing it
  mid-experiment rebaselines that control, and it is pinned by
  `tests/test_ablate_extraction_prompts.py`.
- **B -- A separate `entity` contract/schema field.** The cleanest long-term
  signal, but the largest: a contract version bump, migration, and model/storage
  changes. No evidence yet justifies that cost.
- **C -- Defer, document.** Ship only the normalizations now.

**Chosen: C for this change.** Pursue **A as a follow-up (COA-419), sequenced
after COA-414 completes** so its ablation control is not disturbed. Keep **B
evidence-gated behind the COA-351 graph investigation**, which is what would
generate the entity-recurrence evidence a dedicated field needs. This change
touches no extraction code, no contract, and no schema.

## Consequences

- `User`/`user` and surrounding-whitespace variants now share one dedup bucket
  and one histogram bucket. Reinforcement stops fragmenting on trivial case
  variation; `distinct_subjects` measures entities under that variation.
- The dedup predicate is non-sargable (a function wraps the column) and there
  is no index on `subject`. This is tolerable because the eligible set is small
  by construction (still gated by category + agent), and no index is added here.
- The real 96% mega-bucket and the `the user` synonym are unchanged by this
  work and require COA-419 (option A) to move. Entity-recurrence measurement --
  a fresh live Light->Deep eval rerun through `longmemeval_analysis.py`,
  provider-budgeted per ADR 0033 -- is deferred to that follow-up, where it
  measures whether the prompt guidance actually reduced the bucket. Under B/C
  it stays explicitly deferred, satisfying the COA-415 acceptance clause's "or
  explicitly deferred" branch.
- Regression coverage: a same-entity case/whitespace variant now merges, and
  distinct subjects with identical vectors still do not over-merge
  (`tests/test_pipeline.py`); the histogram folds case/whitespace variants into
  one labelled bucket (`tests/test_longmemeval_analysis.py`).
