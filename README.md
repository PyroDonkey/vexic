# Vexic

**Memory your agents can trust.**

[![PyPI](https://img.shields.io/badge/PyPI-coming%20soon-lightgrey.svg)](https://pypi.org/project/vexic/)
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

## Python Quickstart

Use the library directly from Python — append to a session transcript, then
search it back:

```python
import asyncio
from pydantic_ai.messages import ModelRequest, UserPromptPart
from vexic import (
    AppendTranscriptRequest, LocalMemoryService, MemoryCapability, MemoryScope,
    Principal, PrincipalType, RedactionContext, SearchTranscriptRequest, TrustBoundary,
)
from vexic.storage import single_message_adapter

scope = MemoryScope(
    tenant_id="local", session_id="session-1",
    principal=Principal(principal_id="me", principal_type=PrincipalType.HUMAN),
    trust_boundary=TrustBoundary.LOCAL_TRUSTED,
    capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
)

async def main() -> None:
    service = LocalMemoryService(db_path="memory.db", tenant_id="local")
    service.init_schema()
    message = ModelRequest(parts=[UserPromptPart(content="I prefer tabs over spaces.")])
    await service.append_transcript(AppendTranscriptRequest(
        scope=scope,
        messages_json=[single_message_adapter.dump_json(message).decode()],
        redaction=RedactionContext(forbidden_values=()),
    ))
    result = await service.search_transcript(SearchTranscriptRequest(scope=scope, query="tabs"))
    print(result.hits[0].body)

asyncio.run(main())
```

Output: `User: I prefer tabs over spaces.` All environment variables the
package and its adapters read are listed in
[`docs/configuration.md`](docs/configuration.md).

## Repository Map

- `src/vexic/` - memory contract, local service, storage, retrieval, and hosted
  adapter code.
- `tests/` - executable conformance and reliability coverage.
- `console/` and `website/` - isolated Next.js apps (control-plane and marketing
  site); not package runtime.
- `docs/usage.md` - setup, MCP, recorder, hosted-alpha, and smoke-test examples.
- `docs/architecture.md` and `docs/memory-service-contract.md` - architecture
  and contract references.
- `docs/adr/` - accepted architecture decision records.
- `docs/ai/README.md` - internal automation and maintainer tooling docs.

## Package Boundary

The repository root remains `uv`-managed. The Vexic Console (`console/`) is a
repo-local Next.js control-plane app — not Vexic package runtime, not a
`vexic.*` entrypoint, and not memory-core runtime under `src/vexic`. The
marketing site (`website/`) is likewise a repo-local Next.js app outside the
package. Their dependencies stay in their own `package.json` files
(`console/package.json`, `website/package.json`); do not add Node package files
at the root.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the branch workflow,
[SECURITY.md](SECURITY.md) for reporting vulnerabilities, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## License

Apache-2.0. See [LICENSE](LICENSE).
