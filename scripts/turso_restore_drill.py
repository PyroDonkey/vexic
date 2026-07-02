"""Thin CLI wrapper for the verify-gated Turso restore drill (COA-273 P5
Task 18).

This script wires the REAL, secret-bearing dependencies -- Turso Platform
API provisioning, canonical-migration import, catalog activation, and
Platform API destroy -- and hands them to `vexic.restore.run_restore_drill`
as injected callables. All secrets (`TURSO_PLATFORM_API_TOKEN`,
`TURSO_ORG`, `TURSO_GROUP`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`) are
read HERE, in the CLI/adapter layer -- `src/vexic/restore.py` itself never
touches the environment or any adapter.

Usage:
    uv run python scripts/turso_restore_drill.py \\
        --tenant-id tenant_abc123 \\
        --project-id proj_def456 \\
        --artifact-path /path/to/canonical-migration.json \\
        --hosted-root .hosted-memory \\
        --expected-row-counts messages=42,long_term_memory=7

The drill:
    1. Provisions an isolated replacement Turso database
       (`vexic-restore-drill-{tenant_id}-{token_hex}`) via
       `TursoProvisioningPort.create_database` + `mint_token` (full-access,
       short expiration).
    2. Imports the canonical migration artifact into it via
       `import_canonical_migration`.
    3. Verifies row counts on the replacement match `--expected-row-counts`
       (a simple, documented count-based gate over the canonical tables --
       FTS/vector projection parity is NOT separately counted here, but is
       implied: `import_canonical_migration` always runs
       `repair_memory_projections`, which rebuilds those projections from
       whatever canonical rows just landed, so a canonical row-count match
       means the projections were rebuilt from the same rows).
    4. On success: repoints the catalog at the replacement via
       `HostedTenantCatalog.activate_replacement_database` (bumps
       `generation`, quarantining the pre-repoint handle) and leaves the
       replacement database in place as the new customer target.
    5. On verify failure (or any exception before activation): destroys the
       replacement Turso database via `TursoProvisioningPort.destroy_database`
       and leaves the original tenant target untouched/active.

Never logs or prints a minted token or the platform API token.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from adapters.turso_adapter import TursoProvisioningPort
from vexic.hosted_local import HostedTenantCatalog
from vexic.migration import CANONICAL_TABLES, import_canonical_migration
from vexic.restore import RestoreDrillResult, run_restore_drill
from vexic.storage.connection import StorageTarget, connect


class _ReplacementHandle(NamedTuple):
    """What `provision_replacement()` hands to every later stage: the
    Turso database NAME (needed for `destroy_database`) and a connectable
    `StorageTarget` (DSN + a full-access token minted for the drill)."""

    db_name: str
    target: StorageTarget


def _parse_expected_row_counts(raw: str) -> dict[str, int]:
    expected: dict[str, int] = {}
    if not raw.strip():
        return expected
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        table_name, _, count_str = entry.partition("=")
        table_name = table_name.strip()
        if table_name not in CANONICAL_TABLES:
            raise ValueError(f"Unknown canonical table in --expected-row-counts: {table_name!r}")
        expected[table_name] = int(count_str.strip())
    return expected


def _count_rows(target: StorageTarget, table_name: str) -> int:
    with closing(connect(target)) as conn:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
    return int(row[0])


def make_provision_replacement(
    port: TursoProvisioningPort, *, tenant_id: str
):
    def provision_replacement() -> _ReplacementHandle:
        db_name = f"vexic-restore-drill-{tenant_id}-{secrets.token_hex(6)}"
        dsn, token = port.provision(db_name, expiration="30m", read_only=False)
        return _ReplacementHandle(db_name=db_name, target=StorageTarget(dsn, token))

    return provision_replacement


def make_import_canonical(
    artifact_path: Path, *, tenant_id: str, project_id: str | None
):
    def import_canonical(replacement: _ReplacementHandle) -> None:
        import_canonical_migration(
            artifact_path,
            replacement.target,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    return import_canonical


def make_verify(expected_row_counts: dict[str, int]):
    def verify(replacement: _ReplacementHandle) -> bool:
        for table_name, expected_count in expected_row_counts.items():
            if _count_rows(replacement.target, table_name) != expected_count:
                return False
        return True

    return verify


def make_activate(catalog: HostedTenantCatalog, *, tenant_id: str):
    def activate(replacement: _ReplacementHandle) -> None:
        catalog.activate_replacement_database(tenant_id, replacement.target.target)

    return activate


def make_destroy(port: TursoProvisioningPort):
    def destroy(replacement: _ReplacementHandle) -> None:
        port.destroy_database(replacement.db_name)

    return destroy


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--artifact-path", required=True, type=Path)
    parser.add_argument("--hosted-root", default=".hosted-memory", type=Path)
    parser.add_argument(
        "--expected-row-counts",
        default="",
        help="Comma-separated table=count pairs, e.g. 'messages=42,long_term_memory=7'.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    args = _build_arg_parser().parse_args(argv)
    expected_row_counts = _parse_expected_row_counts(args.expected_row_counts)

    port = TursoProvisioningPort.from_env(os.environ)
    catalog = HostedTenantCatalog(args.hosted_root)

    result: RestoreDrillResult[_ReplacementHandle] = run_restore_drill(
        provision_replacement=make_provision_replacement(port, tenant_id=args.tenant_id),
        import_canonical=make_import_canonical(
            args.artifact_path, tenant_id=args.tenant_id, project_id=args.project_id
        ),
        verify=make_verify(expected_row_counts),
        activate=make_activate(catalog, tenant_id=args.tenant_id),
        destroy=make_destroy(port),
    )

    if result.activated:
        print(
            f"Restore drill PASSED for tenant {args.tenant_id!r}: "
            f"replacement {result.replacement.db_name!r} activated."
        )
        return 0
    print(
        f"Restore drill FAILED verification for tenant {args.tenant_id!r}: "
        f"replacement {result.replacement.db_name!r} destroyed; original tenant target untouched."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
