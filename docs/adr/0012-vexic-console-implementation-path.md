# Vexic Console starts as one Next.js app

Status: accepted

Vexic will build the first public website and account dashboard as one small
Next.js App Router app on Vercel, named Vexic Console. Public marketing routes
and authenticated console routes live in the same app; the Python Hosted Memory
API stays on Railway at `api.vexic.dev`, and production control-plane storage
can move to Neon without moving dashboard code into `src/vexic`.

## Decision

Clerk owns human login, account settings, organization switching, and
organization membership. A Clerk Organization is the human Customer Account.
Projects are Vexic-owned control-plane records under a Customer Account, and
Agent API Keys are Vexic-owned machine credentials minted, scoped, verified, and
revoked by the hosted Vexic API. Clerk API Keys are not accepted as memory API
credentials.

Authenticated Console access requires an active Clerk Organization. Personal
account sessions must not create Vexic Projects or Agent API Keys.

The first Vexic Console route set is intentionally small:

- `/` public landing page;
- `/sign-in` and `/sign-up` through Clerk;
- `/console` authenticated project list and empty "create first project" state;
- `/console/projects/[projectId]` project workspace with API Keys, Usage & Caps,
  and minimal Project Settings tabs;
- `/console/settings` thin Clerk user and organization settings wrapper;
- `/console/support` Vexic-internal Support View for metadata only.

The Agent API Key flow is project-scoped: an authorized human creates a Vexic
key from the project workspace, selects the v1 capability and optional agent
scope, sees the raw key once, and can later list key metadata or revoke the key.

## Deferred

The first Console does not include billing, invoices, plan upgrades, payment
methods, raw memory browsing, transcript or fact viewers, MCP playgrounds,
complex analytics, audit export, restore/delete UI, enterprise SSO, custom team
management beyond Clerk defaults, or a separate static marketing site.

Support/admin surfaces must not browse raw memory by default. They may show
account, project, key, usage, audit, job, incident, restore, export, and delete
metadata needed to operate hosted Vexic.

## Consequences

This keeps the first website and dashboard easy to ship while preserving the
existing memory boundary: Vexic Console is a control-plane client, not memory
core runtime. The hosted API remains the authority for project-scoped agent
credentials, capability checks, revocation, rate-limit dimensions, and
sanitized operational telemetry.
