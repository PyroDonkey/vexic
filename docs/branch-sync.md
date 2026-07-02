# Branch Sync Procedure

> Role: the operational git command sequences for the feature -> `dev` ->
> `main` workflow and for keeping `dev` and `main` from drifting.
> Authority: the always-loaded hard-stops, the branching model, and the naming
> scheme live in `docs/ai/AGENTS.md` (Repository Workflow). This doc holds only
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

## Starting a feature branch

From fresh `dev` (sequence above):

1. `git switch -c <type>/coa-<id>-<slug> dev` (naming scheme in
   `docs/ai/AGENTS.md`; `chore/<slug>` when no Linear issue exists).
2. Work, verify, commit on the feature branch.
3. `git push -u origin <branch>` and open a PR into `dev` with
   `Fixes COA-<id>` in the description. Squash-merge; the branch auto-deletes.

If `dev` moved while the branch was open and the PR conflicts, merge fresh
`origin/dev` into the feature branch (or rebase if the branch has not been
shared beyond the PR) and re-verify.

## Before opening or updating a `dev` to `main` release PR

Re-check branch drift from fresh remote refs while staying on `dev`:

1. `git fetch origin`
2. `git rev-list --left-right --count origin/main...dev`. The first number is
   how far `dev` is behind `origin/main`, and it must be `0` before opening or
   claiming the PR is ready. If it is nonzero, run the post-release
   fast-forward below first; if `--ff-only` fails, stop and report.
3. `gh api repos/PyroDonkey/vexic/compare/main...dev --jq '{behind_by:.behind_by, files:[.files[].filename]}'`
   and stop if GitHub lists unintended files.

Release PRs merge with a merge commit (enforced by the `main` ruleset, along
with the required `test` check).

## Immediately after a release PR merges

The merge commit lands on `main` only, so `dev` now reads "behind main by 1".
Fast-forward `dev` before any other work:

1. `git fetch origin`
2. `git switch dev && git merge --ff-only origin/main`
3. `git push origin dev`

This always fast-forwards because `main` is a merge of `dev`. If `--ff-only`
fails, something else landed on `main` (for example a hotfix that was not
back-merged): stop and report; do not merge or rebase without direction.

## Hotfix (rare)

1. Cut `fix/coa-<id>-<slug>` from fresh `main`.
2. PR into `main`, merge, confirm the deploy.
3. Immediately merge `main` back into `dev`:
   `git switch dev && git fetch origin && git merge origin/main && git push origin dev`.

The `.claude/hooks/check_branch_sync.py` SessionStart hook mirrors the drift
check in read-only form: it fetches origin and reports drift, but never merges.
If it reports drift, run the matching sequence above before starting work.
