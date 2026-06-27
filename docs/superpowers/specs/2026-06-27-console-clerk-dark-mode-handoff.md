# Vexic Console - Clerk Dark Mode - Codex Handoff

Date: 2026-06-27
Branch: dev
Owner: Ryan
Executor: Codex
Self-contained: yes. Assumes NO prior chat context.

## Background

The Vexic Console (authenticated control-plane UI under `console/`) is a Next.js
app on shadcn/ui + Tailwind v4 + React 19, with a dark/light theme toggle via
`next-themes`. It uses Clerk (`@clerk/nextjs` 6.39.5, Clerk JS v5) for auth.

Problem: the shadcn UI honors dark mode, but the Clerk-rendered components do
NOT. Clerk components render in their own isolated styling context and ignore the
app's Tailwind `dark` class, so they stay light while the rest of the console is
dark.

Goal: make all Clerk UI follow the app theme, including when the user's theme is
"system". Clerk should match the RESOLVED theme (actual dark or light).

## Approach (settled)

Use Clerk's `appearance` prop with the `dark` base theme from `@clerk/themes`,
driven by the resolved `next-themes` value.

Key constraint: `ClerkProvider` currently lives in `console/app/layout.tsx`,
which is a server component. `next-themes` `useTheme()` is client-only. So
introduce a small CLIENT wrapper that reads the resolved theme and renders
`ClerkProvider` with the matching appearance. It must sit INSIDE the existing
`ThemeProvider` (next-themes) so `useTheme()` works.

Current `console/app/layout.tsx` shape (for reference):

```
const body = isClerkConfigured() ? <ClerkProvider>{children}</ClerkProvider> : children;
return (
  <html ...>
    <body>
      <ThemeProvider attribute="class" defaultTheme="dark" enableSystem>
        <TooltipProvider>{body}</TooltipProvider>
      </ThemeProvider>
    </body>
  </html>
);
```

`ClerkProvider` already renders inside `ThemeProvider`, so a client wrapper slots
in cleanly.

### Steps

1. Add the `@clerk/themes` dependency. Verify the version is compatible with
   `@clerk/nextjs` 6.39.5 / Clerk JS v5 by checking current upstream docs before
   pinning. Do not bump `@clerk/nextjs`.

2. Create a client component, e.g.
   `console/components/clerk-theme-provider.tsx`:
   - `"use client"`
   - `import { useTheme } from "next-themes"`
   - `import { ClerkProvider } from "@clerk/nextjs"`
   - `import { dark } from "@clerk/themes"`
   - Read `resolvedTheme` from `useTheme()` (this collapses "system" to the
     actual dark/light, satisfying the "follow resolved theme" requirement).
   - Render `<ClerkProvider appearance={{ baseTheme: resolvedTheme === "dark" ? dark : undefined }}>{children}</ClerkProvider>`.
   - Handle the hydration/first-paint case: `resolvedTheme` is undefined until
     mounted. Avoid a hydration mismatch (e.g. a mounted guard, or render
     children with a stable default until mounted). Pick the approach that does
     not flash and does not warn in the console.

3. In `console/app/layout.tsx`, replace the inline `ClerkProvider` with the new
   client wrapper, keeping the existing `isClerkConfigured()` gate exactly as is
   (when Clerk is not configured, still render children without the provider).

4. Confirm the appearance updates LIVE when the theme toggle is used (the
   wrapper re-renders on `resolvedTheme` change), not only on reload.

## Scope Limits

- This task is ONLY Clerk dark mode. Do NOT fix other UI bugs in this change;
  Ryan has noted separate UI cleanup that will be handled on its own.
- Theme must follow the resolved theme (system -> actual dark/light).

## Boundaries (do not cross)

- Client-UI / theming only. This task DOES legitimately edit
  `console/app/layout.tsx` and add a small client component plus the
  `@clerk/themes` dependency. That is allowed because it is cosmetic appearance
  wiring.
- Do NOT change Clerk AUTH behavior: no change to keys, `console/lib/auth.ts`,
  `console/lib/clerk-config.ts`, the `isClerkConfigured()` gate logic, sign-in /
  sign-up routing, middleware, or org gating. Appearance only.
- Do NOT touch `src/vexic`, `console/app/api/control-plane/*`,
  `console/lib/control-plane-*.mjs`, or the public landing page.
- Work on `dev` only. Sync first: `git fetch --prune origin`,
  `git switch dev`, `git pull --ff-only origin dev`. No feature/worktree/codex
  branches unless Ryan names one.

## Method

- Use the `tdd` skill. The themeable seam is the resolved-theme -> baseTheme
  mapping: extract that decision into a tiny pure function (e.g.
  `clerkBaseThemeFor(resolvedTheme)`) and unit-test it (dark -> dark theme,
  light/undefined -> undefined), matching the existing
  `console/lib/console-ui-state.mjs` + `console/tests/*.test.mjs` pattern. The
  provider wiring itself is verified by build + manual smoke.
- This is a single small change. No subagent split needed; one writer.

## Using /fuse

Skip /fuse for the implementation. Use it ONLY if the `@clerk/themes` x Clerk JS
v5 x React 19 SSR/hydration interaction turns out to be non-obvious (e.g.
hydration mismatch you cannot cleanly resolve) and you want a cross-model check.
Keep Opus on the panel. /fuse advises; Ryan settles boundaries.

## Verification

```
cd console
npm run build
node --test tests/*.test.mjs
```
From repo root: `uv run pytest`

Then commit on `dev`, push to `origin/dev`, and open or update the tracking
issue.

## Required Output

End with:

1. "Verified by Codex" - build/tests/unit-test-for-mapping results with evidence.
2. "Ryan must check manually" - the authenticated visual checks Codex cannot run
   without signing in on https://vexic.dev (Clerk is behind login; do NOT enter
   Ryan's credentials):
   - In dark mode: UserButton dropdown, OrganizationSwitcher, UserProfile and
     OrganizationProfile (settings page), and the sign-in / sign-up pages all
     render dark.
   - Toggling the theme updates Clerk UI live without reload.
   - "system" theme resolving to dark also themes Clerk dark.
   - No theme flash or hydration warning in the browser console on load.
```
