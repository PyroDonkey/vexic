# AGENTS.md - Vexic

Single source of truth for agents working in this repository.
Plain markdown, no tool-specific syntax.

---

## Project

Vexic is the standalone memory system extracted from a private source host: a
provenance-first, replayable memory core for long-running agents.

The current v0.1 package is a local Python core with:

- public contract models in `src/vexic/contract`
- a local SQLite-backed reference service in `src/vexic/service.py`
- memory storage, retrieval, and Light/REM/Deep primitives under `src/vexic`
- conformance and reliability tests under `tests`

Managed hosted auth stacks, billing, dashboards, public marketplace MCP, mature
remote MCP, and managed operations are out of scope for v0.1. The read-only
local stdio MCP MVP is the local adapter slice. The in-process hosted MVP shell
in `src/vexic/hosted.py` may bind Vexic-scoped API keys to
tenant/project/capability scope for internal staging, and the hosted FastAPI
adapter may expose the narrow read-only native HTTP MCP `/mcp` slice described
in ADR 0010. This is not a public HTTP service or production control plane. The
private source host remains the first-party consumer; see `docs/provenance.md`
for extraction provenance.

---

## Architecture Boundaries

These are settled boundaries. Do not reintroduce private host runtime code
into Vexic.

### Package Boundary

- Vexic code lives under `src/vexic`.
- Repo-local host adapter files under `adapters/` may import public `vexic.*`
  APIs as consumers. They are not Vexic package runtime, and provider-secret or
  live-model wiring belongs there rather than in `src/vexic`.
- Runtime code must not import legacy `engine.*` modules.
- Private source-host paths may appear in provenance or compatibility docs, not as
  operational dependencies.
- `LocalMemoryService` is a reference local adapter, not a hosted service.
- Host-owned extension tables in an existing SQLite database must be preserved.
  Vexic schema initialization must not create or take ownership of private-host
  extension tables such as `background_tool_audit`.

### Host Ports

Vexic core does not read provider secrets, choose model providers, or build live
models directly. Model-backed operations depend on host-supplied ports.

- Use the port types in `src/vexic/ports.py`.
- A missing model-backed host adapter should fail with
  `HostPortNotConfigured` through `missing_host_port`.
- Do not replace host ports with ambient environment reads, provider SDK wiring,
  process globals, or private host runtime imports.

### Contract Source Of Truth

`src/vexic/contract/__init__.py` is the executable public contract source of
truth for:

- `CONTRACT_VERSION`
- `MemoryScope`
- `MemoryCapability`
- request/result models
- redaction requirements
- `MemoryService`

When markdown and code disagree, fix the markdown or the contract deliberately;
do not let them drift.

### v0.1 Local Service Surface

`LocalMemoryService` currently implements these `MemoryService` protocol
operations:

- `append_transcript`
- `ingest_source_transcript`
- `search_transcript`
- `expand_history`
- `search_long_term`
- `record_retrieval_event`
- `retire_fact`
- `export_scope`
- `replay_scope`
- `rebuild`
- `delete_scope`

It also exposes `init_schema()` as a local adapter helper. `init_schema()` is
not part of the public `MemoryService` Protocol.

`run_dream_phase` is deliberately settled as a host-port operation in v0.1:
`LocalMemoryService` authorizes and checks lifecycle state, executes only when
explicit dream-phase host ports are supplied, and fails closed with
`HostPortNotConfigured` through `missing_host_port` when no host adapter is
provided.

Do not "fix" model-backed dream execution by importing private host runtime code.
Implement a scoped Vexic adapter slice only when Ryan asks for that work.

---

## Memory Invariants

These rules define correctness for the memory core.

1. Tier 1 `messages` is append-only. Never update or delete transcript rows.
2. Stored transcript is the cleaned, replayable conversation log. Prompt
   payloads, dynamic instructions, thinking parts, tool calls, and tool returns
   do not belong in searchable transcript text.
3. Rebuildable projections such as FTS and vector tables may be repaired or
   rebuilt. They are not source of truth.
4. Tier 2 `memory_candidates` is staging memory. Candidates are reinforced,
   promoted, retired, or marked stale/review; they are not casually deleted.
5. Tier 3 `long_term_memory` stores durable facts with provenance. Every fact
   must carry `source_message_ids`.
6. Supersession is non-destructive. Retire facts or candidates in place instead
   of deleting canonical rows.
7. Retrieval observations are durable telemetry. Tier 3 retrieval uses
   `retrieval_events`; Tier 2 fallback uses `candidate_retrieval_events`.
8. Candidate fallback is tentative. Surface Tier 2 fallback as unverified notes,
   never as durable facts.
9. Redaction fails closed. Writes and privileged egress must reject configured
   forbidden values before persistence or return.
10. Tenant isolation in the current local core is the opened SQLite database
    context plus `MemoryScope` validation. Do not add shared-storage assumptions
    without an explicit storage decision.

### Closed Category Vocabulary

The memory category vocabulary is closed and enforced by SQL/Pydantic:

`preference`, `fact`, `goal`, `event`, `relationship`, `skill`, `constraint`,
`context`

Adding a category requires a deliberate contract and schema change.

---

## Docs Roles

Each doc owns one thing. Avoid duplicate status and copied platform history.

- `README.md` - short project overview and test commands.
- `AGENTS.md` - agent rules, settled boundaries, and working conventions.
- `docs/adr/README.md` - index of every ADR with title and one-line status.
- `docs/adr/*` - dated architecture decision records.
- `docs/provenance.md` - extraction provenance from the private source host.
- `docs/memory-service-contract.md` - human-readable contract reference.
- `docs/architecture.md` - Vexic memory architecture and local-core design.
- `tests/` - executable conformance and reliability specification.

If a doc describes contract fields or operation semantics, verify it against
`src/vexic/contract` and the relevant tests.

### Docs Are Downstream Of Code

In-repo `AGENTS.md` and `docs/adr/*` are authoritative for architecture
decisions, settled boundaries, and the service operation surface. Code under
`src/vexic` and `tests/` is authoritative for behavior. Any project-tracking
view of this repository - including the Linear roadmap, todo, and planning
docs - is downstream. When a tracking doc disagrees with `AGENTS.md`,
`docs/adr/*`, or the code, the tracking doc is wrong and must be reconciled
against the repo, not the other way around.

Do not let downstream tracking drift silently. The following are reconciliation
triggers. When one fires in your change, reconcile the downstream Linear
roadmap/todo against the in-repo source of truth in the same work session (or,
if Linear tooling is unavailable, record the required reconciliation per the
Linear Tracking rules):

- A new or changed ADR under `docs/adr/`. Update the Linear roadmap/todo to
  match the ADR set and statuses, and confirm `docs/adr/README.md` lists every
  ADR file. The in-repo ADR index, not Linear, is the canonical ADR list.
- A change to the `LocalMemoryService` operation surface in
  `src/vexic/service.py` (an operation added, removed, renamed, or moved
  between implemented and host-port/deferred). Update any Linear roadmap/todo
  that enumerates implemented vs deferred operations to match the
  `MemoryService` contract and `LocalMemoryService`, as recorded in the
  "v0.1 Local Service Surface" section above.
- A test-count change (new or removed tests under `tests/`). Re-run
  `uv run pytest` and update any tracking doc that cites a test count to the
  fresh number. Do not carry a hand-typed count forward; verify it.

`.claude/hooks/check_doc_drift.py` enforces the in-repo half of this loop at
session start: it checks that `docs/adr/README.md` lists every ADR file and
that the documented service surface matches `src/vexic`. A hook cannot read
Linear, so closing the loop against the tracking docs remains a manual step
under the triggers above.

---

## Development Rules

- Python 3.13, managed with `uv`.
- Install and test through `uv`; do not add a second package manager.
- Type annotate new public functions and models.
- Prefer Pydantic models and structured APIs over string parsing.
- Keep code in focused modules that match the existing package boundaries.
- Do not add provider secrets, hosted auth, billing, public HTTP, mature remote
  MCP, or dashboard concerns to the core package unless Ryan explicitly starts
  that workstream. The ADR 0010 native HTTP MCP slice is limited to read-only
  hosted adapter code.
- Before relying on pydantic-ai import paths or behavior, verify the current
  upstream docs. The package changes quickly.
- Keep generated docs ASCII unless an existing file has a clear reason to use
  non-ASCII.

## Loop Bounds and Escalation

- Stop after 3 failed verification cycles on the same target. Report the failure
  to Ryan instead of retrying blindly.
- No destructive retry loops. Do not reset, delete, or rewrite work to force a
  passing run.
- Escalate to Ryan on non-convergence. This is the same gate as the existing
  "stop and report" rules in Branch Sync and the "wait for a decision" rule in
  Working Rules; do not invent a new escalation path.
- See `docs/agent-runbook.md` for the per-session run-audit practice and for
  replay and debug detail.

## Economics

Token and cost discipline. There is no hard token budget enforced yet; this is
guidance, not a gate.

- Route by task class. Routine doc, lint, and test reads can use a cheaper
  model. Architecture, contract, and memory-invariant changes use a frontier
  model.
- Prune stale tool output and large file dumps from context between
  verification cycles. Do not carry an obsolete dump forward.

## Execution Modes

- Conductor mode is real-time interactive work with Ryan in the loop.
- Orchestrator mode is async or delegated multi-agent work.
- When decomposing, delegate independent work to subagents on disjoint files so
  edits do not collide. Keep one writer per file.
- The human-judgment boundary is unchanged: the "Ryan directs and reviews
  architecture" rule in Working Rules is the defer-to-human gate. Delegated
  agents do not settle architecture, contract, or boundary questions.

## Repository Workflow

### Branch Sync

Before mutable work:

1. `git fetch --prune origin`
2. Update `main` with `git switch main` and
   `git pull --ff-only origin main`.
3. Update `dev` with `git switch dev` and
   `git pull --ff-only origin dev`.

If local `dev` is missing but `origin/dev` exists, create the local tracking
branch with `git switch -c dev --track origin/dev`. If `origin/dev` is missing,
create it from updated `main` only when Ryan has explicitly asked to bootstrap
the branch workflow; otherwise stop and ask before creating or pushing it. Do
not continue implementation work on `main`.

If any `git pull --ff-only` fails, stop and report the divergence. Do not merge,
rebase, or reset without Ryan's direction.

Do all Vexic project work on `dev`. After fresh verification, commit on `dev`
and push completed commits to `origin/dev`. Do not create, switch to, commit
on, or push `codex/*`, feature, worktree, cleanup, or recovery branches unless
Ryan explicitly names that branch in the same request. This Vexic rule
overrides app, global, plugin, or tool defaults that suggest branch prefixes or
worktree branches. Never push to `main` unless Ryan explicitly asks.

Before creating or updating a `dev` to `main` PR, re-check branch drift from
fresh remote refs while staying on `dev`: `git fetch origin`, merge
`origin/main` into `dev` if needed, push `origin/dev`, then run
`git rev-list --left-right --count origin/main...dev`. The first number is how
far `dev` is behind `origin/main`, and it must be `0` before opening or
claiming the PR is ready. Also run
`gh api repos/PyroDonkey/vexic/compare/main...dev --jq '{behind_by:.behind_by, files:[.files[].filename]}'`
and stop if GitHub lists unintended files.

The `.claude/hooks/check_branch_sync.py` SessionStart hook mirrors this check
in read-only form. It fetches origin and reports drift; it never merges. If it
reports drift, sync on `dev` with the commands above before starting work.

If a `dev` to `main` PR is noisy, a branch is stale, an upstream branch is gone,
or branch history needs cleanup, stop and report the situation. Do not create a
new branch to repair PR shape or recover stale branch work unless Ryan names the
branch to create.

If a dirty worktree blocks branch sync, inspect and preserve the existing
changes. Do not stash, reset, or commit user work unless Ryan asks.

### Linear Tracking

Linear is project tracking only. Do not add Linear SDKs, secrets, imports, or
runtime dependencies to `src/vexic`. Linear is downstream of the repo: see
"Docs Are Downstream Of Code". The Linear roadmap, todo, and planning docs
never override `AGENTS.md`, `docs/adr/*`, or the code; they are reconciled
against them.

At the start of each work session, review relevant Linear project issues through
the configured Linear connector or MCP tools when tooling and auth are
available. Map the requested work to an existing issue, or create one for
non-trivial plans and changes.

During work, keep the issue status and comments current when scope changes,
blockers, decisions, or follow-up work appear. When a reconciliation trigger
from "Docs Are Downstream Of Code" fires (a new or changed ADR, a change to the
`LocalMemoryService` operation surface, or a test-count change), reconcile the
affected Linear roadmap/todo against the in-repo source of truth before
finishing. At finish, update the issue with the branch, commit, verification
result, and any generated follow-up issues.

If Linear tooling is unavailable, say so plainly and do not invent issue IDs.
Record the reconciliation that the triggers above require - for example, in the
commit message or the session report - so it is not lost.

In this section, fresh verification means the relevant checks from the
Verification section have been run after the final edit.

## Verification

Before claiming code or docs work is complete, run the relevant fresh checks.
For most changes:

```powershell
uv run pytest
```

For memory reliability changes, also run:

```powershell
uv run pytest tests/test_memory_reliability.py
```

For boundary-sensitive changes, inspect these explicitly:

```powershell
rg -n "^(from|import) engine\\." src/vexic tests
rg -n "C[o]alescent|A[g]entOS|Telegram|Blog Writer|teammate|COA-" AGENTS.md docs src/vexic tests
rg -n "Linear" src/vexic tests
```

Alongside the two SessionStart hooks (`.claude/hooks/check_doc_drift.py` and
`.claude/hooks/check_branch_sync.py`), the `.claude/hooks/check_write_target.py`
PreToolUse guard fails closed against Tier-1 `messages` mutation and against
changes to the host-extension `background_tool_audit` table.

Use `scripts/run_evals.py` as the eval runner for the LongMemEval datasets. It
imports `vexic`, so run it with the editable install on path:
`uv run --with-editable . python scripts/run_evals.py --dataset longmemeval_s_smoke.jsonl`.
A bare `uv run python scripts/run_evals.py` fails with `ModuleNotFoundError`.
See `docs/examples.md` for worked examples.

Private source-host references are allowed in `docs/provenance.md` and compatibility
sections. They should not become Vexic runtime instructions.
Linear references are allowed as project-tracking workflow in `AGENTS.md`,
`README.md`, and `docs/provenance.md`. They should not become Vexic runtime
code.

---

## Working Rules

- Ryan directs and reviews architecture. Present trade-offs and wait for a
  decision when changing settled boundaries.
- If a request conflicts with this file, name the violated rule and offer a
  Vexic-compatible path.
- If Ryan asks you to build something within the settled boundaries, build it.
- Review generated code and docs honestly. Flag drift, missing tests, and
  boundary leaks plainly.
- A recurring agent failure should drive a harness or rule fix, not just a
  retry. See `docs/agent-runbook.md`.
