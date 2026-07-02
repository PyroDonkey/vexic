# Vexic Website

Marketing/landing site for Vexic as a repo-local Next.js App Router app.

Boundary: this directory is a repo-local Next.js marketing app, not Vexic
package runtime and not a `vexic.*` entrypoint. Keep memory-core runtime under
`src/vexic`; per ADR 0012, dashboard and web concerns stay outside the memory
core. The repository root and Python core remain `uv`-managed — do not add
Node package files at the repository root.

## Local Checks

Run repository checks from the repository root:

```powershell
uv run pytest
```

Run website checks from this directory:

```powershell
npm install
npm test
npm run build
```

The npm package surface is scoped to `website/` for the Vercel app. It is not
Vexic package runtime and is not part of the Python memory-engine install.

## Vercel

- Root directory: `website/`
- Framework preset: Next.js
- Build command: `npm run build`

## Environment

Optional (all have defaults):

- `NEXT_PUBLIC_SITE_URL` — canonical site origin for metadata, robots, and
  sitemap. Defaults to `https://vexic.dev`.
- `NEXT_PUBLIC_CONSOLE_URL` — target of the "Sign in" links. Defaults to
  `https://console.vexic.dev`; set this to the deployed Console URL.

## Waitlist

`POST /api/waitlist` is a stub: it validates the email (shared validator in
`lib/waitlist.mjs`, covered by `npm test`) and acknowledges the signup without
storing it. Wire a durable store or provider before treating signups as real.
The hero, final CTA, and `/pricing` forms all post to this route with a
`source` field for later attribution.

## Pages

- `/` — landing page (hero, problem, how-it-works with animated pipeline,
  features, integrations, quickstart, final CTA)
- `/pricing` — pricing waitlist page
- `/docs`, `/blog` — styled coming-soon stubs
