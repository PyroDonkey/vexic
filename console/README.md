# Vexic Console

Small Next.js App Router console source slice for the Vexic control plane.

Boundary: this directory is a repo-local Next.js control-plane app, not Vexic
package runtime and not a `vexic.*` entrypoint. Keep memory-core runtime under
`src/vexic`; Console talks to hosted control-plane surfaces as a client.

The control-plane API routes are stubs until hosted endpoints are live. The
repository root and Python core remain `uv`-managed.

## Local Checks

```powershell
uv run pytest
```

Run repository checks from the repository root. This source slice does not carry
its own package-manager manifest, lockfile, or alternate install/test flow.

## Vercel

- Root directory: `console/`

## Environment

Required:

- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- `CLERK_SECRET_KEY`
- `VEXIC_HOSTED_API_BASE_URL`

Route defaults:

- `NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in`
- `NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up`
- `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/console`
- `NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/console`

Internal support:

- `VEXIC_INTERNAL_ORG_ID`
