# Vexic

**Memory your agents can trust.**

[![PyPI version](https://img.shields.io/pypi/v/vexic.svg)](https://pypi.org/project/vexic/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/PyroDonkey/vexic/actions/workflows/ci.yml/badge.svg)](https://github.com/PyroDonkey/vexic/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/)

Vexic is a local-first memory core for long-running AI agents. It stores cleaned
conversation history, stages candidate memories for review, and promotes durable
facts — every one traceable to the messages it came from.

The current package is a Python 3.13 core with a SQLite reference service, public
contract models, retrieval primitives, and conformance tests. It runs entirely on
your machine: it reads and writes a local database and never exfiltrates data.

> **Local MCP works today. A hosted service is coming** — see
> [vexic.dev](https://vexic.dev) to learn more and join the waitlist. Hosted
> surfaces in this repository are internal-alpha adapter code, not a public
> service contract.

## Quick Start

Install and test the Python memory core with `uv`
([install uv](https://docs.astral.sh/uv/)):

```bash
uv run pytest
```

Run the local read-only MCP server against a Vexic database:

```bash
# POSIX shells (bash/zsh)
uv run python scripts/vexic-mcp-stdio.py --db-path ./memory.db --tenant-id local --session-id default
```

```powershell
# PowerShell
uv run python scripts\vexic-mcp-stdio.py --db-path .\memory.db --tenant-id local --session-id default
```

The server exposes two read-only tools to the agent: `recall_conversation_history`
(this session's transcript) and `recall_user_memory` (durable facts and
preferences). See [`docs/usage.md`](docs/usage.md) for MCP client setup, the
transcript recorder, and smoke-test examples.

## Repository Map

- `src/vexic/` - memory contract, local service, storage, retrieval, and hosted
  adapter code.
- `tests/` - executable conformance and reliability coverage.
- `docs/usage.md` - setup, MCP, recorder, hosted-alpha, and smoke-test examples.
- `docs/architecture.md` and `docs/memory-service-contract.md` - architecture
  and contract references.
- `docs/adr/` - accepted architecture decision records.
- `docs/ai/README.md` - internal automation and maintainer tooling docs.

Vexic Console and the marketing website live in the private
`PyroDonkey/vexic-website` repository (COA-295: open-core boundary) — see
[ADR 0012's addendum](docs/adr/0012-vexic-console-implementation-path.md).

## Package Boundary

The repository root remains `uv`-managed; do not add Node package files at the
root. Console and website ownership moved out of this repository entirely
(see Repository Map above) — there is no in-repo Node package surface to
isolate anymore.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the branch workflow,
[SECURITY.md](SECURITY.md) for reporting vulnerabilities, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## License

Apache-2.0. See [LICENSE](LICENSE).
