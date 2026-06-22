# Agent scope is exact and shared rows are explicit

Vexic separates agent-private memory with an optional `agent_id` refinement on
`MemoryScope` and `MemoryScopeSelector`. A missing `agent_id` means shared
memory inside the same tenant, project, user, and session parent scope; it is
not a wildcard, and agent-specific reads do not implicitly include shared rows.
This keeps `principal_id` as auth/audit identity only, preserves append-only
transcript rows by leaving legacy rows as `NULL` shared rows, and makes
shared-plus-agent retrieval an explicit adapter or caller choice.
