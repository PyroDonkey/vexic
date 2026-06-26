# Vexic Console

Small Next.js App Router console source slice for COA-230.

Boundary: this directory is a repo-local Next.js control-plane app, not Vexic
package runtime and not a `vexic.*` entrypoint. Keep memory-core runtime under
`src/vexic`; Console talks to hosted control-plane surfaces as a client.

The control-plane API routes are stubs until hosted endpoints are live.
This repo is managed with `uv` only; do not add JavaScript package manager
files or install/test/build commands here until the console packaging path is
changed deliberately.
