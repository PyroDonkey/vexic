# Vexic Console UI Redesign - Codex Handoff

Date: 2026-06-27
Branch: dev
Owner: Ryan (directs/reviews architecture)
Executor: Codex (grunt work)

## Goal

Rebuild the Vexic Console UI from scratch on shadcn/ui + Tremor. Fresh look;
the current hand-rolled design is rejected. Keep Clerk auth and the
control-plane API backend exactly as-is. Also update the deprecated Node.js
version.

This is a UI rebuild plus a runtime/dependency bump. It is NOT a backend or
contract change.

## Hard Boundaries (from AGENTS.md - do not cross)

- All work stays under `console/`. Do NOT touch `src/vexic`. Do NOT add Node
  package files at the repo root.
- Console keeps its isolated npm surface in `console/`. It is a Vercel Next.js
  app, not memory-core runtime.
- Work on the `dev` branch only. Do not create feature/worktree/codex branches
  unless Ryan names one.
- Keep Clerk wiring intact and functional:
  - `console/app/console/layout.tsx` (OrganizationSwitcher, UserButton, shell)
  - `console/lib/auth.ts`, `console/lib/clerk-config.ts`
  - `console/app/sign-in/[[...sign-in]]/page.tsx`,
    `console/app/sign-up/[[...sign-up]]/page.tsx`
  - `console/app/console/settings/settings-panels.tsx` keeps embedded Clerk
    `UserProfile` / `OrganizationProfile`.
- Keep every `console/app/api/control-plane/*` route and its JSON contract
  unchanged. The UI consumes the same shapes: `Project`, `AgentKey`, `Usage`,
  `SupportRecord`. No backend behavior change.
- Do not add provider secrets, billing, or public HTTP concerns. Pure UI +
  runtime bump.

## Locked Decisions

- Theme: dark + light with a user toggle (use `next-themes`).
- Scope: authed `/console` views only. Leave the public landing page
  `console/app/page.tsx` as-is.
- Dependencies: may be bumped to a compatible set. Expected: React 19 +
  Tailwind v4 + shadcn init + Next 16.2.9. Verify the build is green BEFORE
  any redesign work.
- Tremor flavor: use Tremor Raw (copy-in components sharing the Tailwind
  config), not the `@tremor/react` npm package, to avoid a second version pin.
  Verify current Tremor Raw upstream before relying on import paths.
- Node.js: bump to Node 22 LTS (active LTS; Node 20 reached EOL ~2026-04).
  Node 24 is an acceptable alternative if Ryan prefers. Update:
  - `console/package.json` `engines.node` (currently `>=20.9.0 <21`)
  - `@types/node` devDependency to the matching major
  - any Vercel project Node version setting / `.nvmrc` if present
  Keep the "pin major for Vercel" intent from recent commits.
- Brand: keep the Vexic logo SVG and a teal accent, expressed as a theme token.

## Known Landmine - fix FIRST

`console/package.json` pins `react`/`react-dom` `18.3.1` alongside
`next 16.2.9`. Next 16 expects React 19. This base is mismatched. Task 1 is to
resolve to a building set (React 19 + Tailwind v4 + shadcn init) and confirm
`next build` is green. Do not start redesign on a broken base.

## Work Breakdown

Build test-first using the `tdd` skill (red-green-refactor) throughout.

1. Runtime + base setup (sequential, blocking - do this alone first):
   - Resolve React/Next version mismatch; confirm `next build` green.
   - Bump Node to 22 LTS across engines, `@types/node`, Vercel/`.nvmrc`.
   - Init Tailwind v4 + shadcn; wire `next-themes` dark/light toggle.
   - Replace `console/app/globals.css` hand-rolled classes with Tailwind base +
     theme tokens (keep teal + logo).
   - Gate: `cd console; npm run build` and `node --test tests/*.test.mjs` pass.

2. Parallelizable view rebuilds (after base is green). One writer per file:
   - Shell: `console/app/console/layout.tsx` sidebar/topbar + theme toggle.
   - Project list: `console/app/console/project-list.tsx`.
   - Project workspace tabs (keys / usage / settings):
     `console/app/console/projects/[projectId]/project-workspace.tsx`.
   - Usage tab charts on Tremor (within the workspace file or an extracted
     chart component).
   - Support view: `console/app/console/support/support-view.tsx`.

3. Verify (sequential, last):
   - `cd console; npm run build` green.
   - `cd console; node --test tests/*.test.mjs` green.
   - Manual smoke: sign in via Clerk, list/create project, create/revoke key,
     usage charts render, support view, theme toggle persists.

## Agent Splitting Guidance

- Do step 1 with a single agent. It is sequential and everything depends on it.
  Do not parallelize the base setup.
- After the base is green, split step 2 across subagents on DISJOINT files so
  edits never collide. Keep one writer per file. Suggested split:
  - Agent A: shell + theme toggle.
  - Agent B: project-list.
  - Agent C: project-workspace + usage/Tremor charts.
  - Agent D: support-view.
- Each subagent shares the same shadcn primitives and theme tokens from step 1;
  it must not re-init Tailwind/shadcn or re-pin deps.
- Reconverge for step 3 verification with a single agent.

## Using /fuse (model deliberation)

`/fuse` emulates OpenRouter's Fusion router: a panel of up to 8 models answers
in parallel, a judge model returns structured comparative analysis (consensus,
contradictions, coverage gaps, unique insights, blind spots), then the outer
model synthesizes the final answer. Default panel includes Claude Opus, GPT,
and Gemini Pro. Cost is roughly 4-5x a single completion.

Use it where the cost of being wrong outweighs a few extra completions:

- USE for: resolving the React/Next/Tailwind version-compatibility matrix
  (high-stakes, fast-moving, easy to get subtly wrong); choosing the shadcn +
  Tremor Raw integration approach under Tailwind v4; any architecture call that
  would otherwise need Ryan.
- DO NOT USE for: routine component scaffolding, simple lookups, mechanical
  edits, or anything speed-sensitive. Single-model is fine there.
- Keep Opus on the panel for architecture/contract-adjacent questions.
- /fuse advises; it does not settle settled boundaries. Architecture, contract,
  and boundary questions still defer to Ryan (AGENTS.md Working Rules).

## Escalation

- Stop after 3 failed verification cycles on the same target; report to Ryan.
- No destructive retry loops (no reset/delete to force a green run).
- If the version mismatch cannot be resolved within pins, report the blocker;
  do not silently downgrade scope.

## Verification Commands

```
cd console
npm run build
node --test tests/*.test.mjs
```
