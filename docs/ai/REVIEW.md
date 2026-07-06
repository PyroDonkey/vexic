# Kilo review policy

Internal tooling doc for review agents and maintainers; not public product
documentation.

This policy was formerly the repo-root `REVIEW.md`. It is preserved here for
manual review-agent configuration and Vexic-specific review calibration. Prefer
these domain rules over built-in guidance where they differ; Kilo's hard safety,
read-only, diff-line, duplicate, and output-format constraints still win.

## What Vexic is

Python 3.13, managed with `uv`. Core is `src/vexic`: an executable public
contract (`contract/__init__.py`), a local SQLite reference service
(`LocalMemoryService`), storage tiers under `storage/**`, host-supplied ports
(`ports.py`), and the promotion/redaction pipeline. Hosted
FastAPI + MCP adapters (`hosted*.py`, `*_http.py`, `*_mcp.py`) are multi-tenant
and read-only by default. `adapters/` (repo root) is host-owned provider wiring:
in review scope, but never package core. Console and website (formerly
`console/`, `website/`) live in the private `PyroDonkey/vexic-website` repo,
not here (ADR 0012 addendum). Decisions live in `docs/adr/` (index is
canonical); behavior of record is `src/vexic` + `tests/`, and prose docs are
downstream.

## Review style

Balanced overall; strict on the memory core and hosted/security surfaces;
lenient on docs. Vexic's real risk is semantic
corruption of memory (wrong tier, lost provenance, cross-tenant leakage), not
typical web-app bugs, so review for invariants, not style. Priority: (1)
memory-correctness invariants, (2) tenant scope + redaction + secrets, (3)
architecture boundaries, (4) tests for changed behavior, (5) provenance/doc
drift, (6) style. CI runs doc-drift checking plus `uv run pytest`; the reviewer
is still the gate for contract drift, type correctness, and behavior that tests
do not cover. Do not block on formatting.

A destructive fix is usually the wrong fix here: prefer supersede / retire in
place, rebuild a projection, or mark data unverified over deleting or updating
canonical rows. Mutating canonical history can be a critical bug even when it is
simpler and the tests pass.

## Severity calibration

**CRITICAL** - any path returning rows outside the validated `MemoryScope`
(cross-tenant/scope leak); redaction bypass (forbidden values reach persistence
or egress); Tier-1 `messages` update/delete; a durable Tier-3 fact without
`source_message_ids`; `expand_history` or any privileged egress without
`MemoryCapability.EXPAND_HISTORY`, redaction, or the message/char caps; an
MCP/hosted surface registering writes/export/delete/rebuild/admin by default
(defaults are `recall_conversation_history` + `recall_user_memory` only); provider secrets
or API keys committed, logged, or written into client/`.mcp.json` config; SQL
built by string interpolation in a storage adapter.

**HIGH** - treating rebuildable FTS/vector projections as source of truth; core
reading provider secrets/env or wiring provider SDKs instead of host ports (a
missing port must fail closed with `HostPortNotConfigured` via
`missing_host_port`; `run_dream_phase` is host-port-only); a contract change in
`contract/__init__.py` without a matching `CONTRACT_VERSION` bump or tests;
storage correct on SQLite but broken on libSQL/Turso - cursor lifecycle,
iteration after a caught exception, or transaction semantics (ADR 0019); a
missing capability check on a scoped operation; a fact or candidate destroyed
instead of retired.

**MEDIUM** - boundary leak (`engine.*` import; private source-host names; Node
package files at repo root; Console/website logic reintroduced into `src/vexic`
or this repository; Vexic schema init
creating or owning host-extension tables such as `background_tool_audit`);
closed-category vocabulary violated; a new operation/contract path with no test;
an ADR file added or renamed without updating `docs/adr/README.md`, or
service-surface prose diverging from `service.py`; Tier-2 fallback surfaced as
durable.

**LOW** - naming, non-ASCII in generated docs, docstring/comment drift, local
duplication, speculative generalization or unused config knobs (repo prefers
stdlib or a one-line solution over a new abstraction).

## Domain invariants (enforce these)

1. Tier-1 `messages` is append-only; never update/delete. Store only cleaned
   replayable text - no prompt payloads, thinking, tool calls, or tool returns
   in searchable transcript.
2. FTS and vector tables are rebuildable projections, never source of truth.
3. Tier-2 `memory_candidates` is staging (reinforce/promote/retire/stale), not
   casually deleted; fallback surfaces as unverified notes, never durable facts.
4. Tier-3 `long_term_memory` facts are durable and each carries
   `source_message_ids`; supersession is non-destructive (retire in place).
5. Redaction fails closed on both writes and privileged egress.
6. Tenant isolation = the opened SQLite context + `MemoryScope` validation, not
   caller goodwill; no shared-storage assumptions without an explicit storage
   ADR.
7. Category vocabulary is closed: `preference, fact, goal, event, relationship,
   skill, constraint, context`.
8. Host ports own all LLM/provider work (`ports.py`); core never reads provider
   secrets or wires provider SDKs.
9. `contract/__init__.py` is the executable contract source of truth (version,
   `MemoryScope`, `MemoryCapability`, models, redaction, `MemoryService`);
   markdown follows code.
10. No private tracker / source-host identifiers (e.g. Linear/COA issue IDs,
    internal hostnames, private-repo or source-host URLs) in `src/vexic`,
    `tests`, schema values, public contract fields, or
    `docs/architecture.md` / `docs/hosted-mvp.md` /
    `docs/memory-service-contract.md`; allowed only in provenance, ADR, runbook,
    or README pointers.

## Files to skip

Suppress findings on generated/vendored files: `uv.lock`,
`node_modules/`, `.venv/`, `build/`, `dist/`,
`*.egg-info/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.next/`, the
generated `pypi/` tree, and any `*.db` / `*.sqlite*` (e.g. `memory.db`). Still
review `pyproject.toml`, `adapters/**`, workflow YAML,
and any intentional dependency change (spot-check new deps for supply-chain
risk).

## Verification expectations

- Primary gate: `uv run pytest`. Memory-reliability changes also run
  `tests/test_memory_reliability.py`; storage runs `test_storage_conformance.py`
  plus `test_storage_*`; boundary runs `test_public_boundary.py`,
  `test_package_manager_policy.py`, `test_schema_ownership.py`.
- Do not run the live provider smoke (`vexic.live_retrieval_baseline`); it is
  opt-in behind `--allow-live` and `OPENROUTER_API_KEY`. Out of scope.
- Hook awareness: Claude runtime hooks are local-only and are not committed.
  `scripts/check_doc_drift.py --ci` is the committed ADR-index and
  service-surface parity check. Verify any change weakening local guardrails
  manually.
- `expand_history` has no dedicated audit hook on the local stdio path yet;
  treat any change that widens that egress as high-risk until audit coverage
  exists.
- Confirm changed contract fields and operation semantics against
  `src/vexic/contract` and the tests, not prose docs.

## Summary style

ASCII, concise, grouped by severity (Critical to Low). Lead with a one-line
impact header naming touched invariants (e.g. `Touches: Tier-1 append-only,
redaction`). Cite `path:line`. Map each finding to an invariant number or ADR id
when one applies, and state the concrete failure (input to wrong result), not
vibes. Stay tracker-neutral: never invent Linear/COA IDs. Note skipped files and
the pytest status; if this file was truncated at 10k characters, say so.

## Sub-agent usage

Estimate changed files and changed lines, then pick the largest tier either
signal triggers. Sub-agents are read-only, post no comments, and return `path,
line, severity, rationale, confidence`. The main reviewer verifies every
finding, drops duplicates and low-confidence noise, checks diff-line validity,
and posts.

- **0 sub-agents** - docs-only (not the three restricted docs), ADR-only,
  lockfile/generated-only, formatting-only, or a single-file typo/config change;
  or any change of at most 2 files and under 100 changed lines. For ADR-only
  changes still confirm `docs/adr/README.md` lists every ADR file.
- **1 sub-agent** - 3-5 files or 100-300 changed lines confined to one risky
  area (storage/schema, contract/scope, hosted auth, redaction, or a migration).
- **2-3 sub-agents** - a change spanning a few domains (contract+storage,
  storage+tests, hosted+API); one per seam.
- **Full 6 sub-agents** - 6+ files or more than 300 changed lines, or any
  security-sensitive or cross-cutting work; scale toward six as domain spread
  grows and shard by independent domain (never point all six at the same files):
  1. Contract & scope - `contract/__init__.py`, `MemoryScope` /
     `MemoryCapability`, tenant isolation, redaction requirements,
     `CONTRACT_VERSION`.
  2. Storage & schema - `storage/**`, `schema.py`, `migration.py`, the
     `connect()` seam, libSQL/Turso dual-backend + cursor/transaction parity,
     projection rebuilds, append-only.
  3. Hosted & MCP - `hosted*.py`, `*_http.py`, `*_mcp.py`, Bearer + `X-Vexic-*`
     scope binding, default read-only registration, `expand_history` gating.
  4. Memory pipeline - `pipeline.py`, `promotion*`, `candidates.py`,
     `longterm.py`, `rem.py`, `deep.py`; provenance, supersession, redaction
     fail-closed.
  5. Recorders & host ports - `recorders/**`, `ports.py`, `adapters/**`; no
     provider secrets in core, `HostPortNotConfigured` behavior,
     cleaned-transcript rules.
  6. Tests, docs & boundary - `tests/**` coverage of changed behavior, ADR/doc
     drift, provenance-ID scan, root-package boundary.

Use fewer than six when the diff touches fewer domains.
