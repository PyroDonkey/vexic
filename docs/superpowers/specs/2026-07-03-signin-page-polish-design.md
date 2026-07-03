# Sign-in Page Polish — Design

Date: 2026-07-03
Status: Approved

## Goal

Four small changes across the two web apps:

1. Remove the "Don't have an account? Sign up" footer action from the Clerk sign-in panel (console).
2. Add a "get notified" affordance on the sign-in page that links to the existing marketing-site waitlist (console).
3. Add the marketing hero's ambient dot-lattice animation behind the sign-in page (console).
4. Point the marketing site's "Sign in" links at the console's `/sign-in` route instead of the console root (website).

## Non-goals

- No new backend, database access, or API route in the console. The "get notified" affordance is a link to the marketing waitlist, not an inline form (decided: single source of truth stays the website's Turso-backed waitlist).
- No new npm dependencies in either app.
- No changes to Clerk configuration outside the `appearance` prop.

## Design

### console/

**1. Hide sign-up prompt** — `console/app/sign-in/[[...sign-in]]/page.tsx`

Add to `signInAppearance.elements`:

```ts
footerAction: { display: "none" }
```

This removes the "Don't have an account? Sign up" row while leaving the rest of the Clerk card (including "Secured by Clerk" branding rules) untouched.

**2. "Get notified" link**

Below the `<SignIn>` panel, render a small line:

> Don't have an account yet? **Get notified when access opens →**

- Href: `${SITE_URL}/#waitlist` where `SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://vexic.dev"` (mirrors the website's own fallback).
- Styled via new `.auth-notify` rules in `console/app/globals.css`, using the same literal auth palette (#9aa89e muted, #e5e2e1 foreground, #10b981 accent) as the existing `.auth-*` classes.
- Rendered in both the configured (`<SignIn>`) and unconfigured (`AuthConfigNotice`) states.

**3. Ambient background animation**

- Copy `website/components/ambient-canvas.tsx` verbatim to `console/components/ambient-canvas.tsx` (self-contained client component: canvas + rAF loop, IntersectionObserver, ResizeObserver, `prefers-reduced-motion` handling; zero dependencies).
- `.auth-page` gains `position: relative; overflow: hidden`.
- Mount `<AmbientCanvas color="#10b981" maxOpacity={0.5} speed={1} density={1} fadeDirection="to-bottom" className="mix-blend-screen" />` as the first child of `<main className="auth-page">`.
- Wrap the wordmark, panel, and notify link in `.auth-content` (`position: relative; z-index: 1; display: flex; flex-direction: column; align-items: center; gap: 36px`) so content paints above the canvas; move the flex layout from `.auth-page` onto `.auth-content` as needed while keeping the page centered.
- Canvas stays `pointer-events-none` and `aria-hidden`; no interaction or a11y impact.

### website/

**4. Sign-in links → `/sign-in`**

- `website/lib/links.ts`: add `export const SIGN_IN_URL = `${CONSOLE_URL}/sign-in`;`
- Swap `CONSOLE_URL` → `SIGN_IN_URL` in the three "Sign in" anchors:
  - `website/components/site-nav.tsx` (desktop, ~line 45)
  - `website/components/site-nav.tsx` (mobile, ~line 99)
  - `website/components/site-footer.tsx` (~line 53)
- Update `website/README.md` line describing `NEXT_PUBLIC_CONSOLE_URL` to note links target `/sign-in`.

## Error handling

- Nothing new: the link is static; the canvas already degrades to a single static frame under reduced motion and skips rendering entirely when 2D context is unavailable.
- Unconfigured Clerk env keeps the existing `AuthConfigNotice` path.

## Testing / verification

- Console: run dev server, verify sign-up footer row gone, notify link present and pointing at `https://vexic.dev/#waitlist` (or `NEXT_PUBLIC_SITE_URL`), animation renders behind the card, form remains interactive.
- Website: verify all three "Sign in" anchors resolve to `${CONSOLE_URL}/sign-in`.
- Existing test suites (`node --test`) in both apps still pass.
