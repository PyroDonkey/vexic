# Production telemetry boundary is settled before product analytics

Status: accepted

Vexic will decide the production telemetry boundary before adding production
analytics. Operational telemetry belongs to the hosted control plane: sanitized
audit, usage, and job records may be stored outside Customer Memory Databases to
run, audit, meter, and debug the hosted memory API. Memory-domain retrieval
telemetry stays tenant-scoped in the Customer Memory Database because it is part
of replayable memory behavior, not a cross-tenant product analytics stream.

The v1 production vocabulary is intentionally small: `HostedAuditEvent`,
`HostedUsageEvent`, `HostedJobEvent`, `retrieval_events`, and
`candidate_retrieval_events`. Operational telemetry must not contain raw memory
payloads, prompt payloads, hidden instructions, thinking traces, tool bodies,
raw API keys, provider secrets, database tokens, or configured forbidden values.

Product-improvement data collection is default off for customer-data-derived
content. Non-content operational aggregates may be used for capacity, reliability,
and product planning. Any use of content-bearing memory telemetry, including
query-bearing retrieval rows, for cross-tenant product improvement requires a
separate consent, retention, deletion, security, and legal gate.

This chooses a narrow boundary decision now instead of either deferring logging
policy until production storage exists or building full analytics now.
