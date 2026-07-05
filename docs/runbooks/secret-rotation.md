# Secret Rotation Runbook

> Role: operator procedure for rotating hosted credentials and keeping live
> secrets out of working trees. Complements the "Compromised Credentials"
> section of `docs/adr/0008-hosted-data-encryption-and-backup-posture.md`.

## Principles

- Working trees hold placeholders, never live secrets. Live values belong in
  the deploy platform's secret store (Railway variables today) or an OS
  keychain, referenced by env at process start.
- Database tokens are minted short-lived **with an `exp` claim**. A token
  without `exp` never expires and must be treated as an incident finding:
  rotate it and re-mint with an expiration window.
- The Turso platform API token is org-wide (it can create databases and mint
  tokens for any tenant database). It is the highest-value secret in the
  system: least privilege, never on disk in a checkout, rotate on any
  suspicion of exposure.

## Rotate the Clerk secret key (console)

1. In the Clerk dashboard, create a replacement secret key and revoke the old
   one. Rotation invalidates active console sessions; schedule accordingly.
2. Update `CLERK_SECRET_KEY` in the deploy platform's secret store.
3. For local development, update the untracked `.env.local` in the private
   `PyroDonkey/vexic-website` repo's `console/` directory (COA-295 — console
   no longer lives in this repo).

## Rotate a Turso database auth token

1. Mint a replacement with an expiration window:
   `turso db tokens create <db> --expiration <window>`.
2. Decode-check the new token carries an `exp` claim before use.
3. Update the consuming environment (deploy platform secret store or local
   env), then invalidate the old token:
   `turso db tokens invalidate <db>` (rotates the signing keys; re-mint any
   other tokens for that database afterwards).

## Rotate the Turso platform API token

1. Create a replacement token in the Turso account settings; store it only in
   the deploy platform secret store.
2. Revoke the old token.
3. Confirm no checkout keeps a live copy (search for `TURSO_PLATFORM_API_TOKEN`
   outside `.env.example` templates).

## After any rotation

- Verify nothing secret-bearing is tracked:
  `git log -p -- .env.turso` must return nothing.
- Follow ADR 0008 "Compromised Credentials" when rotation is
  compromise-driven: revoke, rotate adjacent credentials, audit access, and
  create fresh exports under the new keys.
