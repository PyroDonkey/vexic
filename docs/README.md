# Vexic Documentation

Start here if you are new to the project.

## Using Vexic

- [Usage guide](usage.md) — running the memory service locally, wiring the
  MCP server into Claude Code and Codex, dream-phase configuration.
- [Examples](examples.md) — worked examples, including the retrieval evals.
- [Memory service contract](memory-service-contract.md) — the versioned
  public contract (`MemoryService`, scopes, capabilities) that
  implementations must satisfy.
- [Architecture](architecture.md) — the three-tier memory model and how the
  pieces fit together.
- [Provenance](provenance.md) — how every stored fact carries its source.

## Hosted service (internal alpha)

- [Hosted MVP](hosted-mvp.md) — hosted API surface, auth, and deployment.
- [Runbooks](runbooks/) — operations: incident response, migration, secret
  rotation, and dated drill records.

## Decisions and internals

- [Architecture Decision Records](adr/README.md) — the canonical ADR index.
- [Branch sync](branch-sync.md) — the dev/main branching workflow.
- [Agent runbook](agent-runbook.md) — running Vexic's own agents.
- [`ai/`](ai/README.md) — maintainer/automation tooling docs (internal; not
  product documentation).
