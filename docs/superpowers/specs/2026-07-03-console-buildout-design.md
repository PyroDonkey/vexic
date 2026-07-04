# Vexic Console Build-Out: Operational Depth, Billing Scaffolding, Data Control

Date: 2026-07-03
Status: approved design, pre-implementation

## Summary

The first Vexic Console (ADR 0012) shipped a minimal control-plane client:
project list, project workspace with Keys/Usage/Settings tabs, thin Clerk
settings wrapper, and an internal support view. This design builds the console
out along three axes chosen for this iteration:

1. **Operational depth** — dream job visibility, richer usage analytics, and
   key lifecycle detail.
2. **Billing scaffolding** — plan tiers and limits display with no payment
   processor.
3. **Data control** — retention settings, async export, project memory
   deletion, tenant deletion, and a data-control event history.

Memory visibility (fact browsers, transcript viewers) remains deferred, as in
ADR 0012.

The plan covers both sides of each feature: the Next.js console UI and the
hosted control-plane endpoints (`/control/v1/*`, ADR 0013) each feature
requires. Python adapter work is specified here as a dependency of each slice.

## Structural approach

**Grow in place** (Approach C from brainstorming): keep the current route
structure and add vertical slices to it. No dashboard-shell redesign now.

**Shell trigger note**: when tenant-level navigation destinations exceed ~7 or
project workspace tabs exceed 6, a sidebar-shell refactor becomes its own
ADR'd project. Until that trigger, tabs and the existing header navigation
carry the console.

## Architecture and boundaries

- Console remains a **control-plane client** (ADR 0012). No memory content is
  rendered in console UI. Export delivers raw memory only as a downloaded
  archive produced by an async job — never as browsable pages.
- All new backend work lands in the hosted adapter
  (`vexic.hosted_control_plane_http` plus `hosted_local` stores). No new
  `MemoryService` contract operations, with one flagged exception: retention
  needs an age-scoped purge entry point over the ADR 0022 purge machinery
  (see Retention below).
- Auth model unchanged: Clerk organization gates the console; the Console
  Service Credential gates `/control/v1/*` (ADR 0013 trust split). Destructive
  writes additionally require the acting human to hold the Clerk `org:admin`
  role, enforced in console API routes. Note: the console auth helper does
  not capture the Clerk org role today — adding role capture and the admin
  check is part of the first destructive slice, not existing behavior.
  Tenant deletion additionally gets a server-side two-step confirm token
  (see Tenant deletion) because a console-side check alone cannot gate an
  irreversible wipe.

### Navigation

- `/console` — project list, unchanged, plus a plan badge in the header.
- `/console/projects/[projectId]` — tabs become
  **Keys · Usage · Jobs · Data · Settings**.
- `/console/billing` — new tenant-level page: plan tier, limits vs usage,
  upgrade contact.
- `/console/settings` — gains a **Danger Zone** for tenant/account deletion
  and a tenant-level copy of the data-control event history.
- `/console/support` — unchanged, plus job failure reasons (see Jobs).

## Feature area 1: operational depth

### Jobs tab (new, per project)

- Table of recent dream runs: phase (Light/REM/Deep), started/finished
  timestamps, status (succeeded / failed / running), counts (candidates
  staged, facts promoted).
- **Failure reasons are not shown to customers.** Failed rows show a generic
  "We're looking into it — contact support if this persists." Sanitized
  failure reasons appear only in the internal Support View; the endpoint
  returns reasons only when the caller flags internal support context.
- Header strip: last successful run per phase, and next scheduled run if the
  scheduler exposes it.
- Empty state explains dream phases in customer terms ("Vexic reviews recent
  conversations in the background and promotes durable facts").
- Backend: `GET /control/v1/clerk-orgs/{org}/projects/{id}/jobs?limit=50`.
  Job lifecycle events are recorded by `HostedBackgroundJobRunner`, but
  `HostedJobEvent` and the `hosted_job_events` table carry no `project_id`
  today and the recorder never writes one. This slice includes: adding
  `project_id` to the event model, schema, and `_record_job` (the scope is
  already in hand at record time), plus the per-project store query.
  Historical events predate project attribution and will not appear in
  per-project views; the tab's empty state must not imply "no runs ever."

### Usage tab upgrade

- Time-series chart: operations per day by type (append, search, expand),
  last 30 days. Uses the Tremor chart components already in the repo.
- Cap proximity: per-cap meter with a warning state at 80% and an alert state
  at 95%.
- Per-key attribution table: operations by key id over the period, so a
  customer can see which agent is consuming quota.
- Backend: extend the usage endpoint with `?granularity=day&days=30`
  returning bucketed rows, and add `GET .../usage/by-key`. Usage events do
  **not** carry key identity today: `HostedUsageEvent` has no `key_id`, and
  `principal_id` is set from the key's agent scope — the literal string
  `"shared"` for most keys, so distinct keys collapse into one bucket. This
  slice adds a `key_id` column to usage events, threads it through every
  record site, and aggregates on it. Events recorded before the column
  exists appear as "unattributed" in the by-key view.

### Key lifecycle detail

- `last_used_at` per key in the key list. New column on the key store,
  written at successful key auth. Throttle mechanism: a single
  `UPDATE ... SET last_used_at = now WHERE key_id = ? AND (last_used_at IS
  NULL OR last_used_at < now - 60s)` — the guard lives in the database, not
  in process memory, so it stays correct across restarts and across multiple
  adapter processes if the ADR 0013 process split ever lands.
- Revoked-key history: key list endpoint gains `?include=revoked`. The
  current store query hardcodes revoked-exclusion in its SQL, so this is a
  new store query variant, not a parameter pass-through. Console shows a
  collapsed "Revoked keys" section with revoked-at timestamps.
- Stale-key nudge: badge on keys unused for more than 30 days. Display only —
  no expiry or rotation enforcement.

## Feature area 2: billing scaffolding

No payment processor in this iteration.

- **Plan model**: the control plane gains a `plan` field per tenant
  (`alpha`, `free`, `pro`; enum extensible). Set by operator only — no
  self-serve plan changes. A plan defines a cap bundle: operation quotas per
  day, maximum projects, maximum keys, and a retention floor if a tier ever
  wants one.
- **`/console/billing` page**: current plan card, the plan's included limits
  against current usage (reusing the usage meters), and an "Upgrade" contact
  link (mailto or contact form). No invoices, no payment methods, no Stripe.
- **Plan badge** in the console header (for example "Alpha").
- Backend: `GET /control/v1/clerk-orgs/{org}/plan` returning plan id, display
  name, and limits map. Operators set plans through the existing operator
  tooling path (direct store method); there is no public write endpoint yet.
- **Forward note**: when payments become real, the presumed path is Stripe
  Customer Portal. The plan enum and limits map are designed so Stripe
  product ids can map onto them without schema rework.

## Feature area 3: data control

All destructive writes (retention changes, deletes) require Clerk `org:admin`.
Export requires organization membership only.

### Data tab (new, per project) — three panels

**1. Retention**

- Setting: transcript retention in days. Range 1 day → forever. Stored as a
  nullable integer where null means forever; **forever is the default**.
  Presets offered: 30 / 90 / 365 / Forever, plus custom input.
- Shortening the window shows a confirmation dialog stating: transcripts
  older than N days will be purged on the next retention run; promoted facts
  survive, but their source-message provenance links become tombstoned.
- Enforcement: a retention pass runs inside the background job cycle,
  ordered **after** dream promotion, using the ADR 0022 purge machinery
  scoped to expired transcript windows. "After promotion" cannot be a mere
  scheduling convention — dream runs are independent externally-triggered
  invocations and fail closed when model ports are unconfigured, so a
  stalled promotion pipeline must not let retention destroy transcripts
  whose candidates were never promoted. The retention pass therefore gates
  on a **promotion watermark**: it only purges transcript windows older than
  the last successful promotion run for the scope. If the watermark lags the
  retention cutoff, the pass skips, records a skipped-with-reason job event,
  and the console Data tab surfaces "retention is waiting on background
  processing" rather than silently ignoring the customer's setting.
- **Flag**: the age-scoped purge entry point is the one piece of this plan
  that reaches beyond the hosted adapter. It gets its own small design check
  before implementation.
- Backend: `GET`/`PUT /control/v1/clerk-orgs/{org}/projects/{id}/retention`.

**2. Export**

- "Request export" creates an async export job producing a JSON archive of
  facts, transcripts, and metadata. The job list shows status; completed jobs
  expose a time-limited signed download link. Raw memory reaches the user
  only as this file.
- Archives are stored temporarily and deleted after 72 hours.
- Backend: `POST .../exports`, `GET .../exports`,
  `GET .../exports/{id}/download`. The export job uses a `memory:export`
  capability minted internally for the job itself. The `v1-memory` Agent API
  Key label mapping (ADR 0013) is unchanged — customer agent keys never gain
  export capability through this work.
- **Flag — export needs its own design check before implementation**, at
  least as formal as retention's. Unresolved decisions it must settle:
  - **Storage target**: the existing `export_scope` writes plaintext JSON to
    local container disk, which is neither durable across redeploys nor
    shared across replicas — the download and the 72-hour lifecycle both
    break on Railway. An object-store target (or equivalent durable store)
    must be chosen; ADR 0008's S3 backup path is operator infrastructure,
    not automatically this.
  - **Encryption at rest**: export deliberately decodes memory content to
    plaintext (ADR 0023), so a durable plaintext archive reintroduces
    exactly the at-rest exposure the content codec closed. The archive must
    be encrypted at rest; the design check picks the mechanism.
  - **Signing scheme** for the time-limited download link (what signs it,
    key lifecycle, expiry enforcement).
  - **Deletion enforcement**: what actually guarantees the 72-hour delete
    runs (not merely intended).

**3. Delete project memory** (Danger Zone within the Data tab)

- Type-the-project-name confirmation. The confirmation dialog must include
  erasure-horizon disclosure per ADR 0022: purge removes data from the live
  database, but point-in-time-recovery history and operator backup objects
  age out on their own retention schedule — the dialog must not claim
  instantaneous global erasure.
- Execution order (ADR 0022 requires writers stopped before purge; live keys
  during purge would let in-flight agent writes or a concurrent dream job
  recreate data after "delete"):
  1. revoke the project's agent keys;
  2. quiesce — confirm no in-flight dream or retention job holds the scope;
  3. tombstone the project memory scope;
  4. physically purge via the ADR 0022 path;
  5. residue check — verify the scope is empty before marking the operation
     complete.
- The project record remains with `deleted` status so event history stays
  coherent.
- Backend: `POST .../projects/{id}/delete-memory`, idempotent.

### Tenant deletion (Danger Zone in `/console/settings`)

- Type-the-organization-name confirmation plus a second explicit
  "I understand this is irreversible" checkbox. The dialog carries the same
  erasure-horizon disclosure as project memory deletion (ADR 0022: backups
  and PITR history age out on their own schedule).
- Deletes: all project memory (purge, using the same
  revoke → quiesce → tombstone → purge → residue-check order per project),
  the customer memory database, agent keys, and project records; tombstones
  the Customer Account Mapping. The Clerk organization itself is untouched —
  the user's login remains and the console shows the empty state afterward.
- **Server-side two-step confirm** — a console-side role check alone is too
  thin a gate for an irreversible tenant wipe (the shared Console Service
  Credential would otherwise be the only real barrier, and ADR 0013 defers
  adapter-side role verification):
  1. `POST /control/v1/clerk-orgs/{org}/delete-tenant/confirmations` mints a
     single-use confirm token bound to the org, expiring in 10 minutes;
  2. `POST /control/v1/clerk-orgs/{org}/delete-tenant` requires a valid
     unexpired token and consumes it.
  A single buggy or coerced console route cannot delete a tenant in one
  call; both steps are recorded in event history.
- Runs as an async job with progress state, since purge can be slow.

### Event history

- Panel at the bottom of the Data tab, with a tenant-level copy in settings.
- Table of data-control and key events: retention changed, export
  requested/completed, project memory deleted, tenant deletion initiated/
  completed, key minted, key revoked. Columns: actor (Clerk user id resolved
  to display name console-side), timestamp, event type. Metadata only — no
  payloads.
- This is deliberately **not** a general audit-log browser; that remains
  deferred. The slice exists so every destructive operation is visibly
  audited from the day it ships.
- Backend: `GET .../events?category=data-control`. This is **new
  instrumentation, not a read view**: no control-plane handler records audit
  events today (the existing sanitized audit path serves only agent-facing
  memory operations), and the audit event model has no category, event-type,
  or actor fields. The slice adds those fields, then instruments every
  relevant control-plane handler (key mint/revoke, project create,
  retention change, export, deletes).
- **Actor is console-asserted.** The console must start forwarding the
  acting Clerk user id on every mutating control-plane call (it forwards
  none today, and the adapter currently hardcodes `revoked_by:
  "console-service"`). Per ADR 0013 the adapter cannot verify Clerk
  identities in this slice, so the actor field is recorded and displayed as
  console-asserted metadata, not verified identity. Adapter-side Clerk JWT
  verification remains the eventual hardening path, deferred as in ADR 0013.

## Error handling

- Console: every new fetch follows the existing load-state pattern
  (loading / ready / error plus toast). Async jobs (export, tenant delete)
  poll with backoff; terminal failure states are rendered, never silent.
- Backend: fail closed everywhere — missing control credential, unknown
  organization, and non-admin destructive calls all reject. Destructive
  endpoints are idempotent (repeating a delete is a no-op success). ADR 0013
  response rules extend to all new endpoints: no secrets, request payloads,
  or transcript text in responses, errors, or logs.

## Testing

- Python: TDD per endpoint — auth rejection, role paths, cross-tenant
  isolation, idempotency, purge-after-promotion ordering, export lifecycle,
  and event recording. Extends the existing conformance and adapter suites.
- Console: extend the existing `npm test` pattern — route-handler tests
  against the in-memory store, and component-state tests (empty / loading /
  error / populated) for each new tab and panel.
- The in-memory development store gains all new endpoints so console work
  never requires a live backend.

## Delivery order

Each slice is a vertical: endpoint + UI + tests, independently landable.

1. Key lifecycle detail (smallest; sets the pattern)
2. Usage analytics upgrade (includes the `key_id` usage-event column)
3. Jobs tab (includes the `project_id` job-event column)
4. Billing scaffold and plan model
5. Event history (audit instrumentation + actor plumbing; foundation for
   danger-op visibility)
6. Retention (includes the flagged age-scoped purge design check and the
   promotion-watermark gate)
7. **Durable async job substrate** — job queue, worker loop, persisted
   progress state, and polling contract. Net-new infrastructure: the current
   background runner is a single synchronous CLI-invoked dream-phase method,
   and export and tenant deletion both depend on jobs that outlive one
   request. Gets its own design check (queue storage, worker topology,
   crash/retry semantics) before implementation.
8. Export (depends on 7; includes the flagged export design check —
   storage, at-rest encryption, signing, deletion enforcement)
9. Project memory delete
10. Tenant delete (depends on 7 for async execution)

Rationale: read-only operational surfaces first (low risk, immediate value);
the plan model lands before limits display depends on it; event history lands
**before** destructive operations so every delete is audited from day one;
the async job substrate lands before anything that needs it; destructive
operations come last, ordered smallest blast radius first.

## Explicitly out of scope

- Fact/candidate browsing, transcript viewers, or any memory content in
  console UI (ADR 0012 deferral stands).
- Payment processing, invoices, self-serve plan changes.
- General audit-log browser beyond the data-control event history.
- Sidebar-shell redesign (trigger condition recorded above).
- Per-project caps or quota enforcement beyond existing tenant caps
  (ADR 0013 note stands).
- Restore/undelete UI.
