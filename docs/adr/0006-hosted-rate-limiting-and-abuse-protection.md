# Hosted rate limiting starts with edge WAF plus in-process MVP quotas

Status: accepted

## Context

The hosted memory API will eventually receive traffic from automated agents and
customer applications. It needs abuse controls before external customer data is
accepted so one caller cannot exhaust storage, retrieval, background jobs, or
model spend.

Vexic v0.1 does not ship a public HTTP service or production control plane.
The current hosted boundary is the in-process `HostedMemoryService` shell and
local staging adapters. That shell can enforce authenticated operation quotas,
but it cannot identify unauthenticated source IPs or absorb volumetric attacks.

Railway remains the intended origin runtime for the first hosted API process.
Railway's public networking docs describe network-layer DDoS mitigation and
recommend Cloudflare for application-layer WAF protection. Cloudflare's WAF
and rate limiting rules are the selected app-layer edge path for a public
hosted API.

## Decision

Use two layers:

- Cloudflare in front of Railway for public app-layer WAF, DDoS, and coarse
  unauthenticated rate limiting before traffic reaches the hosted API origin.
- `HostedMemoryService` in-process quotas as the v0.1 MVP backstop for
  authenticated agent-key traffic and expensive memory operations.

The in-process limiter is intentionally local-staging grade. It is
single-process, in-memory, and reset on process restart. It is not DDoS
protection and is not a production distributed quota store. A public hosted API
must keep Cloudflare enabled and add a durable or edge-backed quota store before
external customer-data launch.

## MVP Enforcement

`HostedMemoryService` checks rate limits after API-key authentication and scope
binding, before delegating to the memory core or model-backed host ports.

The v0.1 Python contract for over-limit requests is
`HostedRateLimitExceeded`, which includes `retry_after_seconds`. A future HTTP
adapter should map that exception to `429 Too Many Requests` with a
`Retry-After` response header.

The in-process implementation uses monotonic process time, a thread lock around
bucket updates, expired-bucket pruning, and a hard bucket-count cap. Those are
local safety rails, not a substitute for distributed quota state.
Authenticated attempts consume quota before delegation, even when the delegated
operation fails, so retry storms against misconfigured or unavailable host ports
are also bounded.

Default in-process dimensions:

| Dimension | v0.1 shell behavior |
| --- | --- |
| Tenant | Included in the bucket key selected by API-key auth. |
| Principal | Included in the bucket key selected by API-key auth. |
| Agent key | Included by key id; raw API keys are never logged. |
| Endpoint | Operation name is included in the bucket key. |
| Source IP | Deferred to Cloudflare and the future HTTP adapter. |
| Human user | Deferred to the control plane or session-auth adapter. |

Default in-process quotas:

| Operation class | Default |
| --- | --- |
| Ordinary authenticated operations | 120 requests per 60 seconds per tenant/principal/key/operation. |
| `expand_history` | 30 requests per 60 seconds per tenant/principal/key/operation. |
| `run_dream_phase`, `export_scope`, `replay_scope`, `rebuild`, `delete_scope` | 6 requests per hour per tenant/principal/key/operation. |

These defaults are safe staging defaults, not a pricing plan. Production hosted
limits should be configured from measured traffic, customer tier, and abuse
readiness requirements.

## Public Edge Defaults

When a public HTTP adapter exists, use these defaults unless a launch review
sets stricter values:

| Traffic | Default posture |
| --- | --- |
| Unauthenticated memory API routes | Deny. Memory operations require auth. |
| Health or status routes | 60 requests per minute per source IP at the edge. |
| Login, signup, key-provisioning, and control-plane routes | 5 requests per minute per source IP and per account identifier. |
| Authenticated user-session traffic | 60 requests per minute per user, plus endpoint-specific caps. |
| Agent-token traffic | Enforce both edge/API-key rules and in-process operation quotas. |
| Suspicious scans, malformed payloads, or repeated auth failures | Challenge or block at Cloudflare; record sanitized audit events at the host when identity is known. |

Origin bypass must be prevented. The Railway origin should only be reachable
through the selected edge path once the API is public.

## Payload And Expensive-Operation Caps

The public HTTP adapter should reject oversized requests before they enter the
memory core:

| Surface | Default cap |
| --- | --- |
| Search query text | 1000 characters. |
| Transcript append or source-ingest request body | 1 MiB per request and 100 messages per request. |
| Verbatim history expansion | Keep existing bounded message-range and returned-text caps. |
| Export, replay, rebuild, delete, and dream phases | Admin-only capability plus the stricter hourly operation quota above. |

Model-backed Light, REM, and Deep work remains host-port backed. Token and
dollar spend caps become mandatory before enabling real hosted model adapters.
Until model usage telemetry is available from those adapters, operation quotas
are the MVP control and real spend accounting remains a launch gate.

Background jobs should allow at most one running dream phase per
tenant/project/phase in the hosted job runner. A durable hosted runner must
enforce that with a shared lock or queue; the current local runner only records
single-process lifecycle events.

## Abuse Logging And Overrides

Rate-limited requests are audit and usage events, not raw payload logs. Events
record operation, tenant id, principal id, status, timestamp, and error type.
They must not include raw API keys, request bodies, transcript text, or search
queries.

Repeated limit violations should trigger key review or revocation through the
host-owned control surface. Manual override should be explicit, time-bounded,
and recorded as an operator action. Those workflows are outside the v0.1 core
package.

## Consequences

This decision satisfies the hosted MVP need for testable enforcement without
adding a public HTTP service, provider SDK, billing system, dashboard, or
production operations stack to `src/vexic`.

The core memory contract does not gain rate-limit fields. Rate limiting remains
a hosted adapter concern around the contract. MCP and future HTTP adapters must
inherit these hosted rules rather than defining incompatible limits.

The remaining launch gates are Cloudflare configuration, origin lock-down,
durable distributed quota state, token/dollar spend accounting, auth-failure
edge throttles, alerting, and a manual abuse-response workflow.

Sources checked on 2026-06-21:

- [Railway public networking specs and limits](https://docs.railway.com/networking/public-networking/specs-and-limits)
- [Railway custom domains](https://docs.railway.com/guides/public-networking)
- [Cloudflare rate limiting rules](https://developers.cloudflare.com/waf/rate-limiting-rules/)
- [Cloudflare WAF overview](https://developers.cloudflare.com/waf/)
