# WI-1 — Summarize phase core: report

## What was implemented

1. `src/vexic/ports.py` — added `summary_agent_factory: AgentFactory | None = None` to
   `DreamPhasePorts`, placed alongside `extraction_agent_factory` /
   `contradiction_agent_factory`, before `defer_contradiction` (all fields have
   defaults, so field order is safe for existing keyword-only call sites; verified
   no positional construction of `DreamPhasePorts` exists in the repo).

2. `src/vexic/summarize.py` (new) — `run_summarize_phase(db_path, model_group, *, agent_id=None,
   timezone_name="UTC", now_utc=None, secrets=None, summary_agent_factory=None,
   forbidden_secret_values=(), content_codec=None) -> UsageSummary`.
   - Fail-closed: raises `missing_host_port("Session summarization", hint="Provide
     build_summary_agent in the dream-phase adapter.")` when no factory is given.
   - `init_db(db_path, content_codec=content_codec)` — the only place `content_codec`
     threads through, since `session_summaries.py` storage functions don't accept it
     yet (per brief, left untouched; WI-3 finishes codec threading there).
   - Leaf pass (`_run_leaf_pass`): loops `find_session_compaction_span` ->
     `render_compaction_source` -> `agent.run(source)` -> `record_session_summary(kind="leaf", ...)`
     until no span remains, per session.
   - Condense pass (`_run_condense_pass`): re-fetches `fetch_session_summary_frontier`;
     if `len(frontier) > 8` (`CONDENSE_MAX_FRONTIER_LEAVES`) or
     `sum(token_estimate) > TAU_SOFT // 3`, condenses. Design decision: the frontier
     is always a single contiguous message-id run by construction (each leaf starts
     exactly where the previous coverage left off — `find_session_compaction_span`
     never leaves gaps), so "the oldest contiguous run of frontier summaries" from
     the brief is simply the entire current frontier. One condense call replaces
     the whole frontier with one `condensed` row (`replaces_summary_ids` = every
     frontier summary's id).
   - Per-session error isolation: `run_summarize_phase` iterates
     `list_compactable_session_ids` and wraps each session's leaf+condense passes
     in try/except; a failure (agent exception, or a redaction violation raised
     inside `record_session_summary`) increments an error counter, prints a
     content-free message (`type(exc).__name__` only, no exception text — matches
     the `test_dream_error_policy.py` convention used by `pipeline.py`/`rem.py`/
     `deep.py`, though `summarize.py` is not in that test's `DREAM_MODULES` tuple
     since the brief didn't ask to extend it), and moves on to the next session.
     `UsageSummary` is accumulated across successful sessions only (a mid-session
     failure loses that session's usage tally from the return value, though any
     leaf rows already committed before the failure remain persisted — same
     trade-off as the existing dream phases' whole-cycle try/except).
   - No new `dream_runs`-style storage/error table: nothing in the brief or existing
     `session_summaries.py` schema calls for one, so isolation is purely in-process
     (print + continue), not persisted per-session error state.

3. `tests/test_summarize.py` (new) — 5 tests using a fake `AgentFactory`/agent
   (`FakeSummaryAgent`, following the `test_pipeline.py` `SimpleNamespace` pattern):
   - `test_fails_closed_without_summary_agent_factory`
   - `test_leaf_pass_writes_leaf_rows_and_terminates`
   - `test_condense_pass_triggers_on_frontier_leaf_count`
   - `test_redaction_failure_records_error_and_continues_other_sessions`
   - `test_per_session_error_isolation`

## TDD Evidence

RED — before `src/vexic/summarize.py` existed:

```
$ uv run pytest tests/test_summarize.py -q
...
ImportError while importing test module '.../tests/test_summarize.py'.
E   ModuleNotFoundError: No module named 'vexic.summarize'
1 error in 1.96s
```
Expected: the module under test doesn't exist yet, so collection fails on import —
confirms the tests are wired to the not-yet-built implementation, not vacuously
passing.

Iterating on the condense/redaction/error-isolation tests also surfaced a real
misunderstanding on my first pass (worth recording): I initially assumed
`find_session_compaction_span`'s time-gap boundary fires once per >2h gap. It
actually only reports the *latest* gap boundary in the whole message history
(`_latest_boundary_message_id` never breaks the loop), so a naive "N gaps -> N
leaf spans in one run" test was wrong — the condense test was rewritten to
pre-seed the frontier directly via `record_session_summary` instead of relying
on repeated time-boundary spans, which is both correct and clearer.

GREEN:

```
$ uv run pytest tests/test_summarize.py -q
.....
5 passed in 0.75s
```

Full suite before commit:

```
$ uv run pytest -q
629 passed, 16 skipped, 78 subtests passed in 16.48s
```

## Files changed

- `src/vexic/ports.py` — added `summary_agent_factory` field.
- `src/vexic/summarize.py` — new module.
- `tests/test_summarize.py` — new test module.

## Self-review

- Matches brief signature/behavior exactly; no scope creep into contract/service/
  hosted files or `session_summaries.py`.
- Naming mirrors `pipeline.py`/`deep.py` conventions (`_forbidden_secret_values`,
  `usage.plus(...)`, `missing_host_port`).
- No dead code left in test file (removed an unused `RaisingAgent` stub and a
  dead routing-agent scaffold from an earlier draft of the redaction test).
- Tests exercise real behavior end-to-end against a real sqlite db via `init_db`/
  `save_messages`/`record_session_summary`/`fetch_session_summary_frontier` —
  no mocking of storage, only the model-facing agent is faked.
- Full `pytest` output is clean (no warnings surfaced beyond pre-existing skips).

## Concerns

- The "oldest contiguous run" condense semantics were underspecified in the brief
  beyond "contiguous by message-id ranges." I concluded (and documented in the
  module docstring) that this is always the *entire* current frontier given how
  spans are built with no gaps possible. If a later task changes span-building to
  permit gaps, the condense pass would need revisiting to select a genuine subset.
- Mid-session usage loss on error (a session that fails partway through the leaf
  loop after already committing some leaf rows loses that session's usage
  contribution from the returned `UsageSummary`, though the rows themselves
  persist). This mirrors the existing phases' error-handling shape but is worth
  flagging since summarize's per-session isolation is finer-grained than the
  whole-cycle try/except in `pipeline.py`/`deep.py`/`rem.py`.
- `summarize.py` is not added to `test_dream_error_policy.py`'s `DREAM_MODULES`
  tuple; the brief didn't ask for that file to be touched, and the module already
  independently follows the same content-free error-print convention.
