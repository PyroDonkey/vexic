# Vexic

Vexic is a local-first memory core for long-running AI agents. It stores
cleaned conversation history, stages candidate memories for review, and promotes
durable facts with provenance.

The current package is a Python 3.13 core with a SQLite reference service,
public contract models, retrieval primitives, and conformance tests. Hosted
surfaces in this repository are internal-alpha adapter code, not a public
service contract.

## Quick Start

Install and test the Python memory core with `uv`:

```powershell
uv run pytest
```

Run the local read-only MCP server against a Vexic database:

```powershell
uv run python scripts\vexic-mcp-stdio.py --db-path .\memory.db --tenant-id local --session-id default
```

## Repository Map

- `src/vexic/` - memory contract, local service, storage, retrieval, and hosted
  adapter code.
- `tests/` - executable conformance and reliability coverage.
- `console/` - isolated Next.js control-plane app; it is not package runtime.
- `docs/usage.md` - setup, MCP, recorder, hosted-alpha, and smoke-test examples.
- `docs/architecture.md` and `docs/memory-service-contract.md` - architecture
  and contract references.
- `docs/ai/README.md` - internal automation and maintainer tooling docs.

## Package Boundary

The repository root remains `uv`-managed. The Vexic Console source lives in
`console/` as a repo-local Next.js control-plane app and is not Vexic package runtime,
not a `vexic.*` entrypoint, and not memory-core runtime under `src/vexic`.

Console dependencies stay in `console/package.json` and
`console/package-lock.json`; do not add Node package files at the root or treat
Console dependencies as memory-core requirements.
