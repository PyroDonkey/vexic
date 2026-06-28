# COA-244 - Wire Vexic Console control-plane to the hosted Vexic API

Date: 2026-06-27
Status: Design (pending review)
Linear: COA-244 (Wire Vexic Console control-plane to the hosted Vexic API, replace in-memory stub)

## Problem

The Vexic Console manages projects, agent API keys, and usage, but its
control-plane data layer is an in-memory stub. `console/lib/control-plane-store.mjs`
stores projects and keys in process-local `Map`s and fabricates usage totals.
Keys created in the dashboard are never persisted, never reach the Python
authenticator (`HostedApiKeyStore`), and vanish on restart. The hosted side
already exposes the real control-plane HTTP API (COA-247, ADR 0013) and a
SQLite-backed key store (COA-242/243). This work replaces the stub with a real
HTTP client so the dashboard manages genuine, authenticatable hosted keys.

This makes the "working API key system" goal true end to end and turns the
dashboard from a facade over fake data into an MVP over real data.

## Scope

In scope:

- A new console HTTP client that calls the hosted control-plane HTTP API.
- Rewrite `control-plane-store.mjs` as a dispatcher: real client when
  configured, in-memory stub as an explicit non-production fallback.
- Add `await` at the existing call sites in `control-plane-api.mjs`.
- Error mapping and usage payload normalization in the client.
- Tests for the client (mocked `fetch`) plus preservation of existing stub
  tests.

Out of scope (tracked elsewhere):

- Railway/Vercel deployment (COA-235, COA-233).
- Server-side support metadata endpoint (no hosted endpoint exists).
- Billing and caps semantics.
- Server-side tenant auto-provision behavior on GET (see Known Behavior C3).
- Org-level (`GET /clerk-orgs/{org}/usage`) wiring; only per-project usage is
  wired (see Known Behavior m4).

## Architecture

The seam stays at `control-plane-store.mjs`. Routes and React UI are unchanged.

```
route.ts (UNCHANGED)
  -> control-plane-api.mjs (add awaits; no signature change)
     -> control-plane-store.mjs (REWRITE as dispatcher)
        |-- configured  -> control-plane-client.mjs (NEW: fetch the real API)
        |-- dev fallback -> in-memory stub (KEPT, gated; see Configuration)
```

### Module responsibilities

- `control-plane-store.mjs` (rewrite): a dispatcher exposing the same named
  functions it does today (`createProject`, `listProjects`, `getProject`,
  `createAgentKey`, `listAgentKeys`, `revokeAgentKey`, `usageSummary`,
  `supportMetadata`, `resetStoreForTests`). It selects the real client or the
  stub per the Configuration rules below. The current stub implementation is
  preserved verbatim behind the dispatcher.
- `control-plane-client.mjs` (new): a thin `fetch` wrapper. One function per
  console operation. Owns base-URL composition, the `Authorization: Bearer`
  header, usage normalization, and HTTP-status-to-error mapping. It contains no
  Clerk logic and no React logic.
- `control-plane-api.mjs` (minimal change): its response builders already run
  inside async route handlers and already `await request.json()`. Add `await`
  to the store calls. No signature or error-shape change. `requireUser` /
  `requireOrg` console-layer guards are unchanged and continue to run before any
  client call.

### Endpoint mapping

All paths are under the hosted base URL, keyed by the Clerk org id:
`/control/v1/clerk-orgs/{orgId}/...`.

| Console function | HTTP method + path | Notes |
| --- | --- | --- |
| `listProjects` | `GET /projects` | |
| `createProject` | `POST /projects` | body `{name, environment}` |
| `getProject` | `GET /projects/{projectId}` | |
| `listAgentKeys` | `GET /projects/{projectId}/keys` | |
| `createAgentKey` | `POST /projects/{projectId}/keys` | returns `{rawKey, key}` |
| `revokeAgentKey` | `POST /projects/{projectId}/keys/{keyId}/revoke` | console DELETE route maps to server POST-revoke; 204 on success |
| `usageSummary` | `GET /projects/{projectId}/usage` | totals normalized (see C1) |
| `supportMetadata` | (no server endpoint) | returns empty when wired (see M5) |

The server auto-provisions the tenant on each call, so the console sends no
explicit `/tenant` provisioning request.

## Configuration

Console environment:

- `VEXIC_CONTROL_PLANE_URL` - base URL of the hosted control-plane API.
- `VEXIC_CONTROL_PLANE_TOKEN` - shared control-plane bearer token.

Hosted environment (already implemented): `VEXIC_CONTROL_PLANE_TOKENS`
(comma-separated) must include the console token. The server compares with
`hmac.compare_digest`.

### Dispatch and fallback rules (M4)

The stub must never serve fabricated data to real authenticated users in
production.

1. If `VEXIC_CONTROL_PLANE_URL` is set: use the real client. If the client is
   selected but `VEXIC_CONTROL_PLANE_TOKEN` is missing, the client still issues
   the request; the server returns 401, which surfaces as a configuration error
   (fail closed). Never silently fall back to the stub when a URL is present.
2. If `VEXIC_CONTROL_PLANE_URL` is unset AND `NODE_ENV !== "production"`: use the
   in-memory stub (local UI development without the Python backend).
3. If `VEXIC_CONTROL_PLANE_URL` is unset AND `NODE_ENV === "production"`: fail
   closed. Control-plane functions raise/return a clear "control plane not
   configured" error; the UI shows an error state rather than fabricated data.

## Security and trust model

### Org identity is server-derived only (C5)

`orgId` passed into the control-plane path segment is always derived
server-side from `readAuthContext()` (`auth.ts`, Clerk `session.orgId`). It is
never taken from request bodies, query parameters, or any browser-controllable
input. The shared bearer token proves the caller is the Console service, not
that a human belongs to an org (ADR 0013 trust model). The Console is therefore
the sole enforcement point for org membership. Any future change that lets the
browser influence the org id segment is a cross-tenant breach and must be
rejected in review.

### Two distinct 403s must not be conflated (C4)

There are two unrelated 403 responses:

- The console-layer `requireOrg` 403 (`active_org_required`) in
  `control-plane-api.mjs`, which fires before any fetch when the user has no
  active org. This is an expected user state the UI already handles
  (`projectCreateFailureMessage`). It is unchanged and must not be remapped.
- The upstream server 403 from the hosted API.

HTTP-status remapping (below) applies only to responses returned by
`control-plane-client.mjs`. The console-layer guards are untouched.

### Error mapping (client layer only)

| Upstream status | Console result | Rationale |
| --- | --- | --- |
| 200 / 201 / 204 | passthrough | |
| 400 | 400 `invalid_request` | bad input |
| 404 | 404 `not_found` | unknown project/key |
| 401 / 403 | 500 `control_plane_unavailable` | server-config bug, not a user auth issue |
| network / timeout | 502 `control_plane_unavailable` | upstream unreachable |

The client logs the upstream status code server-side for 401/403/5xx so the
most likely misconfiguration (token mismatch, which produces a persistent 401)
is diagnosable rather than appearing as a generic outage (M3).

## Payload handling

### Usage totals normalization (C1)

The server returns `usage.totals` with keys: `requests`, `writes`,
`retrievals`, `modelRequests`, `inputTokens`, `outputTokens`, `totalTokens`,
`estimatedCostMicros`. These differ from the stub's keys and include a raw
cost-in-micros integer. The current UI iterates `Object.entries(usage.totals)`
and camelCase-splits each key, which would render `estimatedCostMicros` as a
large unlabeled number.

The client normalizes `usage.totals` before returning it:

- Select the metrics to display deliberately rather than rendering all eight
  raw keys. Display set: `requests`, `writes`, `retrievals`, plus a formatted
  cost figure.
- Convert `estimatedCostMicros` to a dollar amount for display (micros / 1e6),
  labeled as cost, not a raw count.
- Token metrics (`inputTokens` / `outputTokens` / `totalTokens`) and
  `modelRequests` are omitted from the normalized totals for the MVP. They can
  be added later with count-appropriate labels.

Caps remain `{}` from the server; the UI already renders these as "No cap"
(`console-ui-state.mjs`). This is intentional per ADR 0013 (see C2).

### Key payload

The server `_key_payload` returns the fields the UI uses (`id`, `name`,
`capability`, `agentScope`, `display`, `createdAt`) plus extras (`projectId`,
`prefix`, `last4`, `revokedAt`) that the UI ignores. No transformation needed.

## Testing

- Existing stub tests (`tests/control-plane-api.test.mjs`) stay green: with no
  `VEXIC_CONTROL_PLANE_URL` set under the test (non-production) environment, the
  dispatcher uses the stub, and `resetStoreForTests` keeps working.
- New `tests/control-plane-client.test.mjs`: mock `fetch` and assert, for each
  operation, the correct method, path (including the server-derived `orgId`
  segment), `Authorization: Bearer` header, and request body; assert the
  DELETE-to-POST-revoke mapping; assert usage totals normalization (including
  cost formatting); assert the status-to-error mapping table.
- The raw-key format differs between stub (`vx_live_...`) and server
  (`vx_<hex>_...`); real-client tests must not reuse the stub's `vx_live_`
  assertion (M2).

## Known behavior and follow-ups (not fixed here)

- C2: Wiring removes the cap bars the stub showed; intentional per ADR 0013.
- C3: The hosted server provisions a tenant and SQLite DB file as a side effect
  of read-only GETs. This is existing accepted server behavior (COA-247);
  flagged here as a follow-up, out of scope for COA-244.
- M1: Revoking an already-revoked or unknown key yields a 404 the UI surfaces as
  a failed-revoke toast; behavior matches the prior stub.
- m4: The org-level `GET /clerk-orgs/{org}/usage` endpoint is not wired; only
  per-project usage is. Noted for future scope.

## Boundaries

This work stays within settled boundaries:

- The control-plane client lives in `console/`, the isolated Next.js package
  surface. No control-plane or fetch code is added to `src/vexic`.
- No provider secrets, billing, or hosted auth logic is added to the core
  package. The console holds only the shared control-plane bearer token via
  environment configuration.
- No Linear SDK, secret, or runtime dependency is added.
