"""One-off migration of the hosted control-plane catalog between storage
targets.

The as-shipped move is the local ``control-plane.db`` on the Railway volume ->
a managed Turso/libSQL control-plane database, activated by
``VEXIC_CONTROL_PLANE_TARGET=turso``. The copy requires an empty target
(raises ``TargetNotEmptyError`` otherwise), walks tables parents-first so
foreign-key enforcement cannot reject a child row, and uses plain ``INSERT``
so any constraint conflict surfaces instead of being silently dropped.
Completeness is then an exact per-table row-count match. It reports only
per-table row counts and never emits API-key hashes, tokens, or DSNs.

``src/vexic`` never reads provider secrets: ``migrate_control_plane`` takes an
already-resolved target (a filesystem ``Path``/``str`` or a ``StorageTarget``).
The CLI ``main`` builds the Turso ``StorageTarget`` through a lazy
``adapters.turso_adapter`` import (the only place credentials are read), exactly
as the hosted service factory does.
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.storage.connection import StorageTarget, connect

ControlPlaneTarget = str | Path | StorageTarget


@dataclass(frozen=True)
class TableMigration:
    """Row-count outcome for one copied table."""

    table: str
    source_rows: int
    target_rows_after: int

    @property
    def complete(self) -> bool:
        """True when the target holds exactly the source rows. The target is
        required to start empty (see ``migrate_control_plane``), so an exact
        match is the real verification: fewer means rows were dropped, more
        would mean the empty-target precondition was bypassed."""
        return self.target_rows_after == self.source_rows


def _open(target: ControlPlaneTarget) -> sqlite3.Connection:
    if isinstance(target, StorageTarget):
        return connect(target)
    return connect(target, timeout=30)


def _ensure_target_schema(target: ControlPlaneTarget) -> None:
    """Create the full control-plane schema on the target if absent.

    Both stores' constructors run their ``CREATE TABLE IF NOT EXISTS`` DDL
    against the supplied control target, so instantiating them is the
    idempotent way to materialize every catalog and API-key table.
    ``HostedTenantCatalog`` needs a filesystem root only for its local
    customer-db bookkeeping directory, never for the control target itself, so
    a throwaway temp dir is sufficient.
    """
    if isinstance(target, StorageTarget):
        with tempfile.TemporaryDirectory() as tmp:
            HostedTenantCatalog(tmp, control_target=target)
            HostedApiKeyStore(control_target=target)
    else:
        root = Path(target).parent
        HostedTenantCatalog(root, control_target=target)
        HostedApiKeyStore(control_target=target)


def _ordered_tables(conn: sqlite3.Connection) -> list[str]:
    """Every non-internal source table, in foreign-key dependency order:
    a referenced (parent) table always precedes the tables that reference it.

    Alphabetical order would copy children such as ``tenant_projects`` before
    ``tenants``; on a target that enforces foreign keys (Turso may) those
    inserts fail and leave a partially populated catalog. Ordering by
    ``PRAGMA foreign_key_list`` avoids that regardless of the target's FK
    enforcement. Table names come from the source's own ``sqlite_master``.
    """
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    ]
    table_set = set(tables)
    parents: dict[str, set[str]] = {}
    for table in tables:
        refs = {
            row[2]
            for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
        parents[table] = (refs & table_set) - {table}

    ordered: list[str] = []
    placed: set[str] = set()

    def _visit(table: str, on_stack: frozenset[str]) -> None:
        if table in placed:
            return
        for parent in sorted(parents[table]):
            if parent not in on_stack:  # ignore any cycle edge (schema is a DAG)
                _visit(parent, on_stack | {table})
        placed.add(table)
        ordered.append(table)

    for table in tables:
        _visit(table, frozenset())
    return ordered


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> None:
    columns = [row[1] for row in src.execute(f"PRAGMA table_info({table})").fetchall()]
    if not columns:
        return
    col_list = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    rows = src.execute(f"SELECT {col_list} FROM {table}").fetchall()
    if not rows:
        return
    # Plain INSERT into a verified-empty target: any constraint conflict is a
    # real anomaly that must surface, not be silently swallowed by OR IGNORE.
    dst.executemany(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
        rows,
    )


class TargetNotEmptyError(RuntimeError):
    """Raised when the target already holds control-plane rows.

    The cutover copies into a freshly provisioned (empty) database. Refusing a
    non-empty target is what makes the plain-``INSERT`` copy and the exact
    row-count verification trustworthy: it rules out silently dropping a source
    row that collides with a pre-existing, different target row. Recover by
    recreating a fresh target database and re-running."""


def migrate_control_plane(
    source: ControlPlaneTarget,
    target: ControlPlaneTarget,
    *,
    ensure_target_schema: bool = True,
) -> list[TableMigration]:
    """Copy every control-plane table from ``source`` into an EMPTY ``target``.

    Returns one ``TableMigration`` per source table with source/after row
    counts. The target must contain no control-plane rows (raises
    ``TargetNotEmptyError`` otherwise); copy into a fresh database. Tables are
    copied parents-first so foreign-key enforcement on the target cannot reject
    a child row. Table identifiers come from the source's own ``sqlite_master``
    (never external input), so the f-string SQL interpolates trusted names.
    """
    if ensure_target_schema:
        _ensure_target_schema(target)

    results: list[TableMigration] = []
    src = _open(source)
    try:
        dst = _open(target)
        try:
            tables = _ordered_tables(src)
            nonempty = [t for t in tables if _count(dst, t) > 0]
            if nonempty:
                raise TargetNotEmptyError(
                    "target control plane is not empty; migrate into a fresh "
                    f"database (populated tables: {', '.join(sorted(nonempty))})"
                )
            for table in tables:
                source_rows = _count(src, table)
                _copy_table(src, dst, table)
                dst.commit()
                results.append(
                    TableMigration(
                        table=table,
                        source_rows=source_rows,
                        target_rows_after=_count(dst, table),
                    )
                )
        finally:
            dst.close()
    finally:
        src.close()
    return results


def _format_report(results: list[TableMigration]) -> str:
    width = max((len(r.table) for r in results), default=5)
    lines = ["Control-plane migration (counts only):"]
    for r in results:
        flag = "ok" if r.complete else "INCOMPLETE"
        lines.append(
            f"  {r.table.ljust(width)}  source={r.source_rows:>6}  "
            f"target={r.target_rows_after:>6}  {flag}"
        )
    complete = all(r.complete for r in results)
    lines.append("All tables migrated." if complete else "MIGRATION INCOMPLETE.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vexic.migrate_control_plane",
        description=(
            "Copy the hosted control-plane catalog from a local SQLite file to "
            "a managed Turso/libSQL target. Reports row counts only."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to the source control-plane.db (local SQLite file).",
    )
    parser.add_argument(
        "--target-from-env",
        action="store_true",
        help=(
            "Build the Turso target StorageTarget from TURSO_DATABASE_URL / "
            "TURSO_AUTH_TOKEN via adapters.turso_adapter (the only secret read)."
        ),
    )
    parser.add_argument(
        "--target-file",
        help="Alternative local target file path (for drills/tests).",
    )
    args = parser.parse_args(argv)

    if args.target_from_env == bool(args.target_file):
        parser.error("pass exactly one of --target-from-env or --target-file")

    if args.target_from_env:
        import os

        from adapters.turso_adapter import control_plane_target

        target: ControlPlaneTarget = control_plane_target(dict(os.environ))
    else:
        target = Path(args.target_file)

    results = migrate_control_plane(Path(args.source), target)
    print(_format_report(results))
    return 0 if all(r.complete for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
