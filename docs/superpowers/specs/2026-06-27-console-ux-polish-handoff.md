# Vexic Console UX Polish - Codex Handoff

Date: 2026-06-27
Branch: dev
Owner: Project maintainer (directs/reviews architecture)
Executor: Codex (grunt work)
Self-contained: yes. This brief assumes NO prior chat context.

## Background

The Vexic Console (an authenticated control-plane UI under `console/`) was just
rebuilt from scratch on shadcn/ui + Tremor + Tailwind v4 + React 19 + Node 22,
with a dark/light theme toggle (`next-themes`). That redesign is merged on `dev`
(commit `980c302`, "Redesign Vexic Console on shadcn"). Clerk auth and the
`/api/control-plane/*` API are unchanged and working. `npm run build`,
`node --test`, and `uv run pytest` are all green as of that commit.

The functional surface is complete: shell, project list, project workspace
(keys / usage / settings tabs), settings (embedded Clerk profiles), and support
view all render against live endpoints. What remains is UX polish. This brief
covers that polish. It does NOT touch the public landing page or any backend.

## What Exists Now (read these first)

- Shell + nav + theme toggle: `console/app/console/layout.tsx`
- Root providers (ClerkProvider, ThemeProvider, TooltipProvider):
  `console/app/layout.tsx`
- Project list: `console/app/console/project-list.tsx`
- Project workspace (tabs): `console/app/console/projects/[projectId]/project-workspace.tsx`
- Settings (Clerk): `console/app/console/settings/settings-panels.tsx`
- Support: `console/app/console/support/support-view.tsx`
- Usage meter (progress bar, NOT a chart):
  `console/components/tremor/usage-meter.tsx`
- shadcn primitives present: `console/components/ui/` (badge, button, card,
  input, separator, table, tabs, tooltip). Theme toggle + provider in
  `console/components/`.
- API routes + JSON contracts: `console/app/api/control-plane/*` and
  `console/lib/control-plane-api.mjs`, `console/lib/control-plane-store.mjs`.

Data shapes the UI consumes (do not change these):
- `Project { id, name, environment, createdAt }`
- `AgentKey { id, name, capability, agentScope, display, createdAt }`
- `Usage { periodStart, periodEnd, totals: Record<string,number>, caps: Record<string,number> }`
- `SupportRecord { ticketId, orgId, projectIds[], status, createdAt, updatedAt }`

## Hard Boundaries (do not cross)

- All work stays under `console/`. Do NOT touch `src/vexic`. Do NOT add Node
  package files at the repo root.
- Do NOT change Clerk wiring: `console/app/console/layout.tsx`,
  `console/app/layout.tsx`, `console/lib/auth.ts`,
  `console/lib/clerk-config.ts`, sign-in/sign-up routes, and the embedded
  `UserProfile` / `OrganizationProfile` in settings.
- Do NOT change any `console/app/api/control-plane/*` route or its JSON
  contract, and do NOT change `console/lib/control-plane-*.mjs`. This is a
  client-UI-only change.
- Do NOT redesign or restyle the public landing page (`console/app/page.tsx`)
  or its `globals.css` landing rules.
- Work on the `dev` branch only. No feature/worktree/codex branches unless the
  requester names one. Before starting, sync dev: `git fetch --prune origin`,
  `git switch dev`, `git pull --ff-only origin dev`.

## Tasks (ranked)

Build test-first using the `tdd` skill (red-green-refactor) throughout.

### P1 - Loading states (highest value)

Every view fetches in `useEffect` and renders an empty/false state until data
arrives, so users briefly see "No projects yet" / "No support records" / blank
usage before real data loads.

- Add a shadcn `Skeleton` primitive (`console/components/ui/skeleton.tsx`).
- Add an explicit `loading` state to each fetching view (project-list,
  project-workspace keys+usage, support-view). Render skeleton rows/cards while
  loading; only show the empty state after a successful fetch returns zero rows.
- Distinguish "loading", "empty", and "error" as three separate states.

### P1 - Error states + toasts

Failed fetches are currently swallowed; `project-list` shows a hardcoded
"requires an active organization" message for ANY non-ok response (even a 500).

- Add shadcn `sonner` toast (`Toaster` in `console/app/layout.tsx` or the
  console layout).
- On fetch/mutation failure, surface a real error toast. Replace the hardcoded
  project-create message with branching: 403/insufficient-org vs generic
  failure.
- Add an inline error state (not just a toast) for list loads that fail, so the
  view does not look empty when it actually errored.

### P1 - Raw key copy button

In the workspace keys tab, the one-time raw key is shown inline for manual
selection.

- Add a copy-to-clipboard button (`navigator.clipboard.writeText`) with a
  success toast. Keep the "shown once" warning.

### P2 - Real Tremor usage charts

`usage-meter.tsx` is a progress bar, not a chart. Replace or augment the usage
tab with real Tremor visualizations (e.g. a bar chart of totals vs caps, or per
-metric trend/donut). Use Tremor Raw copy-in components sharing the existing
Tailwind v4 config; verify current Tremor upstream before relying on import
paths. Keep the meter as a fallback for single-value metrics if it reads better.

### P2 - Mobile navigation

The sidebar is `lg:grid`; below the `lg` breakpoint the full `<aside>` stacks on
top of content with no collapse. Add a shadcn `Sheet`-based mobile drawer with a
hamburger trigger in the topbar; keep the desktop grid layout unchanged.

## Agent Splitting Guidance

- If you add new shadcn primitives (`skeleton`, `sonner`, `sheet`), add them
  FIRST in a single agent so all views share one copy. Do not let parallel
  agents each re-add the same primitive.
- After primitives exist, split the per-view work across subagents on DISJOINT
  files, one writer per file:
  - Agent A: project-list (loading + error + toast)
  - Agent B: project-workspace (loading + error + raw-key copy + Tremor charts)
  - Agent C: support-view (loading + error)
  - Agent D: layout (Toaster mount + mobile Sheet nav)
- Reconverge for verification with a single agent.

## Using /fuse (model deliberation)

`/fuse` emulates OpenRouter's Fusion router: a panel of up to 8 models answers
in parallel, a judge model returns structured comparative analysis (consensus,
contradictions, coverage gaps, unique insights, blind spots), then the outer
model synthesizes the final answer. Default panel includes Claude Opus, GPT, and
Gemini Pro. Cost is ~4-5x a single completion. Use it only where the cost of
being wrong outweighs a few extra completions.

- USE for: the Tremor Raw integration approach under Tailwind v4 + React 19
  (fast-moving, easy to get import paths/SSR wrong); deciding the loading-state
  pattern (Suspense vs explicit state) given these are client components.
- DO NOT USE for: adding skeletons, wiring a toast, or a copy button. Those are
  mechanical; single-model is fine.
- Keep Opus on the panel for any architecture-adjacent question.
- /fuse advises; it does not settle boundaries. Architecture/contract/boundary
  questions still defer to the project maintainer.

## Escalation

- Stop after 3 failed verification cycles on the same target; report to the requester.
- No destructive retry loops (no reset/delete to force a green run).
- If a P2 item cannot be done without changing an API contract or a boundary,
  stop and report; do not change the contract.

## Verification

```
cd console
npm run build
node --test tests/*.test.mjs
```

From repo root, confirm nothing else regressed:

```
uv run pytest
```

Add or update `console/tests/*.test.mjs` coverage for new client behavior where
practical (loading/error/empty branching). Manual smoke with a real Clerk
session: sign in, load each view (observe skeleton then data), trigger a failed
action (observe error toast), copy a raw key, resize to mobile (observe drawer),
toggle theme.

## Done Criteria

- P1 items complete and verified; P2 complete or explicitly deferred with a
  reason.
- Build, node tests, and pytest all green.
- No changes outside `console/` client code (plus any `console/tests`).
- Clerk and control-plane API untouched. Landing page untouched.
- Move the tracking issue to In Review with branch, commit, and
  verification result.
