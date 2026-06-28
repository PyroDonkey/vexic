# Hosted control-plane HTTP API is a console-facing adapter slice

Status: proposed

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

Expose a narrow `/control/v1/*` surface on the hosted FastAPI adapter for
internal-alpha Console and operator use.

The control-plane surface is hosted-adapter code only. It may call
`HostedTenantCatalog`, `HostedApiKeyStore`, and hosted telemetry readers, but
it does not add operations to `MemoryService`, change Vexic core storage, or
move Console code into `src/vexic`.

Every `/control/v1/*` request requires a configured control-plane credential.
This credential is distinct from Vexic Agent API Keys:

- control-plane credentials are accepted only by `/control/v1/*`;
- Agent API Keys are accepted only by `/mcp` and the agent-facing `/v1/*`
  memory routes;
- `/mcp` continues to require `Authorization: Bearer <vexic-agent-api-key>`;
- `/v1/*` keeps its existing hosted-agent-key compatibility behavior;
- if no control-plane credential is configured, `/control/v1/*` fails closed.

The initial local/staging auth mechanism is a server-to-server bearer token
configured through the hosted adapter. Clerk remains the human login and
organization authority in the Console. The Python hosted adapter does not
verify Clerk sessions in this slice.

A Clerk Organization maps deterministically to one hosted tenant. The hosted
adapter derives the hosted `tenant_id` from the Clerk organization id and never
accepts caller-supplied tenant ids on the control-plane HTTP surface. Tenant
and project operations are idempotent.

The initial endpoint family is:

- `POST /control/v1/clerk-orgs/{clerk_org_id}/tenant` for idempotent tenant
  lookup and provisioning;
- `GET /control/v1/clerk-orgs/{clerk_org_id}/projects` for project listing;
- `POST /control/v1/clerk-orgs/{clerk_org_id}/projects` for project creation;
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
  reads.

List and usage responses must not return raw Agent API Keys, key hashes,
control-plane credentials, request bodies, transcript text, search queries, or
provider secrets. Errors are sanitized and must not echo supplied credentials.

Keys minted through the control-plane API authenticate through the existing
hosted Agent API Key path. Revocation through the control-plane API makes the
same key invalid for `/mcp` and `/v1/*`.

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
auth, Clerk-org tenant idempotency, project scoping, key mint/list/revoke,
cross-tenant isolation, usage filtering, secret-safe responses, and minted-key
compatibility with `/mcp` and `/v1/*`.
