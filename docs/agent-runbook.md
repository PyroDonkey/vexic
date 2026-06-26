# Agent Operations Runbook

> Role: how a coding agent should operate inside the Vexic repository during
> its own work sessions.
> Companion: `docs/examples.md` for worked behavior examples.
> Authority: `AGENTS.md` and `docs/adr/*` remain the source of truth for
> architecture boundaries and the human gates referenced below.

## Scope Of This Runbook

This runbook is about the agent's own work sessions in this repository: editing
code and docs, running verification, and committing on `dev`. It is operational
hygiene for the agent, not a product feature.

It is deliberately distinct from Vexic's product behavior. Vexic the product is
a provenance-first, replayable memory core that records customer agent memory:
transcript, candidates, durable facts, and retrieval telemetry. The audit and
replay discipline described here applies to the agent's own actions in the repo,
not to customer memory data. Nothing in this runbook adds, removes, or changes a
`MemoryService` operation, a storage table, or a contract field. Do not confuse
an agent-run record with a Tier 1 transcript row, a `dream_runs` audit row, or a
`retrieval_events` row; those are product tables governed by the Memory
Invariants in `AGENTS.md`.

## Agent-Run Audit Logging

Keep a per-session run record so a session can be reviewed or replayed for
debugging after the fact. This is a practice and a format, not tooling to build.
Do not create a database, hook, or script for it unless Ryan asks. A short
markdown note, the session report, or the commit body is enough.

A run record should capture:

- Session identity: date, branch worked on (normally `dev`), and the requested
  task in one or two sentences.
- Actions taken: the ordered, high-level steps the agent performed.
- Tool calls: the meaningful read and search calls used to orient, and any
  shell commands run, with enough detail to repeat them.
- Files touched: every path created, edited, or deleted, with absolute or
  repo-relative paths.
- Verification commands run plus their results: the exact commands from the
  Verification section of `AGENTS.md` (for example `uv run pytest`) and whether
  they passed, including the observed test count when a tracking doc cites one.
- Decisions escalated to Ryan: any point where the agent stopped at a human
  gate, surfaced a trade-off, or asked before changing a settled boundary, and
  what Ryan decided.

The goal is replayability of the work session: a reviewer should be able to read
the record, re-run the same verification, and understand why each change was
made and where a human made the call.

Keep run records metadata-and-action oriented. Do not paste secrets, provider
keys, or configured forbidden values into a run record, consistent with the
fail-closed redaction posture in `AGENTS.md`.

## Loop Bounds And Escalation

Verification failures are normal; spinning on them is not.

- Cap retries per target. After 3 failed verification cycles against the same
  target (the same failing test, the same file, the same command), stop and
  escalate to Ryan instead of attempting a 4th blind cycle. A "cycle" is one
  edit-plus-verify attempt at fixing that target.
- Never spin in a destructive retry loop. Do not repeatedly reset, force, stash,
  or delete to make a command pass. `AGENTS.md` already forbids stashing,
  resetting, or committing user work without Ryan's direction, and forbids
  destructive branch repair unless Ryan names the branch. A retry loop must not
  smuggle those actions in.
- Escalate on non-convergence. If repeated cycles do not converge, the failure
  is unclear, or the only remaining fixes would cross a settled boundary, stop
  and report. State what was tried, the last command output, and the suspected
  cause.

This mirrors the existing "stop and report" human gates in `AGENTS.md`. Those
gates already require the agent to stop rather than improvise when:

- a `git pull --ff-only` fails and branches have diverged;
- a `dev` to `main` PR is noisy, a branch is stale, an upstream branch is gone,
  or branch history needs cleanup;
- `git rev-list --left-right --count origin/main...dev` shows `dev` is behind,
  or `gh api ... compare/main...dev` lists unintended files;
- a dirty worktree blocks branch sync;
- a request conflicts with a rule in `AGENTS.md`, in which case the agent names
  the violated rule and offers a Vexic-compatible path.

A failed verification loop is the same shape of event. Treat the retry cap as
one more stop-and-report gate.

## Feedback Path

A recurring agent failure is an input to the next iteration, not just something
to retry. When the same class of failure shows up across cycles or sessions, the
fix belongs in the harness or the rules, not only in the working tree.

- If the agent keeps making the same mistake because guidance is missing or
  ambiguous, propose a change to `AGENTS.md` (or the relevant doc) so the next
  session starts with the corrected rule. Editing `AGENTS.md` is a settled-rule
  change; surface it to Ryan rather than rewriting boundaries unilaterally.
- If the failure is a drift that a check could have caught, prefer a hook. The
  repository already enforces parts of this loop at session start:
  `.claude/hooks/check_doc_drift.py` checks that `docs/adr/README.md` lists every
  ADR file and that the documented service surface matches `src/vexic`, and
  `.claude/hooks/check_branch_sync.py` reports `dev`-to-`main` drift in
  read-only form. A new recurring failure mode is a candidate for the same kind
  of guard.
- Close the loop in-session where the triggers fire. `AGENTS.md` lists
  reconciliation triggers (a new or changed ADR, a change to the
  `LocalMemoryService` operation surface, or a test-count change) that must be
  reconciled against the in-repo source of truth in the same work session.

The principle: a failure that recurs should produce a rule or check fix so the
next iteration cannot repeat it, rather than another isolated retry.
