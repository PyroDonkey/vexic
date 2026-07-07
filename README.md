# Vexic

**Memory your agents can trust.**

[![PyPI](https://img.shields.io/pypi/v/vexic.svg)](https://pypi.org/project/vexic/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/PyroDonkey/vexic/blob/main/LICENSE)
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

## Install

```bash
pip install vexic
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add vexic
```

## Quick Start

Installing the package puts the `vexic` command on your `PATH`. Run the local
read-only MCP server against a Vexic database:

```bash
# POSIX shells (bash/zsh)
vexic mcp-stdio --db-path ./memory.db --tenant-id local --session-id default
```

```powershell
# PowerShell
vexic mcp-stdio --db-path .\memory.db --tenant-id local --session-id default
```

The server exposes two read-only tools to the agent: `recall_conversation_history`
(this session's transcript) and `recall_user_memory` (durable facts and
preferences). `recall_user_memory` embeds the query locally; install the
optional extra with `pip install "vexic[local-embed]"` to enable it. See
[`docs/usage.md`](https://github.com/PyroDonkey/vexic/blob/main/docs/usage.md)
for MCP client setup, the transcript recorder, and smoke-test examples.

From a source checkout (no install), run the same server through `uv`:

```bash
uv run python scripts/vexic-mcp-stdio.py --db-path ./memory.db --tenant-id local --session-id default
```

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
[`docs/configuration.md`](https://github.com/PyroDonkey/vexic/blob/main/docs/configuration.md).

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
`PyroDonkey/vexic-website` repository (open-core boundary) — see
[ADR 0012's addendum](https://github.com/PyroDonkey/vexic/blob/main/docs/adr/0012-vexic-console-implementation-path.md).

## Package Boundary

The repository root remains `uv`-managed; do not add Node package files at the
root. Console and website ownership moved out of this repository entirely
(see Repository Map above) — there is no in-repo Node package surface to
isolate anymore.

## Contributing

Working from a source checkout? Clone the repository and run the test suite with
[uv](https://docs.astral.sh/uv/):

```bash
uv run pytest
```

See
[CONTRIBUTING.md](https://github.com/PyroDonkey/vexic/blob/main/CONTRIBUTING.md)
for setup and the branch workflow,
[SECURITY.md](https://github.com/PyroDonkey/vexic/blob/main/SECURITY.md) for
reporting vulnerabilities, and
[CODE_OF_CONDUCT.md](https://github.com/PyroDonkey/vexic/blob/main/CODE_OF_CONDUCT.md)
for community expectations.

## License

Apache-2.0. See
[LICENSE](https://github.com/PyroDonkey/vexic/blob/main/LICENSE).
