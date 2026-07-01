# Branch Sync Procedure

> Role: the operational git command sequences for keeping `dev` in sync and for
> checking `dev` to `main` PR drift.
> Authority: the always-loaded hard-stops and the `dev`-only policy live in
> `docs/ai/AGENTS.md` (Repository Workflow > Branch Sync). This doc holds only
> the command steps; it does not relax any rule there.

## Before mutable work

1. `git fetch --prune origin`
2. Update `main` with `git switch main` and `git pull --ff-only origin main`.
3. Update `dev` with `git switch dev` and `git pull --ff-only origin dev`.

If local `dev` is missing but `origin/dev` exists, create the local tracking
branch with `git switch -c dev --track origin/dev`. If `origin/dev` is missing,
create it from updated `main` only when the requester has explicitly asked to
bootstrap the branch workflow; otherwise stop and ask before creating or pushing
it. Do not continue implementation work on `main`.

If any `git pull --ff-only` fails, stop and report the divergence. Do not merge,
rebase, or reset without explicit requester direction. (Always-loaded hard-stop;
see `docs/ai/AGENTS.md`.)

## Before opening or updating a `dev` to `main` PR

Re-check branch drift from fresh remote refs while staying on `dev`:

1. `git fetch origin`
2. Merge `origin/main` into `dev` if needed, then push `origin/dev`.
3. `git rev-list --left-right --count origin/main...dev`. The first number is how
   far `dev` is behind `origin/main`, and it must be `0` before opening or
   claiming the PR is ready.
4. `gh api repos/PyroDonkey/vexic/compare/main...dev --jq '{behind_by:.behind_by, files:[.files[].filename]}'`
   and stop if GitHub lists unintended files.

The `.claude/hooks/check_branch_sync.py` SessionStart hook mirrors this drift
check in read-only form: it fetches origin and reports drift, but never merges.
If it reports drift, sync on `dev` with the commands above before starting work.
