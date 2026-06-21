# Hosted Memory MVP Shell

Role: deployment and readiness notes for the first hosted boundary around the
Vexic memory core.

The hosted MVP shell is an in-process Python boundary in `vexic.hosted`.
Concrete tenant catalog and API-key provisioning live in adapters outside
`src/vexic`; the repository-local `vexic_hosted_local` module is for local
staging and tests. This is not a public HTTP server, dashboard, billing system,
or production customer-data service. A future web/API process can wrap this
boundary without changing the memory contract.

## What Exists

- `HostedMemoryService` exposes the public memory contract operation names,
  binds tenant/principal/capability scope from an adapter-supplied auth context,
  and delegates to `LocalMemoryService`.
- `vexic_hosted_local.HostedTenantCatalog` provisions one isolated
  SQLite-compatible Customer Memory Database per tenant for local staging.
- `vexic_hosted_local.HostedApiKeyStore` creates high-entropy scoped API keys,
  stores only SHA-256 hashes, authenticates with constant-time hash comparison,
  and can revoke keys for local staging.
- `HostedBackgroundJobRunner` records dream-phase job lifecycle events and
  fails closed with `HostPortNotConfigured` while model-backed host ports are
  absent.
- `HostedMemoryService` can send sanitized request audit and usage metadata to
  a telemetry sink without storing tenant metadata in shared service lists.
- The local staging adapter stores request audit and usage rows in each tenant
  SQLite database without raw API keys or request payload text.

## Local Staging

Use a throwaway directory for tenant databases:

```python
from pathlib import Path

from vexic.contract import MemoryCapability
from vexic.hosted import HostedMemoryService
from vexic_hosted_local import HostedApiKeyStore, HostedTenantCatalog

catalog = HostedTenantCatalog(Path(".hosted-memory"))
catalog.provision_tenant("tenant-a", project_ids={"project-a"})

keys = HostedApiKeyStore()
api_key = keys.create_key(
    tenant_id="tenant-a",
    principal_id="agent-a",
    capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
    project_ids={"project-a"},
)

service = HostedMemoryService(catalog, keys, telemetry=catalog)
```

The returned `api_key.raw_key` is shown once. Store it in the caller's secret
store. The in-memory key store is for local staging and tests only. SHA-256 is
used here because generated API keys are high-entropy random tokens; do not
reuse this as a password hashing pattern.

## Hosted Environment

For one internal hosted environment:

- run a server-owned API process that calls `HostedMemoryService`;
- verify human/session auth outside `src/vexic`;
- issue scoped Vexic API keys for agent callers through the server-owned
  control surface;
- provision one managed SQLite/libSQL-compatible Customer Memory Database per
  tenant;
- keep a separate durable catalog for tenant-to-database routing and key hashes;
- keep key revocation durable and fast enough for the expected risk window;
- keep audit and usage ledgers durable outside the tenant memory database;
- supply model-backed host ports before enabling real Light, REM, or Deep jobs.

## Readiness

External customer-memory readiness is blocked by the hosted readiness gate in
Linear. This hosted shell remains internal-only until that gate is satisfied or
an explicit Ryan-approved risk acceptance is recorded.

Internal-only today:

- in-process Python API boundary;
- local SQLite-compatible tenant databases;
- repo-local in-memory tenant catalog and API-key adapter;
- tenant-local SQLite audit and usage telemetry in the local adapter;
- in-memory job lifecycle events;
- one `LocalMemoryService` instance is created per hosted request;
- fail-closed dream jobs without host model ports.

Not production/customer-data ready yet:

- no public HTTP adapter in this package;
- no durable hosted key/catalog/job store or production control-plane
  audit/usage store;
- no restore drill, network hardening, rate limiting, support-access policy, or
  incident runbook;
- no billing, dashboard, portal, enterprise SSO, or compliance claims;
- export/delete/retention depend on the underlying `MemoryService` methods and
  remain limited by the current local service implementation.
