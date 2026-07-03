# Security Policy

## Supported versions

Vexic is pre-1.0 and under active development. Security fixes are applied to the
latest released version on the `0.1.x` line.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a vulnerability

Please report security issues privately — do **not** open a public issue for a
suspected vulnerability.

Use GitHub's private vulnerability reporting: go to the repository's **Security**
tab and choose **Report a vulnerability**. This opens a private advisory visible
only to the maintainers.

When reporting, please include:

- a description of the issue and its impact,
- steps to reproduce or a proof of concept, and
- affected version(s) and environment.

We aim to acknowledge reports within a few days and will keep you updated as we
investigate. Once a fix is available, we will coordinate disclosure and credit
reporters who wish to be named.

## Scope notes

Vexic is local-first: the core reads and writes a local SQLite database and does
not exfiltrate data. Hosted surfaces in this repository are internal-alpha
adapter code, not a public service contract. Secrets and provider credentials
are supplied by the host and are never read directly by the memory core.
