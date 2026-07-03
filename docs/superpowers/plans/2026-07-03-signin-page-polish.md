# Sign-in Page Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the Clerk sign-up prompt from the console sign-in page, add a "get notified" link to the marketing waitlist, port the marketing hero's ambient dot-lattice animation behind the sign-in card, and point the marketing site's "Sign in" links at `/sign-in`.

**Architecture:** Two independent Next.js apps in one repo: `website/` (marketing, vexic.dev) and `console/` (Clerk-authed control plane, console.vexic.dev). Console changes are confined to the sign-in route, its global CSS, and one copied client component. Website changes are a link-constant swap. No new backend, no new dependencies.

**Tech Stack:** Next.js 16 App Router, React 19, Clerk (`@clerk/nextjs` appearance API), Tailwind 4 (website) / plain CSS classes (console auth page), `node --test` source-assertion suites.

## Global Constraints

- No new npm dependencies in either app (spec: "No new npm dependencies in either app").
- No new backend, database access, or API route in the console (spec non-goal).
- Console auth page uses literal palette values, not console theme tokens: canvas `#131313`, foreground `#e5e2e1`, muted sage `#9aa89e`, emerald accent `#10b981`, panel `#1c1b1b`, border `#2a2a2a` (existing `.auth-*` convention in `console/app/globals.css`).
- Marketing site URL fallback is `process.env.NEXT_PUBLIC_SITE_URL ?? "https://vexic.dev"` — exact same expression the website uses.
- Tests are `node --test tests/*.test.mjs` in each app; run from that app's directory.
- Repo convention: source-assertion tests that `readFileSync` route files and `assert.match` on their content (see `console/tests/route-set.test.mjs`).
- Windows/PowerShell environment: `cd console && npm test` style commands work in PowerShell 7.

---

### Task 1: Console — hide Clerk sign-up prompt, add "get notified" waitlist link

**Files:**
- Modify: `console/app/sign-in/[[...sign-in]]/page.tsx`
- Modify: `console/app/globals.css` (append after `.auth-notice code` block, ~line 199)
- Test: `console/tests/sign-in-page.test.mjs` (create)

**Interfaces:**
- Consumes: existing `signInAppearance` object and `.auth-*` CSS classes.
- Produces: `.auth-notify` / `.auth-notify-link` CSS classes and a `SITE_URL` module constant in the sign-in page; Task 2 modifies the same two files (JSX wrapper + CSS) and its test lives in the same test file.

- [ ] **Step 1: Write the failing test**

Create `console/tests/sign-in-page.test.mjs`:

```js
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const pageSource = () =>
  readFileSync(path.join(root, "app/sign-in/[[...sign-in]]/page.tsx"), "utf8");
const cssSource = () => readFileSync(path.join(root, "app/globals.css"), "utf8");

test("sign-in appearance hides the Clerk sign-up footer action", () => {
  assert.match(pageSource(), /footerAction:\s*\{\s*display:\s*"none"\s*\}/);
});

test("sign-in page links to the marketing waitlist for access requests", () => {
  const source = pageSource();
  assert.match(
    source,
    /const SITE_URL = process\.env\.NEXT_PUBLIC_SITE_URL \?\? "https:\/\/vexic\.dev";/
  );
  assert.match(source, /\$\{SITE_URL\}\/#waitlist/);
  assert.match(source, /Get notified when access opens/);
});

test("auth notify styles use the literal marketing palette", () => {
  const css = cssSource();
  assert.match(css, /\.auth-notify\s*\{[^}]*color:\s*#9aa89e/s);
  assert.match(css, /\.auth-notify-link\s*\{[^}]*color:\s*#e5e2e1/s);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `console/`): `node --test tests/sign-in-page.test.mjs`
Expected: FAIL — all three tests fail (no `footerAction`, no `SITE_URL`, no `.auth-notify`).

- [ ] **Step 3: Implement the page changes**

In `console/app/sign-in/[[...sign-in]]/page.tsx`:

Add `footerAction` to `signInAppearance.elements` (after the `formButtonPrimary` entry):

```ts
  elements: {
    cardBox: { border: "1px solid #2a2a2a", boxShadow: "none" },
    formButtonPrimary: {
      boxShadow: "none",
      fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
      fontWeight: 600
    },
    // Access is waitlist-gated for now; the sign-up prompt row is hidden and
    // replaced by the notify link rendered below the panel.
    footerAction: { display: "none" }
  }
```

Add the site URL constant after the imports (above `signInAppearance`):

```ts
// Marketing site root; the sign-in page borrows its waitlist for
// "get notified" requests instead of exposing self-serve sign-up.
const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://vexic.dev";
```

Add the notify line inside `<main className="auth-page">`, after the `SignIn`/`AuthConfigNotice` conditional:

```tsx
      <p className="auth-notify">
        Don&apos;t have an account yet?{" "}
        <a className="auth-notify-link" href={`${SITE_URL}/#waitlist`}>
          Get notified when access opens →
        </a>
      </p>
```

- [ ] **Step 4: Add the CSS**

Append to `console/app/globals.css` after the `.auth-notice code` rule:

```css
.auth-notify {
  color: #9aa89e;
  font-size: 0.875rem;
}

.auth-notify-link {
  color: #e5e2e1;
  text-decoration: underline;
  text-underline-offset: 3px;
  transition: color 150ms ease;
}

.auth-notify-link:hover {
  color: #10b981;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run (from `console/`): `node --test tests/sign-in-page.test.mjs`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the full console suite**

Run (from `console/`): `npm test`
Expected: PASS — including the existing `console does not expose self-serve sign-up` test (the notify link points at the website waitlist, not `/sign-up`, so it stays green).

- [ ] **Step 7: Commit**

```bash
git add console/app/sign-in console/app/globals.css console/tests/sign-in-page.test.mjs
git commit -m "feat(console): hide sign-up prompt, link sign-in to waitlist"
```

---

### Task 2: Console — ambient dot-lattice animation behind the sign-in card

**Files:**
- Create: `console/components/ambient-canvas.tsx` (verbatim copy of `website/components/ambient-canvas.tsx`)
- Modify: `console/app/sign-in/[[...sign-in]]/page.tsx`
- Modify: `console/app/globals.css` (`.auth-page` rule ~line 145, plus new `.auth-content`)
- Test: `console/tests/sign-in-page.test.mjs` (extend)

**Interfaces:**
- Consumes: `.auth-page` CSS class and the sign-in page JSX from Task 1 (notify link inside the page).
- Produces: `AmbientCanvas` React component at `console/components/ambient-canvas.tsx` exporting `export function AmbientCanvas({ color?, maxOpacity?, speed?, density?, fadeDirection?, className? })` — identical signature to the website original; `.auth-content` CSS class layering content above the canvas.

- [ ] **Step 1: Write the failing tests**

Append to `console/tests/sign-in-page.test.mjs`:

```js
test("sign-in page renders the ambient canvas behind a layered content wrapper", () => {
  const source = pageSource();
  assert.match(source, /import \{ AmbientCanvas \} from "@\/components\/ambient-canvas";/);
  assert.match(source, /<AmbientCanvas[^>]*color="#10b981"/s);
  assert.match(source, /fadeDirection="to-bottom"/);
  assert.match(source, /className="auth-content"/);
});

test("auth page positions content above the canvas", () => {
  const css = cssSource();
  assert.match(css, /\.auth-page\s*\{[^}]*position:\s*relative/s);
  assert.match(css, /\.auth-page\s*\{[^}]*overflow:\s*hidden/s);
  assert.match(css, /\.auth-content\s*\{[^}]*z-index:\s*1/s);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `console/`): `node --test tests/sign-in-page.test.mjs`
Expected: FAIL — the two new tests fail; Task 1's three still pass.

- [ ] **Step 3: Copy the component**

Copy `website/components/ambient-canvas.tsx` to `console/components/ambient-canvas.tsx` unchanged (it is a self-contained `"use client"` component with zero imports beyond React):

Run (from repo root): `Copy-Item website/components/ambient-canvas.tsx console/components/ambient-canvas.tsx`

Then prepend a provenance note under the `"use client"` line so the copies can be reconciled later:

```tsx
"use client";

// Copied verbatim from website/components/ambient-canvas.tsx so the sign-in
// page matches the vexic.dev hero backdrop. Keep the two in sync.
```

- [ ] **Step 4: Mount the canvas and layer the content**

In `console/app/sign-in/[[...sign-in]]/page.tsx`, add the import:

```ts
import { AmbientCanvas } from "@/components/ambient-canvas";
```

Restructure the page component — canvas first, all content inside a `.auth-content` wrapper:

```tsx
export default function SignInPage() {
  return (
    <main className="auth-page">
      {/* Same lattice as the vexic.dev hero; content layers above via .auth-content. */}
      <AmbientCanvas
        color="#10b981"
        maxOpacity={0.5}
        speed={1}
        density={1}
        fadeDirection="to-bottom"
        className="mix-blend-screen"
      />
      <div className="auth-content">
        <p className="auth-wordmark">
          Vexic <span className="auth-wordmark-product">Console</span>
        </p>
        {isClerkConfigured() ? (
          <SignIn routing="path" path="/sign-in" appearance={signInAppearance} />
        ) : (
          <AuthConfigNotice />
        )}
        <p className="auth-notify">
          Don&apos;t have an account yet?{" "}
          <a className="auth-notify-link" href={`${SITE_URL}/#waitlist`}>
            Get notified when access opens →
          </a>
        </p>
      </div>
    </main>
  );
}
```

Note: `mix-blend-screen` is a Tailwind utility and the console app already uses Tailwind 4 with `@import "tailwindcss"`, so the class resolves. The canvas element itself carries `pointer-events-none absolute inset-0` from the component.

- [ ] **Step 5: Update the CSS layering**

In `console/app/globals.css`, change `.auth-page` and add `.auth-content` (the flex column moves to the wrapper so the canvas is not a flex item stretched by `gap`):

```css
.auth-page {
  background: #131313;
  color: #e5e2e1;
  display: grid;
  min-height: 100vh;
  overflow: hidden;
  padding: 48px 20px;
  place-items: center;
  position: relative;
}

.auth-content {
  align-items: center;
  display: flex;
  flex-direction: column;
  gap: 36px;
  position: relative;
  z-index: 1;
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run (from `console/`): `node --test tests/sign-in-page.test.mjs`
Expected: PASS (5 tests).

- [ ] **Step 7: Run the full console suite and a production build**

Run (from `console/`): `npm test`
Expected: PASS.

Run (from `console/`): `npm run build`
Expected: build succeeds (verifies the copied client component compiles under the console's TS config).

- [ ] **Step 8: Commit**

```bash
git add console/components/ambient-canvas.tsx console/app/sign-in console/app/globals.css console/tests/sign-in-page.test.mjs
git commit -m "feat(console): add ambient lattice backdrop to sign-in page"
```

---

### Task 3: Website — point "Sign in" links at the console sign-in route

**Files:**
- Modify: `website/lib/links.ts`
- Modify: `website/components/site-nav.tsx:45` and `website/components/site-nav.tsx:99`
- Modify: `website/components/site-footer.tsx:53`
- Modify: `website/README.md:47`
- Test: `website/tests/route-set.test.mjs` (extend)

**Interfaces:**
- Consumes: existing `CONSOLE_URL` export in `website/lib/links.ts`.
- Produces: `export const SIGN_IN_URL = \`${CONSOLE_URL}/sign-in\`;` consumed by `site-nav.tsx` and `site-footer.tsx`.

- [ ] **Step 1: Write the failing test**

Append to `website/tests/route-set.test.mjs` (add `readFileSync` to the existing `node:fs` import):

```js
test("sign-in links target the console sign-in route", () => {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  const links = readFileSync(join(root, "lib/links.ts"), "utf8");
  assert.match(links, /export const SIGN_IN_URL = `\$\{CONSOLE_URL\}\/sign-in`;/);

  for (const file of ["components/site-nav.tsx", "components/site-footer.tsx"]) {
    const source = readFileSync(join(root, file), "utf8");
    assert.ok(source.includes("SIGN_IN_URL"), `${file} should link via SIGN_IN_URL`);
    assert.ok(
      !source.match(/href=\{CONSOLE_URL\}/),
      `${file} should not link to the console root`
    );
  }
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `website/`): `node --test tests/route-set.test.mjs`
Expected: FAIL — `SIGN_IN_URL` does not exist.

- [ ] **Step 3: Implement the link changes**

`website/lib/links.ts` — add below `CONSOLE_URL`:

```ts
export const SIGN_IN_URL = `${CONSOLE_URL}/sign-in`;
```

`website/components/site-nav.tsx` — change the import and both anchors:

```ts
import { GITHUB_URL, NAV_LINKS, SIGN_IN_URL } from "@/lib/links";
```

Desktop anchor (~line 45): `href={CONSOLE_URL}` → `href={SIGN_IN_URL}`.
Mobile anchor (~line 99): `href={CONSOLE_URL}` → `href={SIGN_IN_URL}`.

`website/components/site-footer.tsx` — same swap: import `SIGN_IN_URL` instead of `CONSOLE_URL`, and the anchor at ~line 53 becomes `href={SIGN_IN_URL}`.

`website/README.md` (~line 47) — update the env-var description:

```markdown
- `NEXT_PUBLIC_CONSOLE_URL` — base URL for the console; "Sign in" links target
  `<console>/sign-in`. Defaults to
```

(keep the rest of the original sentence/default value intact).

- [ ] **Step 4: Run test to verify it passes**

Run (from `website/`): `node --test tests/route-set.test.mjs`
Expected: PASS.

- [ ] **Step 5: Run the full website suite**

Run (from `website/`): `npm test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add website/lib/links.ts website/components/site-nav.tsx website/components/site-footer.tsx website/README.md website/tests/route-set.test.mjs
git commit -m "feat(website): point sign-in links at console /sign-in route"
```

---

### Task 4: Browser verification of the sign-in page

**Files:**
- No source changes expected; fixes loop back into Tasks 1–2 files if issues surface.

**Interfaces:**
- Consumes: everything above.
- Produces: visual proof (screenshot) that the sign-up row is gone, the notify link renders, and the lattice animates behind the card.

- [ ] **Step 1: Start the console dev server and load `/sign-in`**

Start the console app (`npm run dev` in `console/`, or the preview tooling if available) and open `http://localhost:3000/sign-in`.

- [ ] **Step 2: Verify**

- No "Don't have an account? Sign up" row inside the Clerk card (if Clerk env is unconfigured locally, the `AuthConfigNotice` renders instead — the appearance rule can't be visually confirmed, note that instead).
- "Don't have an account yet? Get notified when access opens →" renders below the panel and its href is `https://vexic.dev/#waitlist` (or `NEXT_PUBLIC_SITE_URL`).
- Animated emerald dot lattice visible behind/around the card, fading toward the bottom; card and inputs remain fully interactive.
- No console errors.

- [ ] **Step 3: Screenshot for the record**

Capture a screenshot of the rendered page. No commit unless fixes were needed.
