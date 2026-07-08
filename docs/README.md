# Vexic Documentation

Start here if you are new to the project.

## Using Vexic

- [Usage guide](usage.md) - running the memory service locally, wiring the
  MCP server into Claude Code and Codex, dream-phase configuration.
- [Examples](examples.md) - agent behavior and contributor operating patterns,
  including a worked retrieval-evals run.
- [Memory service contract](memory-service-contract.md) - the versioned
  public contract (`MemoryService`, scopes, capabilities) that
  implementations must satisfy.
- [Architecture](architecture.md) - the three-tier memory model and how the
  pieces fit together.
- [Provenance](provenance.md) - how every stored fact carries its source.

## Hosted service (internal alpha)

- [Hosted MVP](hosted-mvp.md) - hosted API surface, auth, and deployment.

Hosted operational runbooks (incident response, migration, secret rotation, and
dated restore/migration drill records) are maintained in the private hosted-ops
repository and are not part of the public docs set.

## Decisions and internals

- [Architecture Decision Records](adr/README.md) - the canonical ADR index.
- [Branch sync](branch-sync.md) - the dev/main branching workflow.
- [Agent runbook](agent-runbook.md) - running Vexic's own agents.
- [`ai/`](ai/README.md) - contributor and maintainer workflow docs for coding
  agents.
