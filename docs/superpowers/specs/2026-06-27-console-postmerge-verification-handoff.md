# Vexic Console Post-Merge Verification - Codex Handoff

Date: 2026-06-27
Branch: dev
Owner: Ryan
Executor: Codex
Self-contained: yes. Assumes NO prior chat context.

## Background

The Vexic Console UI redesign (shadcn/ui + Tremor, dark/light toggle, React 19
/ Tailwind v4 / Node 22) plus UX polish (loading skeletons, error toasts,
raw-key copy, mobile drawer, BarList usage viz) has been merged to `main` and is
LIVE in production.

- Production commit: `f78df53` ("Merge pull request #52 from PyroDonkey/dev")
- Live URL: https://vexic.dev (Vercel, target=production, state READY)
- `main` and `dev` are even.

Your job: verify what can be verified WITHOUT signing in, review the two late
fix commits that were not in the PR review, and produce a clear "Ryan must
check manually" list for everything that is behind Clerk auth.

## Auth Constraint (read this)

The signed-in console is behind Clerk. Production Clerk keys are domain-locked to
`vexic.dev`. You almost certainly CANNOT complete the authenticated flow:

- Do NOT enter Ryan's (or anyone's) credentials. Logging in on his behalf is out
  of scope. Never type passwords or complete sign-in.
- Localhost cannot authenticate (prod keys reject non-`vexic.dev` origins).
- If you have a browser tool that can reach `https://vexic.dev`, you may load
  public routes and READ what renders, but stop at the login wall.

Everything you cannot reach without auth goes into the manual-review list for
Ryan (see "Required Output").

## Tasks

### 1. Confirm the tree is green at current HEAD

```
git fetch --prune origin
git switch dev
git pull --ff-only origin dev
cd console && npm run build
cd console && node --test tests/*.test.mjs
```
From repo root: `uv run pytest`

Report pass/fail with the actual output. If a dev server is already running and
locks `.next` (EBUSY on build), stop that server first; do not retry blindly.

### 2. Review the two late fix commits (NOT previously reviewed)

These landed on `dev` after the redesign PR review and rode into the merge:

- `1efea40` "Fix project workspace state remount"
- `7b487af` "Fix usage meter aria bounds"

For each: `git show <sha>`. Check correctness, confirm they stay within the
client-UI boundary (see Boundaries), and confirm the aria fix gives valid
`aria-valuemin`/`aria-valuemax`/`aria-valuenow` bounds in
`console/components/tremor/usage-meter.tsx`. Flag anything off. These two are the
highest-risk items because they skipped review.

### 3. Verify the live public surface (no auth)

Against https://vexic.dev :
- `/` landing renders.
- `/sign-in` serves the sign-in shell.
- If you can drive a real browser: check the browser console on `/` and
  `/sign-in` for errors. Specifically confirm there is NO Clerk error like
  "Production Keys are only allowed for domain vexic.dev" (that error means a
  domain/key misconfig). Report the console output.
- Confirm `/console` while unauthenticated redirects to sign-in (gate holds).

Do not attempt to bypass the login wall.

## Boundaries (do not cross)

- Client-UI only. Do NOT change `src/vexic`, Clerk wiring
  (`console/lib/auth.ts`, `console/lib/clerk-config.ts`, the console/root
  layouts, sign-in/sign-up routes), any `console/app/api/control-plane/*` route
  or contract, the `console/lib/control-plane-*.mjs` files, or the landing page.
- Work on `dev` only. No feature/worktree/codex branches unless Ryan names one.
- If verification reveals a real bug, you may fix it test-first using the `tdd`
  skill, within the client-UI boundary, on `dev`. If a fix would require
  crossing a boundary, stop and report instead.

## Using /fuse

`/fuse` runs a panel of models with a judge for high-stakes deliberation
(~4-5x cost). Use it only if review of the two late commits surfaces a
non-obvious correctness or accessibility question worth cross-checking. Skip it
for routine verification. Keep Opus on the panel. /fuse advises; Ryan settles
boundaries.

## Escalation

- Stop after 3 failed verification cycles on the same target; report to Ryan.
- No destructive retry loops.

## Required Output

End your run with two clearly separated sections:

1. "Verified by Codex" - build/tests/late-commit-review/public-surface results
   with actual evidence.
2. "Ryan must check manually" - the explicit authenticated-flow checklist Codex
   could not run, so Ryan knows exactly what to click on https://vexic.dev :
   - sign in -> `/console` loads
   - project list: skeleton -> data, no false-empty flash
   - create project -> redirect to workspace
   - keys: create key -> raw key shows once -> Copy button -> success toast
   - revoke key
   - usage tab: BarList + cap meters render with real numbers
   - support tab loads
   - force a failed action -> error toast (not a blank panel)
   - mobile width -> hamburger drawer opens and closes on nav
   - dark/light toggle persists across reload
   - specifically re-check the two late fixes live: workspace tab switching does
     not lose state (remount fix), and the usage meter reads correctly to a
     screen reader / has sane aria bounds (aria fix)

Then update the tracking Linear issue (COA-243 or its successor) with the
verification result and the manual-review list.
```
