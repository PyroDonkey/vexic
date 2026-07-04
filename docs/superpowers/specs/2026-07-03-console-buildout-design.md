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
  role, enforced in console API routes.

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
  Job lifecycle events are already recorded by `HostedBackgroundJobRunner`;
  this is a read view plus a store query method.

### Usage tab upgrade

- Time-series chart: operations per day by type (append, search, expand),
  last 30 days. Uses the Tremor chart components already in the repo.
- Cap proximity: per-cap meter with a warning state at 80% and an alert state
  at 95%.
- Per-key attribution table: operations by key id over the period, so a
  customer can see which agent is consuming quota.
- Backend: extend the usage endpoint with `?granularity=day&days=30`
  returning bucketed rows, and add `GET .../usage/by-key`. Usage events
  already carry key attribution; this is aggregation-query work only.

### Key lifecycle detail

- `last_used_at` per key in the key list. Recorded at successful key auth;
  writes throttled to at most one update per minute per key so hot keys do
  not hammer the store.
- Revoked-key history: key list endpoint gains `?include=revoked`; console
  shows a collapsed "Revoked keys" section with revoked-at timestamps.
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
- Enforcement: a retention pass runs inside the existing background job
  cycle, ordered **after** dream promotion, so candidates are promoted before
  their source transcripts expire. It uses the ADR 0022 purge machinery
  scoped to expired transcript windows.
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
  `GET .../exports/{id}/download`. The export job runs in the background
  runner and uses a `memory:export` capability minted internally for the job
  itself. The `v1-memory` Agent API Key label mapping (ADR 0013) is
  unchanged — customer agent keys never gain export capability through this
  work.

**3. Delete project memory** (Danger Zone within the Data tab)

- Type-the-project-name confirmation. Tombstones the project memory scope,
  then physically purges via the ADR 0022 path. Revokes the project's agent
  keys. The project record remains with `deleted` status so event history
  stays coherent.
- Backend: `POST .../projects/{id}/delete-memory`, idempotent.

### Tenant deletion (Danger Zone in `/console/settings`)

- Type-the-organization-name confirmation plus a second explicit
  "I understand this is irreversible" checkbox.
- Deletes: all project memory (purge), the customer memory database, agent
  keys, and project records; tombstones the Customer Account Mapping. The
  Clerk organization itself is untouched — the user's login remains and the
  console shows the empty state afterward.
- Backend: `POST /control/v1/clerk-orgs/{org}/delete-tenant`. Runs as an
  async job with progress state, since purge can be slow.

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
- Backend: `GET .../events?category=data-control` over the existing
  sanitized audit store, extended with the new event types.

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
2. Usage analytics upgrade
3. Jobs tab
4. Billing scaffold and plan model
5. Event history (foundation for danger-op visibility)
6. Retention (includes the flagged age-scoped purge design check)
7. Export
8. Project memory delete
9. Tenant delete

Rationale: read-only operational surfaces first (low risk, immediate value);
the plan model lands before limits display depends on it; event history lands
**before** destructive operations so every delete is audited from day one;
destructive operations come last, ordered smallest blast radius first.

## Explicitly out of scope

- Fact/candidate browsing, transcript viewers, or any memory content in
  console UI (ADR 0012 deferral stands).
- Payment processing, invoices, self-serve plan changes.
- General audit-log browser beyond the data-control event history.
- Sidebar-shell redesign (trigger condition recorded above).
- Per-project caps or quota enforcement beyond existing tenant caps
  (ADR 0013 note stands).
- Restore/undelete UI.
