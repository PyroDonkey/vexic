# Contributing to Vexic

Thanks for your interest in Vexic, a local-first, provenance-first memory core
for long-running AI agents. This guide covers local setup, the branch workflow,
and the checks we expect before a change lands.

## Development setup

Vexic is a Python 3.13 package managed with [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync            # create the environment and install dependencies
uv run pytest      # run the conformance and reliability suite
```

The optional extras mirror the runtime surfaces:

```bash
uv sync --extra local-embed   # local embedding backend (fastembed)
uv sync --extra hosted        # hosted adapter deps (fastapi, libsql, uvicorn)
```

To try the local read-only MCP server against a Vexic database:

```bash
uv run python scripts/vexic-mcp-stdio.py \
  --db-path ./memory.db --tenant-id local --session-id default
```

## Branch workflow

We use short-lived feature branches that merge into `dev`, and `dev` merges into
`main`:

```
type/<slug>  →  dev  →  main
```

- Branch names use a `type/` prefix (`feat/`, `fix/`, `docs/`, `chore/`, …) and
  a short kebab-case slug describing the change.
- Open pull requests against `dev`. Do not push directly to `main`.
- Keep changes focused; prefer several small PRs over one large one.

## Before you open a PR

Run the relevant checks and confirm they pass:

```bash
uv run pytest                                 # tests
uv run python scripts/check_doc_drift.py --ci # docs match code
```

The doc-drift check guards a few invariants (the ADR index matches the ADR
files; the public contract matches the reference service). If it fails, update
the docs it points at rather than silencing it.

## Architecture decisions

Non-trivial design choices are recorded as ADRs under
[`docs/adr/`](docs/adr/). Read the relevant ADRs before changing behavior in an
area they cover, and add a new ADR (and list it in `docs/adr/README.md`) when a
change makes a decision worth recording.

## Documentation

Repo docs describe architecture, the contract, glossary, and runbooks. They do
not carry project status or delivery tracking; that lives in the issue tracker,
not the repo. Keep docs downstream of code: when behavior changes, update the docs that
describe it in the same change.

## Reporting bugs and requesting features

Open an issue on GitHub. For anything security-sensitive, follow
[`SECURITY.md`](SECURITY.md) instead of filing a public issue.
