# Agent Behavior Examples

> Role: short worked examples of correct in-repo agent behavior.
> Authority: every example is derived from rules in `AGENTS.md`. When this file
> and `AGENTS.md` disagree, `AGENTS.md` wins and this file is reconciled.
> Companion: `docs/agent-runbook.md` for the operating discipline behind these.

These examples show the expected shape of correct behavior. They are not new
rules and they do not relax any rule in `AGENTS.md`.

## Example 1: Branch Sync And A Clean dev -> main Reconciliation

Per the Branch Sync rules in `AGENTS.md`, all Vexic project work happens on
`dev`, and `dev` must not be behind `origin/main` before opening or claiming a
`dev` to `main` PR is ready.

Before starting mutable work, sync from fresh remote refs:

```powershell
git fetch --prune origin
git switch main
git pull --ff-only origin main
git switch dev
git pull --ff-only origin dev
```

If any `git pull --ff-only` fails, stop and report the divergence. Do not merge,
rebase, or reset without Ryan's direction.

Before creating or updating a `dev` to `main` PR, re-check drift while staying
on `dev`:

```powershell
git fetch origin
# merge origin/main into dev only if needed, then:
git push origin dev
git rev-list --left-right --count origin/main...dev
```

The first number is how far `dev` is behind `origin/main` and must be `0` before
opening or claiming the PR is ready. Also confirm GitHub lists only intended
files:

```powershell
gh api repos/PyroDonkey/vexic/compare/main...dev --jq '{behind_by:.behind_by, files:[.files[].filename]}'
```

A clean result looks like `behind_by: 0` with only the files the change set
intended. If `behind_by` is non-zero or unintended files appear, stop. Do not
create a branch to repair PR shape unless Ryan names the branch to create.

## Example 2: A Good Commit Message

`AGENTS.md` directs the agent to commit on `dev` only after fresh verification,
and to prefer a new commit over amending. Commits in this repo use a
Conventional-Commits-style subject. If the agent harness defines a co-author
trailer convention, end the message with that trailer.

```text
docs: add agent operations runbook and behavior examples

Add docs/agent-runbook.md and docs/examples.md to cover agent-run audit
logging, loop bounds and escalation, and the failure-to-rule feedback path.
Examples are grounded in the AGENTS.md Branch Sync commands and the host-port
boundary. Verified with uv run pytest.

Co-Authored-By: <Agent Name> <noreply@example.com>
```

Notes:

- The subject is a short, type-prefixed summary in the imperative mood.
- The body explains what changed and why, and records that verification ran.
- The `Co-Authored-By` trailer is a harness/commit convention, not an AGENTS.md
  rule. Use the exact name and address your own harness specifies (for example,
  Claude Code sets `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`);
  omit it if your harness defines none.
- Commit only after running the relevant fresh checks from the Verification
  section, normally `uv run pytest`.

## Example 3: A Model-Backed Op Fails Closed With HostPortNotConfigured

`AGENTS.md` settles `run_dream_phase` as a host-port operation in v0.1.
`LocalMemoryService` applies the local authorization and tombstone checks, then
executes only when explicit dream-phase host ports are supplied. When those local
checks pass and no host adapter is provided, it fails closed with
`HostPortNotConfigured` through `missing_host_port`. The correct behavior is to
surface that error, not to import private host runtime code or wire a provider
SDK to "fix" it.

Expected behavior when calling a dream phase with a valid, non-tombstoned scope
and no dream-phase ports configured:

```python
service = LocalMemoryService(db_path=str(db_path), tenant_id="tenant-a")
await service.run_dream_phase(request)  # raises HostPortNotConfigured
```

The raised error reads:

```text
Dream phase requires a host-supplied model port. Vexic core does not read
provider secrets or build models directly.
```

This is the intended outcome, not a bug to patch. The same fail-closed posture
applies to other model-backed needs such as embeddings: a missing embedding
adapter fails with `HostPortNotConfigured` rather than reading provider secrets
from the environment. Do not replace host ports with ambient environment reads,
provider SDK wiring, process globals, or private host runtime imports. Implement
a scoped Vexic dream-phase adapter slice only when Ryan asks for that work.
