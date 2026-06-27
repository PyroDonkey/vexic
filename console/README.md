# Vexic Console

Small Next.js App Router console source slice for the Vexic control plane.

Boundary: this directory is a repo-local Next.js control-plane app, not Vexic
package runtime and not a `vexic.*` entrypoint. Keep memory-core runtime under
`src/vexic`; Console talks to hosted control-plane surfaces as a client.

The control-plane API routes are stubs until hosted endpoints are live. The
repository root and Python core remain `uv`-managed.

## Local Checks

Run repository checks from the repository root:

```powershell
uv run pytest
```

Run Console checks from this directory:

```powershell
npm install
npm test
npm run build
```

The npm package surface is scoped to `console/` for the Vercel app. It is not
Vexic package runtime and is not part of the Python memory-engine install.

## Vercel

- Root directory: `console/`
- Framework preset: Next.js
- Build command: `npm run build`

## Environment

Required:

- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- `CLERK_SECRET_KEY`
- Clerk Organizations enabled, with user-created organizations allowed or at
  least one organization created for the signed-in user. Console routes require
  an active Clerk Organization; personal sessions cannot create Vexic projects
  or agent keys.

Route defaults:

- `NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in`
- `NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up`
- `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/console`
- `NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/console`

Internal support:

- `VEXIC_INTERNAL_ORG_ID`

Reserved until hosted endpoints are wired:

- `VEXIC_HOSTED_API_BASE_URL`

## Stub Control Plane

The current `/api/control-plane/*` routes use an in-memory store. Projects,
usage, support metadata, and `vx_live_*` Agent API Keys are local stubs for
Console smoke testing. Generated keys are shown once, can be revoked in the UI,
and are not accepted by a durable hosted Vexic API until the production control
plane is wired.
