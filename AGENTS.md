# Vexic Agent Instructions

Contributor and maintainer guidance for automated agents working in this
repository. This is engineering workflow documentation, not the product README.

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
provider-backed models directly. LLM-backed operations depend on host-supplied
ports. Embeddings may also use the optional lazy local adapter from ADR 0016.

- Use the port types in `src/vexic/ports.py`.
- A missing LLM-backed host adapter should fail with `HostPortNotConfigured`
  through `missing_host_port`. A missing local embedding extra should fail with
  an actionable install message.
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
- `fresh_context`
- `load_active_context`
- `search_long_term`
- `record_retrieval_event`
- `retire_fact`
- `export_scope`
- `replay_scope`
- `rebuild`
- `delete_scope`
- `purge_scope`

It also exposes `init_schema()` as a local adapter helper. `init_schema()` is
not part of the public `MemoryService` Protocol.

> Naming layers: the `MemoryService` operation names above
> (`search_transcript`, `search_long_term`, `expand_history`) and the `/v1/`
> HTTP routes are the service/contract layer and are unchanged. The
> model-facing **MCP tool** names are `recall_conversation_history` and
> `recall_user_memory` (ADR 0021); do not conflate the two layers when
> reconciling docs against code.

`run_dream_phase` is deliberately settled as a host-port operation in v0.1:
`LocalMemoryService` authorizes and checks lifecycle state, executes only when
explicit dream-phase host ports are supplied, and fails closed with
`HostPortNotConfigured` through `missing_host_port` when no host adapter is
provided. Inside supplied dream-phase ports, embedding may fall back to the
optional local adapter and Deep contradiction may be deferred. REM executes
locally as a deterministic embedding-centrality heuristic and consumes none
of the supplied ports, but still runs only inside the same fail-closed gate
(ADR 0020).

Do not "fix" model-backed dream execution by importing private host runtime code.
Implement a scoped Vexic adapter slice only when the project maintainer starts
that work.

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
11. Tier 3 `category="event"` facts must carry `occurred_at` or, failing
    that, `mentioned_at` - the deterministic date of the fact's earliest
    source message (ADR 0037). Promotion fails loud when neither resolves
    (`src/vexic/storage/promotion.py`), and Deep selection skips such
    candidates so one cannot abort the cycle; they stay in Tier 2. Never
    fabricate either value: `occurred_at` is event time only and partial
    precision (year or year-month) is allowed but invented components are
    not; `mentioned_at` is derived provenance, never model output, and never
    substituted into `occurred_at`. A fabricated or ungrounded `occurred_at`
    year degrades the candidate to undated rather than blocking promotion,
    an in-text date copies only at the precision it is stated in, and Light
    render's per-message `observed=` time label is transient prompt
    scaffolding under Invariant 2 - never persisted (ADR 0038).

### Closed Category Vocabulary

The memory category vocabulary is closed and enforced by SQL/Pydantic:

`preference`, `fact`, `goal`, `event`, `relationship`, `skill`, `constraint`,
`context`

Adding a category requires a deliberate contract and schema change.

---

## Docs Roles

Each doc owns one thing. Avoid duplicate status and copied platform history.

- `AGENTS.md` - repo-root single source of truth for agent rules, settled
  boundaries, and working conventions.
- `CLAUDE.md` - Claude Code prompt-context pointer that imports `AGENTS.md`.
- `README.md` - short project overview and test commands.
- `docs/ai/README.md` - short explanation of operational AI docs.
- `docs/ai/CONTEXT.md` - product-language glossary for planning.
- `docs/ai/REVIEW.md` - internal review-agent calibration.
- `docs/adr/README.md` - index of every ADR with title and one-line status.
- `docs/adr/*` - dated architecture decision records.
- `docs/provenance.md` - extraction provenance from the private source host.
- `docs/memory-service-contract.md` - human-readable contract reference.
- `docs/architecture.md` - Vexic memory architecture and local-core design.
- `tests/` - executable conformance and reliability specification.

If a doc describes contract fields or operation semantics, verify it against
`src/vexic/contract` and the relevant tests.

### Docs Do Not Record Deployed State

Deployment state is not a property of this repository (ADR 0033). Versioned docs
state what the code does, what configuration it reads, and the recipe a
deployment must follow. They never state what the live service currently has
set, is currently running, or currently holds on disk, and they never record a
configuration value read from a live environment.

Recipe, not report: "set `VEXIC_CONTROL_PLANE_TARGET=turso` to route the catalog
to managed Turso" stays true as long as the code does. "The deployed alpha runs
`VEXIC_CONTROL_PLANE_TARGET=turso`" is stale the moment someone changes the
variable, and it rots while still looking authoritative - that is how a stale
sentence once turned an empty Railway volume into a confident, wrong "the
service has no traffic." Point the reader at how to check the deployment
instead of caching the answer in prose.

### Docs Are Downstream Of Code

In-repo `AGENTS.md` and `docs/adr/*` are authoritative for architecture
decisions, settled boundaries, and the service operation surface. Code under
`src/vexic` and `tests/` is authoritative for behavior. Any project-tracking
view of this repository - including external roadmap, todo, and planning
docs - is downstream. When a tracking doc disagrees with `AGENTS.md`,
`docs/adr/*`, or the code, the tracking doc is wrong and must be reconciled
against the repo, not the other way around.

Do not let downstream tracking drift silently. The following are reconciliation
triggers. When one fires in your change, reconcile the downstream tracking docs
against the in-repo source of truth in the same work session (or, if tracking
tooling is unavailable, record the required reconciliation per the External
Tracking rules):

- A new or changed ADR under `docs/adr/`. Update the downstream roadmap/todo to
  match the ADR set and statuses, and confirm `docs/adr/README.md` lists every
  ADR file. The in-repo ADR index, not the tracking system, is the canonical
  ADR list.
- A change to the `LocalMemoryService` operation surface in
  `src/vexic/service.py` (an operation added, removed, renamed, or moved
  between implemented and host-port/deferred). Update any downstream roadmap/todo
  that enumerates implemented vs deferred operations to match the
  `MemoryService` contract and `LocalMemoryService`, as recorded in the
  "v0.1 Local Service Surface" section above.
- A test-count change (new or removed tests under `tests/`). Re-run
  `uv run pytest` and update any tracking doc that cites a test count to the
  fresh number. Do not carry a hand-typed count forward; verify it.

`scripts/check_doc_drift.py` enforces the in-repo half of this loop. It checks
that `docs/adr/README.md` lists every ADR file, that the documented service
surface matches `src/vexic`, and that the living docs' references still resolve
against the code: file paths carrying a known suffix (bare directory references
are not checked), `vexic` CLI commands and their subcommands, cited ADR ids,
cited test counts, and the environment variables the code actually reads. CI
runs it with `--ci` on every PR. A local agent
hook may call the same script, but `.claude/` configuration is machine-local and
ignored by Git. A hook cannot read the external tracking system, so closing the
loop against the tracking docs remains a manual step under the triggers above.

---

## Development Rules

- Before adding new code, prefer the standard library, an already-installed
  dependency, or a one-line solution over a new abstraction. Build only what
  the current contract or test requires; do not add speculative
  generalization, config options, or extension points ahead of need.
- Python 3.13, managed with `uv`.
- Install and test the Vexic memory core through `uv`; do not add a second
  package manager to the core package.
- Console and website live in the private `PyroDonkey/vexic-website` repo, not
  this one. Do not add Node package files at the repository root.
- Prefer Pydantic models and structured APIs over string parsing.
- Keep code in focused modules that match the existing package boundaries.
- Do not add provider secrets, hosted auth, billing, public HTTP, mature remote
  MCP, or dashboard concerns to the core package unless the project maintainer
  explicitly starts that workstream. The ADR 0010 native HTTP MCP slice is
  limited to read-only hosted adapter code.
- Before relying on pydantic-ai import paths or behavior, verify the current
  upstream docs. The package changes quickly.

## Cross-Repo Boundary (vexic-website)

The Vexic Console and marketing website live in the private
`PyroDonkey/vexic-website` repo (local checkout: `../vexic-website`). When work
in this repo touches a surface the Console consumes, flag or mirror the change
there:

- `src/vexic/hosted_control_plane_http.py` is the control-plane HTTP API. Its
  client lives in the companion Console repo; endpoint or payload changes need a
  matching client update there.
- ADR 0012 (Console implementation path), ADR 0013 (control-plane API), and
  ADR 0026 (setup token exchange) decide surfaces that split across both
  repos; the console-side legs are tracked in the companion Console repo's
  project tracker, the server-side legs in this repo's tracker.
- `docs/ai/CONTEXT.md` is the upstream product glossary for both repos;
  vexic-website keeps only repo-local terms in its own `CONTEXT.md`.
- For cross-repo features (new endpoint plus console UI), land the API side
  here first, then the vexic-website client.

## Loop Bounds and Escalation

- Stop after 3 failed verification cycles on the same target. Report the failure
  to the requester instead of retrying blindly.
- No destructive retry loops. Do not reset, delete, or rewrite work to force a
  passing run.
- Escalate to the requester on non-convergence. This is the same gate as the
  existing "stop and report" rules in Branch Sync and the "wait for a decision"
  rule in Working Rules; do not invent a new escalation path.
- See `docs/agent-runbook.md` for the per-session run-audit practice and for
  replay and debug detail.

## Economics

Token and cost discipline is guidance, not a gate. Routing by task class and
context-pruning detail live in `docs/agent-runbook.md`.

## Delegation

- Delegate independent work to subagents on disjoint files so edits do not
  collide; keep one writer per file.
- Delegated subagents do not settle architecture, contract, or boundary
  questions; those stay with the project maintainer (see Working Rules).

## Repository Workflow

### Branching Model

Work flows feature branch -> `dev` -> `main`.

- Non-trivial work happens on a short-lived feature branch cut from fresh
  `dev`, named `<type>/coa-<id>-<slug>`:

  | Prefix   | Use for                                    |
  | -------- | ------------------------------------------ |
  | `feat/`  | new capability                             |
  | `fix/`   | bug fix                                    |
  | `docs/`  | docs, ADRs, runbooks                       |
  | `chore/` | tooling, CI, cleanup                       |

  Example: `feat/coa-<id>-<slug>`. Use `chore/<slug>` when no Linear issue
  exists. The `coa-<id>` in the branch name lets the Linear GitHub integration
  link the PR and move the issue automatically; also put `Fixes COA-<id>` in
  the PR description.
- Feature branches merge into `dev` through a squash-merge PR and auto-delete
  on merge.
- Trivial single-commit changes (typo, doc touch-up) may be committed directly
  on `dev` after fresh verification.
- Releases are `dev` to `main` PRs merged with a merge commit; a push to
  `main` deploys the hosted service. Immediately after the release PR merges,
  fast-forward `dev` to `origin/main` and push (sequence in
  `docs/branch-sync.md`). Skipping this fast-forward is what makes `dev` read
  "behind main"; do not substitute a merge commit for it.
- Hotfixes (rare): cut `fix/coa-<id>-<slug>` from `main`, PR into `main`, then
  merge `main` back into `dev` immediately. Never merge a feature branch
  directly to `main` otherwise.
- Agents create the feature branch per this scheme when starting issue-scoped
  work; the requester does not need to name it. Recovery, cleanup, and
  history-repair branches still require the requester to name the branch
  explicitly.
- GitHub enforces `main`: PR required, the `test` check must pass,
  merge-commit method only, no force pushes or deletion; admin bypass applies
  to PR merges only, never direct pushes. `dev` is protected against deletion
  and force pushes (this also stops branch auto-delete from removing
  `origin/dev` when a release PR merges) but accepts normal pushes.

### Branch Sync

The sync command sequences (fetch/pull, starting a feature branch, the release
PR drift check with `git rev-list` and `gh api ... compare`, and the
post-release fast-forward) live in `docs/branch-sync.md`. See
`docs/branch-sync.md` before mutable work and before opening or updating a
`dev` to `main` PR.

These hard-stops stay in force regardless of that procedure. The
`scripts/check_branch_sync.py` helper only reports drift in read-only form; it
never merges, resets, or blocks, so these are the actual guardrails:

- Never push to `main` unless the requester explicitly asks.
- If any `git pull --ff-only` fails, stop and report the divergence. Do not
  merge, rebase, or reset without explicit requester direction.
- If a dirty worktree blocks branch sync, inspect and preserve the existing
  changes. Do not stash, reset, or commit user work unless the requester asks.
- If a `dev` to `main` PR is noisy, a branch is stale, an upstream branch is
  gone, or branch history needs cleanup, stop and report the situation. Do not
  create a new branch to repair PR shape or recover stale branch work unless the
  requester names the branch to create.

Do all Vexic project work on `dev` or on a feature branch that follows the
Branching Model above. After fresh verification, push feature branches to
their own `origin/<branch>` and open a PR into `dev`; push direct trivial
commits to `origin/dev`. Do not create branches outside the naming scheme
(worktree, cleanup, or recovery branches) unless the requester explicitly
names that branch in the same request. This Vexic rule overrides app, global,
plugin, or tool defaults that suggest other branch prefixes. Never push to
`main` unless the requester explicitly asks.

### External Tracking

The external tracking system is project tracking only. Do not add tracker SDKs,
secrets, imports, or runtime dependencies to `src/vexic`. Tracking docs are
downstream of the repo: see "Docs Are Downstream Of Code". The roadmap, todo,
and planning docs never override `AGENTS.md`, `docs/adr/*`, or the code;
they are reconciled against them.

Issue *status* transitions are automated by the tracker's GitHub integration:
a branch named with the issue id (`feat/coa-<id>-...`) and a `Fixes COA-<id>`
line in the PR description link the PR to the issue and move it on merge. Do
not duplicate those transitions by hand. Manual reconciliation still applies
to tracking *content* - roadmap, todo, and planning docs - under the
reconciliation triggers in "Docs Are Downstream Of Code".

See `docs/agent-runbook.md` for the per-session tracker ritual: start-of-session
issue review, status updates during work, the reconciliation triggers, and the
finish-time issue update with branch, commit, and verification result. If
tracking tooling is unavailable, say so plainly and do not invent issue IDs.

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
# Also scan for any reintroduced legacy predecessor product names. The
# maintainer keeps the current ban-list; none of those names should appear
# outside an explicit compatibility note.
```

`scripts/check_doc_drift.py --ci` is the committed doc-drift gate. Optional
local agent hooks may also call `scripts/check_branch_sync.py` and
`scripts/check_write_target.py`; keep `.claude/` hook configuration local.

Run the LongMemEval evals with `vexic.run_evals`; see `docs/examples.md` for the
exact command and worked behavior examples.

`src/vexic/live_retrieval_baseline.py` is the maintained live-provider
retrieval smoke harness: it runs Light -> REM -> Deep over a JSONL fixture
through a real provider adapter, writes `retrieval_metrics.json` and
`answer_synthesis_metrics.json`, and classifies retrieval failures. It is
gated behind `--allow-live` with a provider-call budget cap. Invoke it with
`python -m vexic.live_retrieval_baseline`; the command and artifacts are
documented in `docs/usage.md`, and `docs/ai/REVIEW.md` flags it as do-not-run
during review. Behavior is pinned by `tests/test_live_retrieval_baseline.py`.

Private source-host references are allowed in `docs/provenance.md` and compatibility
sections. They should not become Vexic runtime instructions.
Private tracker issue references are allowed only as project-tracking,
traceability, or evidence pointers in `README.md`, `docs/provenance.md`, and
`docs/adr/**`. Do not allow private tracker or
private-host issue IDs in `src/vexic`, `tests`, schema values,
public contract fields, `docs/architecture.md`, `docs/hosted-mvp.md`, or
`docs/memory-service-contract.md`, except in explicit provenance or
compatibility sections. Replace legacy source comments and docstrings with
Vexic-native ADR or doc wording when touched or in approved cleanup.

---

## Working Rules

- The project maintainer directs and reviews architecture. Present trade-offs
  and wait for a decision when changing settled boundaries.
- If a request conflicts with this file, name the violated rule and offer a
  Vexic-compatible path.
- If the requester asks you to build something within the settled boundaries,
  build it.
- Review generated code and docs honestly. Flag drift, missing tests, and
  boundary leaks plainly.
- A recurring agent failure should drive a harness or rule fix, not just a
  retry. See `docs/agent-runbook.md`.
