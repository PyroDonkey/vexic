# Hosted control-plane HTTP API is a console-facing adapter slice

Status: accepted

## Context

ADR 0012 makes Vexic Console a control-plane client and says the hosted Vexic
API owns Vexic Projects, project-scoped Agent API Keys, key revocation, usage
and caps, and sanitized operational telemetry.

The hosted FastAPI adapter currently exposes only `/health`, `/mcp`, and the
agent-facing `/v1/*` memory routes. Tenant/project provisioning, agent-key
creation and revocation, and usage reads exist as local Python methods in
`hosted_local.py`, but no HTTP control-plane surface binds those methods for
the Console.

The missing decision is the HTTP and auth boundary for that surface. Control
plane auth must be separate from per-agent memory API keys, and the adapter
must not move Console runtime, billing, dashboards, hosted auth stacks, or
memory-core operations into `src/vexic`.

## Decision

Expose a narrow `/control/v1/*` surface on the repo-local hosted FastAPI
adapter for internal-alpha Console and operator use.

The control-plane surface is hosted-adapter code only, currently in
`vexic.hosted_control_plane_http`. It may call `HostedTenantCatalog`,
`HostedApiKeyStore`, and hosted telemetry readers, but it does not add
operations to `MemoryService`, change Vexic core storage, or move Console code
into `src/vexic`. The core app in `vexic.hosted_http` remains the hosted memory
and read-only MCP transport without `/control/v1/*` routes.

Every `/control/v1/*` request requires a configured control-plane credential.
The adapter may accept multiple configured Console Service Credential bearer
tokens so rotation can overlap without a persistent credential registry or
credential-management API. Configured blank tokens are invalid, an empty
post-normalization token set fails closed, and comparison must check every
configured token without short-circuiting. This credential is distinct from
Vexic Agent API Keys:

- control-plane credentials are accepted only by `/control/v1/*`;
- Agent API Keys are accepted only by `/mcp` and the agent-facing `/v1/*`
  memory routes;
- `/mcp` continues to require `Authorization: Bearer <vexic-agent-api-key>`;
- `/v1/*` keeps its existing hosted-agent-key compatibility behavior;
- if no control-plane credential is configured, `/control/v1/*` fails closed.

The initial local/staging auth mechanism is server-to-server bearer tokens
configured through the hosted adapter. Clerk remains the human login and
organization authority in the Console. The Python hosted adapter does not
verify Clerk sessions in this slice.

For this slice, the hosted adapter trusts the Console as the Clerk-enforcing
caller. The Console is responsible for checking that the acting human has an
active Clerk Organization and is authorized for the `clerk_org_id` it sends to
the hosted adapter. The control-plane credential proves the caller is the
Console service, not that a particular human belongs to a particular Clerk
Organization.

A Clerk Organization maps deterministically to one hosted tenant through a
persisted Customer Account Mapping in the hosted control-plane database. The
Console sends a Clerk organization id under delegated authority, and the hosted
adapter resolves a Vexic-owned `tenant_id` from that mapping, provisioning one
only on write operations (see the COA-248 addendum below). The hosted adapter
never accepts caller-supplied tenant ids on the control-plane HTTP surface.
Tenant and project operations are idempotent.

Projects are Vexic-owned control-plane records under the resolved tenant. Normal
project creation generates a hosted `proj_...` id, stores minimal project
metadata in the hosted control-plane database, and registers the exact same
project id through the hosted tenant catalog so `MemoryScope.project_id`, Agent
API Key `project_ids`, and project control-plane records stay byte-identical.
The Console's current local project ids are stub data until the Console is wired
to the hosted control-plane API.

Project `environment` is metadata only in this slice. It defaults to
`production`, may be displayed or filtered by the Console later, and does not
create a separate memory scope or API-key scope. Customers that need hard
development/production isolation in COA-247 use separate Projects with separate
project ids.

Agent API Keys in this slice follow one Console-facing shape. Key creation is
project-scoped and accepts a display `name`, the fixed product capability label
`v1-memory`, and optional `agentScope`. The hosted adapter maps `v1-memory` to
`memory:write`, `memory:search`, and `memory:expand`. It does not mint
`memory:export`, `memory:replay`, or `memory:admin:*` through this label.
Omitted or `shared` agent scope means no `agent_id` restriction. Any other
non-empty `agentScope` value is enforced as the sole allowed `agent_id` for
that key. Key creation returns the raw key once plus metadata; key listing
returns active metadata only and never returns raw keys or hashes.

The initial endpoint family is:

- `POST /control/v1/clerk-orgs/{clerk_org_id}/tenant` for idempotent tenant
  lookup and provisioning;
- `GET /control/v1/clerk-orgs/{clerk_org_id}/projects` for project listing;
- `POST /control/v1/clerk-orgs/{clerk_org_id}/projects` for hosted project
  creation with a server-generated project id;
- `GET /control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}` for
  project lookup;
- `PUT /control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}` for
  idempotent project provisioning;
- `GET /control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/keys` for
  agent-key metadata listing;
- `POST /control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/keys` for
  minting a project-scoped Agent API Key, returning the raw key once;
- `POST /control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/keys/{key_id}/revoke`
  for revocation;
- `GET /control/v1/clerk-orgs/{clerk_org_id}/usage` for sanitized tenant usage
  reads;
- `GET /control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/usage` for
  sanitized project-attributed usage reads.

Project usage in this slice is attribution, not enterprise metering. Hosted
usage and job events record nullable `project_id` when request scope provides
one. Project usage reads return only events attributed to that project under the
resolved tenant. Legacy or unscoped rows with no `project_id` remain visible
only in tenant usage. COA-247 does not add per-project caps, quota enforcement,
billing-grade backfill, custom reporting periods, or enterprise dashboards.
Project usage responses omit project caps rather than returning tenant caps
under a project label.

List and usage responses must not return raw Agent API Keys, key hashes,
control-plane credentials, request bodies, transcript text, search queries, or
provider secrets. Errors are sanitized and must not echo supplied credentials.
Logs and telemetry must not include supplied control-plane credential values.

Keys minted through the control-plane API authenticate through the existing
hosted Agent API Key path. Revocation through the control-plane API makes the
same key invalid for `/mcp` and `/v1/*`.

## Addendum (COA-248): reads are side-effect-free

Originally every control-plane handler provisioned the tenant as its first
action, so a read-only GET (for example the Console's first project-list load)
minted a tenant row, a Customer Account Mapping, and a customer database file.
COA-248 removed passive provisioning from the read path:

- GET endpoints and key revocation resolve an existing Customer Account
  Mapping without inserting anything. Resolution only returns tenants that
  completed provisioning (`active = 1`).
- When no tenant exists for the Clerk organization: project listing returns an
  empty list, tenant usage returns a zero-usage payload, and project-scoped
  reads (project lookup, key listing, project usage) and key revocation return
  `404 not_found`.
- Write paths still auto-provision idempotently: `POST .../tenant`,
  `POST .../projects`, `PUT .../projects/{project_id}`, and
  `POST .../projects/{project_id}/keys`.

## Deferred

- Clerk JWT/session verification inside the Python hosted adapter.
- User-role authorization beyond the Console's Clerk organization checks.
- Billing, invoices, plan upgrades, pricing enforcement, and payment methods.
- Public control-plane launch hardening beyond the existing hosted adapter
  staging posture.
- Dashboard or Console runtime code in `src/vexic`.
- Raw memory browsing, transcript/fact viewers, and support views over memory
  content.

## Consequences

COA-247 can implement the local/staging control-plane HTTP slice with TDD
against the hosted adapter without expanding the `MemoryService` contract or
reusing Agent API Keys as operator credentials.

The implementation must test auth rejection, fail-closed unconfigured control
auth, blank-token config rejection, overlap credential rotation,
no-short-circuit credential comparison, Customer Account Mapping idempotency,
hosted project record creation, project scope registration with the same
project id, metadata-only project environment behavior, key mint/list/revoke,
shared and agent-scoped key behavior, cross-tenant isolation, tenant and
project usage filtering, unattributed usage remaining tenant-only, secret-safe
responses and logs, and minted-key compatibility with `/mcp` and `/v1/*`.

## Notes

Co-deploying the control-plane surface and the core memory app in one FastAPI
process is the accepted topology for this slice; the auth boundary between
control-plane credentials and Agent API Keys is logical and enforced per
request, not process-level. Splitting the core memory and control-plane into
separate services or processes would change this decision and must be recorded
as a superseding or updated ADR that states the new trust boundary, network and
secret surface, deploy ownership, and migration plan. Do not split silently.

This trigger is coupled to durable rate-limiting work (COA-263): a durable
distributed quota store needs shared state outside the core-memory process,
which may itself motivate the split. That choice should therefore be made as
part of the quota design rather than as silent later hardening. See the
security-gap umbrella COA-27.
