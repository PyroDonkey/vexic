# AGENTS.md - Vexic

Single source of truth for agents working in this repository.
Plain markdown, no tool-specific syntax.

---

## Project

Vexic is the standalone memory system extracted from Coalescent: a
provenance-first, replayable memory core for long-running agents.

The current v0.1 package is a local Python core with:

- public contract models in `src/vexic/contract`
- a local SQLite-backed reference service in `src/vexic/service.py`
- memory storage, retrieval, and Light/REM/Deep primitives under `src/vexic`
- conformance and reliability tests under `tests`

Hosted auth, billing, dashboards, public HTTP, remote MCP, and managed
operations are out of scope for v0.1. The read-only local stdio MCP MVP is the
narrow in-scope adapter slice. Coalescent remains the private AgentOS host and
first-party consumer; see `docs/provenance.md` for extraction provenance.

---

## Architecture Boundaries

These are settled boundaries. Do not reintroduce Coalescent host runtime code
into Vexic.

### Package Boundary

- Vexic code lives under `src/vexic`.
- Runtime code must not import `engine.*` from Coalescent.
- Coalescent paths may appear in provenance or compatibility docs, not as
  operational dependencies.
- `LocalMemoryService` is a reference local adapter, not a hosted service.
- Host-owned extension tables in an existing SQLite database must be preserved.
  Vexic schema initialization must not create or take ownership of Coalescent
  extension tables such as `background_tool_audit`.

### Host Ports

Vexic core does not read provider secrets, choose model providers, or build live
models directly. Model-backed operations depend on host-supplied ports.

- Use the port types in `src/vexic/ports.py`.
- A missing model-backed host adapter should fail with
  `HostPortNotConfigured` through `missing_host_port`.
- Do not replace host ports with ambient environment reads, provider SDK wiring,
  process globals, or Coalescent runtime imports.

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
- `search_transcript`
- `expand_history`
- `search_long_term`

It also exposes `init_schema()` as a local adapter helper. `init_schema()` is
not part of the public `MemoryService` Protocol.

The following protocol operations are intentionally deferred or wired elsewhere
in v0.1 and currently raise `NotImplementedError` on `LocalMemoryService`:

- `record_retrieval_event`
- `retire_fact`
- `run_dream_phase`
- `export_scope`
- `replay_scope`
- `rebuild`
- `delete_scope`

Do not "fix" these by importing Coalescent runtime code. Implement a scoped
Vexic adapter slice only when Ryan asks for that work.

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
- `docs/provenance.md` - extraction provenance from Coalescent.
- `docs/memory-service-contract.md` - human-readable contract reference.
- `docs/architecture.md` - Vexic memory architecture and local-core design.
- `tests/` - executable conformance and reliability specification.

If a doc describes contract fields or operation semantics, verify it against
`src/vexic/contract` and the relevant tests.

---

## Development Rules

- Python 3.13, managed with `uv`.
- Install and test through `uv`; do not add a second package manager.
- Type annotate new public functions and models.
- Prefer Pydantic models and structured APIs over string parsing.
- Keep code in focused modules that match the existing package boundaries.
- Do not add provider secrets, hosted auth, billing, public HTTP, remote MCP,
  or dashboard concerns to the core package unless Ryan explicitly starts that
  workstream.
- Before relying on pydantic-ai import paths or behavior, verify the current
  upstream docs. The package changes quickly.
- Keep generated docs ASCII unless an existing file has a clear reason to use
  non-ASCII.

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
runtime dependencies to `src/vexic`.

At the start of each work session, review relevant Linear project issues through
the configured Linear connector or MCP tools when tooling and auth are
available. Map the requested work to an existing issue, or create one for
non-trivial plans and changes.

During work, keep the issue status and comments current when scope changes,
blockers, decisions, or follow-up work appear. At finish, update the issue with
the branch, commit, verification result, and any generated follow-up issues.

If Linear tooling is unavailable, say so plainly and do not invent issue IDs.

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
rg -n "Coalescent|AgentOS|Telegram|Blog Writer|teammate|COA-" AGENTS.md docs src/vexic tests
rg -n "Linear" src/vexic tests
```

Coalescent references are allowed in `docs/provenance.md` and compatibility
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
