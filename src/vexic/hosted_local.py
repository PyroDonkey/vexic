from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from vexic.contract import MemoryCapability, Principal, PrincipalType
from vexic.hosted import (
    HostedAuditEvent,
    HostedAuthContext,
    HostedJobEvent,
    HostedTenant,
    HostedUsageEvent,
)
from vexic.service import LocalMemoryService
from vexic.storage.connection import StorageTarget, _is_libsql_target, connect
from vexic.storage.errors import is_operational_error


_CONTROL_DB_MODE = 0o600
_CONTROL_PLANE_AGENT_CAPABILITIES = frozenset(
    {
        MemoryCapability.WRITE,
        MemoryCapability.SEARCH,
        MemoryCapability.EXPAND_HISTORY,
        MemoryCapability.FRESH_CONTEXT,
        MemoryCapability.DREAM_TRIGGER,
    }
)

# Split into individual DDL statements (rather than one `executescript()`
# blob) so schema init runs identically over a local `sqlite3.Connection` or
# a hosted libSQL/Turso connection through the `connect()` seam -- neither
# the real libSQL driver nor `FakeLibsqlConn` implements `executescript`.
_CONTROL_PLANE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tenants (
        tenant_id TEXT PRIMARY KEY,
        db_filename TEXT NOT NULL UNIQUE,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        customer_target TEXT,
        generation INTEGER NOT NULL DEFAULT 1,
        retired_at TEXT,
        retired_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_projects (
        tenant_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        PRIMARY KEY (tenant_id, project_id),
        FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_account_mappings (
        clerk_org_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hosted_projects (
        project_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        environment TEXT NOT NULL,
        created_at TEXT NOT NULL,
        retired_at TEXT,
        retired_by TEXT,
        FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hosted_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation TEXT NOT NULL,
        tenant_id TEXT,
        principal_id TEXT,
        status TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        error_type TEXT,
        project_id TEXT,
        key_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hosted_usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        operation TEXT NOT NULL,
        tenant_id TEXT,
        principal_id TEXT,
        status TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        model_requests INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        estimated_cost_micros INTEGER NOT NULL DEFAULT 0,
        error_type TEXT,
        project_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hosted_job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        status TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        phase TEXT,
        error_type TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hosted_audit_events_tenant_id
        ON hosted_audit_events(tenant_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hosted_usage_events_tenant_id
        ON hosted_usage_events(tenant_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hosted_usage_events_tenant_project_recorded_at
        ON hosted_usage_events(tenant_id, project_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hosted_usage_events_tenant_project_recorded_at_jd
        ON hosted_usage_events(tenant_id, project_id, julianday(recorded_at))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hosted_job_events_tenant_id
        ON hosted_job_events(tenant_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS dream_sweep_state (
        tenant_id TEXT NOT NULL,
        agent_id TEXT NOT NULL DEFAULT '',
        last_summarize_watermark INTEGER NOT NULL DEFAULT 0,
        last_dream_completed_at TEXT,
        PRIMARY KEY (tenant_id, agent_id),
        FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
    )
    """,
)

# The NULL/shared agent scope stored in `dream_sweep_state.agent_id` -- SQLite
# primary keys cannot contain NULL, so the shared scope uses this sentinel.
_SHARED_AGENT_SENTINEL = ""


@dataclass(frozen=True)
class ProvisionedApiKey:
    key_id: str
    raw_key: str


@dataclass(frozen=True)
class DreamSweepState:
    """Per-(tenant, agent) sweeper bookkeeping in the control database
    (ADR 0030): the highest transcript watermark whose summarize job has run
    to completion for the scope, and when the scope's last full dream chain
    finished running."""

    last_summarize_watermark: int = 0
    last_dream_completed_at: str | None = None


@dataclass(frozen=True)
class HostedProjectRecord:
    project_id: str
    tenant_id: str
    name: str
    environment: str
    created_at: str


@dataclass(frozen=True)
class HostedApiKeyRecord:
    key_id: str
    tenant_id: str
    project_id: str
    name: str
    capability: str
    agent_scope: str
    prefix: str
    last4: str
    display: str
    created_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None
    created_via: str = "console"


@dataclass(frozen=True)
class HostedSetupTokenRecord:
    token_id: str
    tenant_id: str
    project_id: str
    agent_scope: str
    session_id: str
    created_at: str
    expires_at: str
    consumed_at: str | None = None
    consumed_key_id: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True)
class ProvisionedSetupToken:
    token_id: str
    raw_token: str


@dataclass(frozen=True)
class SetupTokenExchange:
    provisioned: ProvisionedApiKey
    key_record: HostedApiKeyRecord
    project_id: str
    session_id: str
    agent_scope: str


@dataclass(frozen=True)
class _HostedSetupToken:
    token_id: str
    token_hash: str
    tenant_id: str
    project_id: str
    agent_scope: str
    session_id: str
    created_at: str
    expires_at: str
    consumed_at: str | None = None
    consumed_key_id: str | None = None
    revoked_at: str | None = None
    revoked_by: str | None = None


@dataclass(frozen=True)
class _HostedApiKey:
    key_id: str
    key_hash: str
    tenant_id: str
    principal_id: str
    capabilities: frozenset[MemoryCapability]
    project_ids: frozenset[str]
    agent_ids: frozenset[str | None]
    created_at: str | None = None
    revoked_at: str | None = None
    revoked_by: str | None = None
    active: bool = True
    last_used_at: str | None = None


@dataclass(frozen=True)
class ReplacementTarget:
    """A validated `activate_replacement_database` replacement, normalized to
    either a local filesystem path or a Turso/libSQL DSN.

    `connect_target` is what gets passed to `connect()` to read the
    replacement's `canonical_migration_imports` metadata and run
    `init_schema` -- a `Path` for the local kind, the DSN string for the
    Turso kind. `repoint_value` is what gets written into the catalog on a
    successful repoint -- the filename for `db_filename` (local) or the DSN
    itself for `customer_target` (Turso). Construct via `_validate_local` or
    `_validate_dsn`, never directly: those two are where the local-path
    (under-root/customer-file) and DSN (well-formed, distinct from the
    current target) checks branch and live.
    """

    kind: str  # "local" | "dsn"
    connect_target: Path | str
    repoint_value: str

    @staticmethod
    def _validate_local(root_path: Path, replacement_db_path: str | Path) -> "ReplacementTarget":
        candidate = Path(replacement_db_path)
        if not candidate.is_absolute():
            candidate = root_path / candidate
        root = root_path.resolve()
        replacement = candidate.resolve()
        try:
            relative = replacement.relative_to(root)
        except ValueError as exc:
            raise ValueError("replacement database must be under hosted root.") from exc
        if len(relative.parts) != 1 or relative.name == "control-plane.db":
            raise ValueError("replacement database must be a customer database file.")
        if not replacement.is_file():
            raise FileNotFoundError(f"Replacement database does not exist: {replacement}")
        return ReplacementTarget(
            kind="local",
            connect_target=replacement,
            repoint_value=relative.name,
        )

    @staticmethod
    def _validate_dsn(current_customer_target: str | None, dsn: str) -> "ReplacementTarget":
        if not _is_libsql_target(dsn) or not urlsplit(dsn).netloc:
            raise ValueError("replacement database must be a well-formed libSQL DSN.")
        if current_customer_target is not None and dsn == current_customer_target:
            raise ValueError(
                "replacement database must differ from the tenant's current customer target."
            )
        return ReplacementTarget(kind="dsn", connect_target=dsn, repoint_value=dsn)

    @staticmethod
    def from_replacement(
        root_path: Path,
        current_customer_target: str | None,
        replacement: str | Path,
    ) -> "ReplacementTarget":
        """Branch on the replacement's kind: a `str` containing a `://`
        authority separator is an attempted DSN, validated as Turso (well-formed
        libSQL scheme + non-empty host, distinct from the current customer
        target -- the filesystem/under-root/`os` checks do NOT apply). Anything
        else (a `Path`, or a `str` filesystem path with no `://`) validates as a
        local under-root customer db file."""
        if isinstance(replacement, str) and "://" in replacement:
            return ReplacementTarget._validate_dsn(current_customer_target, replacement)
        return ReplacementTarget._validate_local(root_path, replacement)


def _insert_audit_event(conn: sqlite3.Connection, event: HostedAuditEvent) -> None:
    """Append one row to the shared control-plane ``hosted_audit_events`` ledger.

    Used by both the tenant catalog and the API-key store, which write to the
    same ``control-plane.db``. Callers own the surrounding transaction/commit.
    """
    conn.execute(
        """
        INSERT INTO hosted_audit_events (
            operation, tenant_id, principal_id, status, recorded_at,
            error_type, project_id, key_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.operation,
            event.tenant_id,
            event.principal_id,
            event.status,
            event.recorded_at,
            event.error_type,
            event.project_id,
            event.key_id,
        ),
    )


class HostedTenantCatalog:
    def __init__(
        self,
        root_path: str | Path,
        *,
        control_target: StorageTarget | None = None,
        customer_target_factory: Callable[[str], str] | None = None,
    ) -> None:
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)
        self._customer_target_factory = customer_target_factory
        self._control_target: str | Path | StorageTarget = (
            control_target
            if control_target is not None
            else self.root_path / "control-plane.db"
        )
        self._init_control_plane_schema()

    def provision_tenant(
        self,
        tenant_id: str,
        *,
        project_ids: set[str] | frozenset[str] = frozenset(),
        customer_target: str | None = None,
    ) -> HostedTenant:
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be blank.")
        project_ids = frozenset(project_ids)
        for project_id in project_ids:
            if not project_id.strip():
                raise ValueError("project_id must not be blank.")
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT db_filename, active, customer_target
                FROM tenants
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
            if row is None:
                db_filename = self._allocate_db_filename(conn)
                target = self._new_customer_target(tenant_id, customer_target)
                conn.execute(
                    """
                    INSERT INTO tenants (tenant_id, db_filename, active, customer_target)
                    VALUES (?, ?, 0, ?)
                    """,
                    (tenant_id, db_filename, target),
                )
                conn.commit()
                needs_customer_init = True
            else:
                db_filename = row[0]
                needs_customer_init = not bool(row[1])
                if customer_target is None and row[2]:
                    target = None  # keep the existing customer target
                else:
                    target = self._new_customer_target(tenant_id, customer_target)
            if needs_customer_init:
                tenant_db_path = self.root_path / db_filename
                LocalMemoryService(db_path=str(tenant_db_path), tenant_id=tenant_id).init_schema()
            conn.execute(
                """
                UPDATE tenants
                SET active = 1, retired_at = NULL, retired_by = NULL
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            )
            if target is not None:
                conn.execute(
                    """
                    UPDATE tenants
                    SET customer_target = ?
                    WHERE tenant_id = ?
                    """,
                    (target, tenant_id),
                )
            self._insert_projects(conn, tenant_id, project_ids)
            conn.commit()
            return self._tenant_from_filename(conn, tenant_id, db_filename)

    def provision_customer_account(self, clerk_org_id: str) -> str:
        if not clerk_org_id.strip():
            raise ValueError("clerk_org_id must not be blank.")
        with closing(self._connect_control()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT tenant_id
                FROM customer_account_mappings
                WHERE clerk_org_id = ?
                """,
                (clerk_org_id,),
            ).fetchone()
            if row is None:
                tenant_id = self._allocate_tenant_id(conn)
                db_filename = self._allocate_db_filename(conn)
                customer_target = self._new_customer_target(tenant_id, None)
                conn.execute(
                    """
                    INSERT INTO tenants (tenant_id, db_filename, active, customer_target)
                    VALUES (?, ?, 0, ?)
                    """,
                    (tenant_id, db_filename, customer_target),
                )
                conn.execute(
                    """
                    INSERT INTO customer_account_mappings (clerk_org_id, tenant_id)
                    VALUES (?, ?)
                    """,
                    (clerk_org_id, tenant_id),
                )
            else:
                tenant_id = str(row[0])
            conn.commit()
        self.provision_tenant(tenant_id)
        return tenant_id

    def provision_missing_customer_targets(self) -> list[HostedTenant]:
        if self._customer_target_factory is None:
            return []
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                """
                SELECT tenant_id
                FROM tenants
                WHERE active = 1 AND customer_target IS NULL
                ORDER BY tenant_id
                """
            ).fetchall()
        provisioned: list[HostedTenant] = []
        for (tenant_id,) in rows:
            provisioned.append(self.provision_tenant(str(tenant_id)))
        return provisioned

    def _new_customer_target(
        self,
        tenant_id: str,
        customer_target: str | None,
    ) -> str | None:
        if customer_target is not None or self._customer_target_factory is None:
            return customer_target
        return self._customer_target_factory(tenant_id)

    def resolve_customer_tenant(self, clerk_org_id: str) -> str | None:
        if not clerk_org_id.strip():
            raise ValueError("clerk_org_id must not be blank.")
        # Join on `active = 1` (matching `get_tenant`) so a mapping left behind
        # by an interrupted `provision_customer_account` -- committed before
        # `provision_tenant` finished customer-db init -- does not resolve on
        # the read path.
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT m.tenant_id
                FROM customer_account_mappings AS m
                JOIN tenants AS t
                    ON t.tenant_id = m.tenant_id AND t.active = 1
                WHERE m.clerk_org_id = ?
                """,
                (clerk_org_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def provision_project(self, tenant_id: str, project_id: str) -> HostedTenant:
        if not project_id.strip():
            raise ValueError("project_id must not be blank.")
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT db_filename
                FROM tenants
                WHERE tenant_id = ? AND active = 1
                """,
                (tenant_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted tenant.")
            self._insert_projects(conn, tenant_id, {project_id})
            conn.commit()
            return self._tenant_from_filename(conn, tenant_id, row[0])

    def create_control_project(
        self,
        tenant_id: str,
        *,
        name: str,
        environment: str = "production",
    ) -> HostedProjectRecord:
        if not name.strip():
            raise ValueError("name must not be blank.")
        environment = environment.strip() or "production"
        created_at = _now()
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM tenants
                WHERE tenant_id = ? AND active = 1
                """,
                (tenant_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted tenant.")
            project_id = self._allocate_project_id(conn)
            conn.execute(
                """
                INSERT INTO hosted_projects (
                    project_id, tenant_id, name, environment, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, tenant_id, name.strip(), environment, created_at),
            )
            self._insert_projects(conn, tenant_id, {project_id})
            conn.commit()
        return HostedProjectRecord(
            project_id=project_id,
            tenant_id=tenant_id,
            name=name.strip(),
            environment=environment,
            created_at=created_at,
        )

    def list_control_projects(self, tenant_id: str) -> list[HostedProjectRecord]:
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                """
                SELECT project_id, tenant_id, name, environment, created_at
                FROM hosted_projects
                WHERE tenant_id = ? AND retired_at IS NULL
                ORDER BY created_at, project_id
                """,
                (tenant_id,),
            ).fetchall()
        return [
            HostedProjectRecord(
                project_id=row[0],
                tenant_id=row[1],
                name=row[2],
                environment=row[3],
                created_at=row[4],
            )
            for row in rows
        ]

    def get_control_project(self, tenant_id: str, project_id: str) -> HostedProjectRecord:
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT project_id, tenant_id, name, environment, created_at
                FROM hosted_projects
                WHERE tenant_id = ? AND project_id = ? AND retired_at IS NULL
                """,
                (tenant_id, project_id),
            ).fetchone()
        if row is None:
            raise PermissionError("Unknown hosted project.")
        return HostedProjectRecord(
            project_id=row[0],
            tenant_id=row[1],
            name=row[2],
            environment=row[3],
            created_at=row[4],
        )

    def retire_tenant(self, tenant_id: str, *, retired_by: str | None = None) -> None:
        """Soft-delete a tenant in place (ADR 0028).

        Non-destructive: marks the tenant inactive (so the existing
        ``active = 1`` gates exclude it) and stamps ``retired_at``/``retired_by``
        while the row and its customer-DB pointer survive. ``provision_tenant``
        reactivates and clears the retirement stamp.
        """
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                "SELECT retired_at FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted tenant.")
            if row[0] is not None:
                raise PermissionError("Hosted tenant already retired.")
            conn.execute(
                """
                UPDATE tenants
                SET active = 0, retired_at = ?, retired_by = ?
                WHERE tenant_id = ?
                """,
                (_now(), retired_by, tenant_id),
            )
            # Audit in the same transaction as the retirement so the destructive
            # state and its audit row commit or roll back together.
            _insert_audit_event(
                conn,
                HostedAuditEvent(
                    operation="retire_tenant",
                    tenant_id=tenant_id,
                    principal_id=retired_by,
                    status="ok",
                    recorded_at=_now(),
                ),
            )
            conn.commit()

    def retire_control_project(
        self,
        tenant_id: str,
        project_id: str,
        *,
        retired_by: str | None = None,
    ) -> None:
        """Soft-delete a control-plane project in place (ADR 0028).

        Non-destructive: stamps ``retired_at``/``retired_by`` so the project
        drops out of active listings while the row survives for recovery/audit.
        """
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT retired_at
                FROM hosted_projects
                WHERE tenant_id = ? AND project_id = ?
                """,
                (tenant_id, project_id),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted project.")
            if row[0] is not None:
                raise PermissionError("Hosted project already retired.")
            conn.execute(
                """
                UPDATE hosted_projects
                SET retired_at = ?, retired_by = ?
                WHERE tenant_id = ? AND project_id = ?
                """,
                (_now(), retired_by, tenant_id, project_id),
            )
            # Audit in the same transaction as the retirement (commit/rollback
            # together) so a retired row never lacks its audit record.
            _insert_audit_event(
                conn,
                HostedAuditEvent(
                    operation="retire_project",
                    tenant_id=tenant_id,
                    principal_id=retired_by,
                    status="ok",
                    recorded_at=_now(),
                    project_id=project_id,
                ),
            )
            conn.commit()

    def upsert_control_project(
        self,
        tenant_id: str,
        project_id: str,
        *,
        name: str,
        environment: str = "production",
    ) -> HostedProjectRecord:
        if not project_id.strip():
            raise ValueError("project_id must not be blank.")
        if not name.strip():
            raise ValueError("name must not be blank.")
        environment = environment.strip() or "production"
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT tenant_id, created_at
                FROM hosted_projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if row is None:
                created_at = _now()
                conn.execute(
                    """
                    INSERT INTO hosted_projects (
                        project_id, tenant_id, name, environment, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, tenant_id, name.strip(), environment, created_at),
                )
            else:
                if str(row[0]) != tenant_id:
                    raise PermissionError("Unknown hosted project.")
                created_at = str(row[1])
                conn.execute(
                    """
                    UPDATE hosted_projects
                    SET name = ?, environment = ?
                    WHERE tenant_id = ? AND project_id = ?
                    """,
                    (name.strip(), environment, tenant_id, project_id),
                )
            self._insert_projects(conn, tenant_id, {project_id})
            conn.commit()
        return HostedProjectRecord(
            project_id=project_id,
            tenant_id=tenant_id,
            name=name.strip(),
            environment=environment,
            created_at=created_at,
        )

    def activate_replacement_database(
        self,
        tenant_id: str,
        replacement_db_path: str | Path,
    ) -> HostedTenant:
        """Repoint `tenant_id` at a verified replacement database.

        `replacement_db_path` is EITHER a local filesystem path (must resolve
        under `root_path` and be a customer db file -- the original,
        pre-Task-12 behavior) OR a Turso/libSQL DSN `str` (validated as a
        well-formed DSN, distinct from the tenant's current
        `customer_target`; the filesystem/`os` checks do not apply --
        `ReplacementTarget.from_replacement` branches on which kind this is).

        Both kinds share the same post-validation flow: read
        `canonical_migration_imports` from the replacement, verify the
        imported tenant/project match the catalog tenant, run `init_schema`
        on the replacement, then repoint the catalog (local sets
        `db_filename`, Turso sets `customer_target`) and bump `generation` so
        a request-scoped service holding the pre-repoint handle stops being
        able to write through it.
        """
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be blank.")
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT db_filename, customer_target
                FROM tenants
                WHERE tenant_id = ? AND active = 1
                """,
                (tenant_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted tenant.")
            current_customer_target = row[1]
            target = ReplacementTarget.from_replacement(
                self.root_path, current_customer_target, replacement_db_path
            )
            project_rows = conn.execute(
                """
                SELECT project_id
                FROM tenant_projects
                WHERE tenant_id = ?
                ORDER BY project_id
                """,
                (tenant_id,),
            ).fetchall()
            project_ids = frozenset(str(project_row[0]) for project_row in project_rows)
            migration_scope = self._replacement_migration_scope(target.connect_target)
            if migration_scope is None:
                raise PermissionError("Replacement database has no migration metadata.")
            imported_tenant_id, imported_project_id = migration_scope
            if imported_tenant_id != tenant_id:
                raise PermissionError("Replacement database tenant does not match catalog tenant.")
            if imported_project_id not in project_ids:
                raise PermissionError("Replacement database project is outside catalog tenant projects.")
            init_schema_target = (
                str(target.connect_target) if target.kind == "local" else target.connect_target
            )
            LocalMemoryService(db_path=init_schema_target, tenant_id=tenant_id).init_schema()
            if target.kind == "local":
                conn.execute(
                    """
                    UPDATE tenants
                    SET db_filename = ?, active = 1, generation = generation + 1
                    WHERE tenant_id = ?
                    """,
                    (target.repoint_value, tenant_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE tenants
                    SET customer_target = ?, active = 1, generation = generation + 1
                    WHERE tenant_id = ?
                    """,
                    (target.repoint_value, tenant_id),
                )
            conn.commit()
            db_filename = target.repoint_value if target.kind == "local" else row[0]
            return self._tenant_from_filename(conn, tenant_id, db_filename)

    def _replacement_migration_scope(
        self, db_path: Path | str
    ) -> tuple[str, str | None] | None:
        try:
            with closing(connect(db_path)) as conn:
                row = conn.execute(
                    """
                    SELECT tenant_id, project_id
                    FROM canonical_migration_imports
                    WHERE id = 1
                    """
                ).fetchone()
        except (sqlite3.DatabaseError, ValueError) as exc:
            # A missing `canonical_migration_imports` table (or otherwise
            # unreadable replacement db) is a "no migration metadata" signal:
            # local sqlite raises a typed `sqlite3.DatabaseError` (preserved
            # here at its original blanket reach); the hosted libSQL backend
            # raises a bare `ValueError` ("no such table") (ADR 0019), gated
            # through `is_operational_error` so an unrelated `ValueError`
            # re-raises rather than being silently swallowed. Both signals ->
            # return None (yields a clean PermissionError upstream).
            if isinstance(exc, sqlite3.DatabaseError) or is_operational_error(exc):
                return None
            raise
        if row is None:
            return None
        return str(row[0]), None if row[1] is None else str(row[1])

    def get_tenant(self, tenant_id: str) -> HostedTenant:
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT db_filename
                FROM tenants
                WHERE tenant_id = ? AND active = 1
                """,
                (tenant_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted tenant.")
            return self._tenant_from_filename(conn, tenant_id, row[0])

    def list_active_tenant_ids(self) -> list[str]:
        """All non-retired tenant ids, ordered for deterministic sweep order."""
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                """
                SELECT tenant_id
                FROM tenants
                WHERE active = 1
                ORDER BY tenant_id
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def dream_scheduling_enabled(self, tenant_id: str) -> bool:
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                "SELECT dream_scheduling FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if row is None:
            raise PermissionError("Unknown hosted tenant.")
        return bool(row[0])

    def set_dream_scheduling(self, tenant_id: str, *, enabled: bool) -> None:
        with closing(self._connect_control()) as conn:
            cursor = conn.execute(
                "UPDATE tenants SET dream_scheduling = ? WHERE tenant_id = ?",
                (1 if enabled else 0, tenant_id),
            )
            if cursor.rowcount == 0:
                raise PermissionError("Unknown hosted tenant.")
            conn.commit()

    def dream_sweep_state(
        self,
        tenant_id: str,
        agent_id: str | None,
    ) -> "DreamSweepState":
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT last_summarize_watermark, last_dream_completed_at
                FROM dream_sweep_state
                WHERE tenant_id = ? AND agent_id = ?
                """,
                (tenant_id, agent_id or _SHARED_AGENT_SENTINEL),
            ).fetchone()
        if row is None:
            return DreamSweepState()
        return DreamSweepState(
            last_summarize_watermark=int(row[0]),
            last_dream_completed_at=row[1],
        )

    def record_summarize_watermark(
        self,
        tenant_id: str,
        agent_id: str | None,
        watermark: int,
    ) -> None:
        # Monotonic: a stale recorder (older tick finishing late) must never
        # rewind a newer watermark.
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                INSERT INTO dream_sweep_state
                    (tenant_id, agent_id, last_summarize_watermark)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id, agent_id) DO UPDATE
                    SET last_summarize_watermark = MAX(
                        dream_sweep_state.last_summarize_watermark,
                        excluded.last_summarize_watermark
                    )
                """,
                (tenant_id, agent_id or _SHARED_AGENT_SENTINEL, watermark),
            )
            conn.commit()

    def record_dream_completed(
        self,
        tenant_id: str,
        agent_id: str | None,
        completed_at: str,
    ) -> None:
        # Monotonic on the ISO-8601 UTC timestamp (lexicographic order matches
        # chronological order for the uniform format this codebase writes).
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                INSERT INTO dream_sweep_state
                    (tenant_id, agent_id, last_dream_completed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id, agent_id) DO UPDATE
                    SET last_dream_completed_at = CASE
                        WHEN dream_sweep_state.last_dream_completed_at IS NULL
                            OR excluded.last_dream_completed_at
                                > dream_sweep_state.last_dream_completed_at
                        THEN excluded.last_dream_completed_at
                        ELSE dream_sweep_state.last_dream_completed_at
                    END
                """,
                (tenant_id, agent_id or _SHARED_AGENT_SENTINEL, completed_at),
            )
            conn.commit()

    def record_audit_event(self, event: HostedAuditEvent) -> None:
        with closing(self._connect_control()) as conn:
            _insert_audit_event(conn, event)
            conn.commit()

    def record_usage_event(self, event: HostedUsageEvent) -> None:
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                INSERT INTO hosted_usage_events (
                    kind, operation, tenant_id, principal_id, status, recorded_at,
                    model_requests, input_tokens, output_tokens, total_tokens,
                    estimated_cost_micros, error_type, project_id, key_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.kind,
                    event.operation,
                    event.tenant_id,
                    event.principal_id,
                    event.status,
                    event.recorded_at,
                    event.model_requests,
                    event.input_tokens,
                    event.output_tokens,
                    event.total_tokens,
                    event.estimated_cost_micros,
                    event.error_type,
                    event.project_id,
                    event.key_id,
                ),
            )
            conn.commit()

    def record_job_event(self, event: HostedJobEvent) -> None:
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                INSERT INTO hosted_job_events (
                    job_id, operation, tenant_id, principal_id, status, recorded_at,
                    phase, error_type, project_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.job_id,
                    event.operation,
                    event.tenant_id,
                    event.principal_id,
                    event.status,
                    event.recorded_at,
                    event.phase,
                    event.error_type,
                    event.project_id,
                ),
            )
            conn.commit()

    def audit_events(self, tenant_id: str | None) -> list[HostedAuditEvent]:
        with closing(self._connect_control()) as conn:
            if tenant_id is None:
                rows = conn.execute(
                    """
                    SELECT operation, tenant_id, principal_id, status, recorded_at,
                           error_type, project_id, key_id
                    FROM hosted_audit_events
                    WHERE tenant_id IS NULL
                    ORDER BY id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT operation, tenant_id, principal_id, status, recorded_at,
                           error_type, project_id, key_id
                    FROM hosted_audit_events
                    WHERE tenant_id = ?
                    ORDER BY id
                    """,
                    (tenant_id,),
                ).fetchall()
        return [HostedAuditEvent(*row) for row in rows]

    def usage_events(
        self,
        tenant_id: str | None,
        *,
        project_id: str | None = None,
        recorded_at_gte: str | None = None,
        recorded_at_lt: str | None = None,
    ) -> list[HostedUsageEvent]:
        conditions: list[str]
        params: list[object] = []
        if tenant_id is None:
            conditions = ["tenant_id IS NULL"]
        else:
            conditions = ["tenant_id = ?"]
            params.append(tenant_id)
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if recorded_at_gte is not None:
            conditions.append("julianday(recorded_at) >= julianday(?)")
            params.append(recorded_at_gte)
        if recorded_at_lt is not None:
            conditions.append("julianday(recorded_at) < julianday(?)")
            params.append(recorded_at_lt)
        where_clause = " AND ".join(conditions)
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                f"""
                SELECT kind, operation, tenant_id, principal_id, status, recorded_at,
                       model_requests, input_tokens, output_tokens, total_tokens,
                       estimated_cost_micros, error_type, project_id, key_id
                FROM hosted_usage_events
                WHERE {where_clause}
                ORDER BY id
                """,
                tuple(params),
            ).fetchall()
        return [HostedUsageEvent(*row) for row in rows]

    _WRITE_OPERATIONS = ("append_transcript",)
    _RETRIEVAL_OPERATIONS = ("search_transcript", "search_long_term")

    def usage_daily(
        self,
        tenant_id: str,
        *,
        project_id: str,
        recorded_at_gte: str,
        recorded_at_lt: str,
    ) -> list[dict[str, object]]:
        events = self.usage_events(
            tenant_id,
            project_id=project_id,
            recorded_at_gte=recorded_at_gte,
            recorded_at_lt=recorded_at_lt,
        )
        buckets: dict[str, dict[str, object]] = {}
        for event in events:
            date = event.recorded_at[:10]
            bucket = buckets.setdefault(
                date, {"date": date, "writes": 0, "retrievals": 0, "other": 0}
            )
            if event.operation in self._WRITE_OPERATIONS:
                bucket["writes"] += 1
            elif event.operation in self._RETRIEVAL_OPERATIONS:
                bucket["retrievals"] += 1
            else:
                bucket["other"] += 1
        return [buckets[date] for date in sorted(buckets)]

    def usage_by_key(
        self,
        tenant_id: str,
        *,
        project_id: str,
        recorded_at_gte: str,
        recorded_at_lt: str,
    ) -> list[dict[str, object]]:
        events = self.usage_events(
            tenant_id,
            project_id=project_id,
            recorded_at_gte=recorded_at_gte,
            recorded_at_lt=recorded_at_lt,
        )
        counts: dict[str | None, int] = {}
        for event in events:
            counts[event.key_id] = counts.get(event.key_id, 0) + 1
        return [
            {"keyId": key_id, "requests": count}
            for key_id, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0] is None, item[0] or "")
            )
        ]

    def job_events(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[HostedJobEvent]:
        conditions = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        order = "ORDER BY id"
        if limit is not None:
            order = "ORDER BY id DESC LIMIT ?"
            params.append(limit)
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    job_id, operation, tenant_id, principal_id, status,
                    recorded_at, phase, error_type, project_id
                FROM hosted_job_events
                WHERE {" AND ".join(conditions)}
                {order}
                """,
                tuple(params),
            ).fetchall()
        return [
            HostedJobEvent(
                job_id=row[0],
                operation=row[1],
                tenant_id=row[2],
                principal_id=row[3],
                status=row[4],
                recorded_at=row[5],
                phase=row[6],
                error_type=row[7],
                project_id=row[8],
            )
            for row in rows
        ]

    def _connect_control(self) -> sqlite3.Connection:
        return _connect_control_db(self._control_target)

    def _init_control_plane_schema(self) -> None:
        # Individual `execute()` calls, not `executescript()`: the latter is a
        # `sqlite3`-only API not part of the libSQL-compatible
        # `StorageConnection` protocol (see `vexic.storage.schema.init_db` for
        # the same pattern on the customer-memory schema).
        with closing(self._connect_control()) as conn:
            for statement in _CONTROL_PLANE_SCHEMA_STATEMENTS:
                conn.execute(statement)
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_usage_events)").fetchall()
            }
            if "project_id" not in columns:
                conn.execute("ALTER TABLE hosted_usage_events ADD COLUMN project_id TEXT")
            if "key_id" not in columns:
                conn.execute("ALTER TABLE hosted_usage_events ADD COLUMN key_id TEXT")
            audit_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_audit_events)").fetchall()
            }
            if "project_id" not in audit_columns:
                conn.execute("ALTER TABLE hosted_audit_events ADD COLUMN project_id TEXT")
            if "key_id" not in audit_columns:
                conn.execute("ALTER TABLE hosted_audit_events ADD COLUMN key_id TEXT")
            job_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_job_events)").fetchall()
            }
            if "project_id" not in job_columns:
                conn.execute("ALTER TABLE hosted_job_events ADD COLUMN project_id TEXT")
            tenant_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(tenants)").fetchall()
            }
            if "customer_target" not in tenant_columns:
                conn.execute("ALTER TABLE tenants ADD COLUMN customer_target TEXT")
            if "generation" not in tenant_columns:
                conn.execute(
                    "ALTER TABLE tenants ADD COLUMN generation INTEGER NOT NULL DEFAULT 1"
                )
            if "retired_at" not in tenant_columns:
                conn.execute("ALTER TABLE tenants ADD COLUMN retired_at TEXT")
            if "retired_by" not in tenant_columns:
                conn.execute("ALTER TABLE tenants ADD COLUMN retired_by TEXT")
            if "dream_scheduling" not in tenant_columns:
                conn.execute(
                    "ALTER TABLE tenants ADD COLUMN dream_scheduling INTEGER NOT NULL DEFAULT 1"
                )
            # Sweep state moved from tenant-keyed to (tenant, agent)-scoped
            # before any release shipped the table. The state is disposable
            # bookkeeping (worst case: one redundant sweep), so an old-shape
            # table is dropped and recreated rather than migrated.
            sweep_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(dream_sweep_state)").fetchall()
            }
            if sweep_columns and "agent_id" not in sweep_columns:
                conn.execute("DROP TABLE dream_sweep_state")
                conn.execute(_CONTROL_PLANE_SCHEMA_STATEMENTS[-1])
            project_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_projects)").fetchall()
            }
            if "retired_at" not in project_columns:
                conn.execute("ALTER TABLE hosted_projects ADD COLUMN retired_at TEXT")
            if "retired_by" not in project_columns:
                conn.execute("ALTER TABLE hosted_projects ADD COLUMN retired_by TEXT")
            conn.commit()

    def _allocate_db_filename(self, conn: sqlite3.Connection) -> str:
        # NOTE(alpha): local staging assumes serialized provisioning; retry INSERT on
        # IntegrityError if concurrent tenant creation becomes a real workload.
        for _ in range(100):
            db_filename = f"customer-{secrets.token_hex(16)}.db"
            exists = conn.execute(
                """
                SELECT 1
                FROM tenants
                WHERE db_filename = ?
                """,
                (db_filename,),
            ).fetchone()
            if exists is None:
                return db_filename
        raise RuntimeError("Unable to allocate hosted tenant database path.")

    def _allocate_tenant_id(self, conn: sqlite3.Connection) -> str:
        for _ in range(100):
            tenant_id = f"tenant_{secrets.token_hex(8)}"
            exists = conn.execute(
                """
                SELECT 1
                FROM tenants
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
            if exists is None:
                return tenant_id
        raise RuntimeError("Unable to allocate hosted tenant id.")

    def _allocate_project_id(self, conn: sqlite3.Connection) -> str:
        for _ in range(100):
            project_id = f"proj_{secrets.token_hex(8)}"
            exists = conn.execute(
                """
                SELECT 1
                FROM hosted_projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if exists is None:
                return project_id
        raise RuntimeError("Unable to allocate hosted project id.")

    def _insert_projects(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        project_ids: set[str] | frozenset[str],
    ) -> None:
        for project_id in project_ids:
            if not project_id.strip():
                raise ValueError("project_id must not be blank.")
            conn.execute(
                """
                INSERT OR IGNORE INTO tenant_projects (tenant_id, project_id)
                VALUES (?, ?)
                """,
                (tenant_id, project_id),
            )

    def _tenant_from_filename(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        db_filename: str,
    ) -> HostedTenant:
        project_rows = conn.execute(
            """
            SELECT project_id
            FROM tenant_projects
            WHERE tenant_id = ?
            ORDER BY project_id
            """,
            (tenant_id,),
        ).fetchall()
        catalog_row = conn.execute(
            """
            SELECT customer_target, generation
            FROM tenants
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchone()
        customer_target = None if catalog_row is None else catalog_row[0]
        generation = 1 if catalog_row is None else int(catalog_row[1])
        return HostedTenant(
            tenant_id=tenant_id,
            db_path=self.root_path / db_filename,
            project_ids=frozenset(row[0] for row in project_rows),
            customer_target=customer_target,
            generation=generation,
        )

class HostedApiKeyStore:
    def __init__(
        self,
        root_path: str | Path | None = None,
        *,
        control_target: StorageTarget | None = None,
    ) -> None:
        self._keys: dict[str, _HostedApiKey] = {}
        self._control_metadata: dict[str, HostedApiKeyRecord] = {}
        self._setup_tokens: dict[str, _HostedSetupToken] = {}
        self.root_path = Path(root_path) if root_path is not None else None
        self._control_target: str | Path | StorageTarget | None = None
        if control_target is not None:
            self._control_target = control_target
            self._init_control_plane_schema()
        elif self.root_path is not None:
            self.root_path.mkdir(parents=True, exist_ok=True)
            self._control_target = self.root_path / "control-plane.db"
            self._init_control_plane_schema()

    def create_key(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        capabilities: set[MemoryCapability] | frozenset[MemoryCapability],
        project_ids: set[str] | frozenset[str] = frozenset(),
        agent_ids: set[str | None] | frozenset[str | None] = frozenset(),
    ) -> ProvisionedApiKey:
        key_id = secrets.token_hex(8)
        raw_key = f"vx_{key_id}_{secrets.token_urlsafe(32)}"
        stored = _HostedApiKey(
            key_id=key_id,
            key_hash=self._hash(raw_key),
            tenant_id=tenant_id,
            principal_id=principal_id,
            capabilities=frozenset(capabilities),
            project_ids=frozenset(project_ids),
            agent_ids=frozenset(agent_ids),
            created_at=_now(),
        )
        if self._control_target is None:
            self._keys[key_id] = stored
        else:
            with closing(self._connect_control()) as conn:
                conn.execute(
                    """
                    INSERT INTO hosted_api_keys (
                        key_id, key_hash, tenant_id, principal_id, capabilities,
                        project_ids, agent_ids, created_at, revoked_at, revoked_by
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        stored.key_id,
                        stored.key_hash,
                        stored.tenant_id,
                        stored.principal_id,
                        _capabilities_json(stored.capabilities),
                        _strings_json(stored.project_ids),
                        _nullable_strings_json(stored.agent_ids),
                        stored.created_at,
                    ),
                )
                conn.commit()
        return ProvisionedApiKey(key_id=key_id, raw_key=raw_key)

    def authenticate(self, raw_key: str) -> HostedAuthContext:
        key_id = self._parse_key_id(raw_key)
        key_hash = self._hash(raw_key)
        stored = self._load_key(key_id)
        if hmac.compare_digest(stored.key_hash, key_hash) and stored.active:
            self._touch_last_used(stored)
            return HostedAuthContext(
                key_id=stored.key_id,
                tenant_id=stored.tenant_id,
                principal=Principal(
                    principal_id=stored.principal_id,
                    principal_type=PrincipalType.AGENT,
                ),
                capabilities=stored.capabilities,
                project_ids=stored.project_ids,
                agent_ids=stored.agent_ids,
            )
        raise PermissionError("Invalid hosted API key.")

    _LAST_USED_MIN_INTERVAL_DAYS = 60.0 / 86400.0  # one minute, in julianday units

    def _touch_last_used(self, stored: _HostedApiKey) -> None:
        now = _now()
        if self._control_target is None:
            if _last_used_is_fresh(stored.last_used_at, now):
                return
            self._keys[stored.key_id] = replace(stored, last_used_at=now)
            return
        try:
            with closing(self._connect_control()) as conn:
                conn.execute(
                    """
                    UPDATE hosted_api_keys
                    SET last_used_at = ?
                    WHERE key_id = ?
                      AND (
                        last_used_at IS NULL
                        OR julianday(?) - julianday(last_used_at) >= ?
                      )
                    """,
                    (now, stored.key_id, now, self._LAST_USED_MIN_INTERVAL_DAYS),
                )
                conn.commit()
        except Exception:
            # Last-used telemetry must never break the auth hot path.
            pass

    def revoke_key(self, key_id: str, *, revoked_by: str | None = None) -> None:
        if self._control_target is None:
            try:
                stored = self._keys[key_id]
            except KeyError as exc:
                raise PermissionError("Unknown hosted API key.") from exc
            newly_revoked = stored.revoked_at is None
            self._keys[key_id] = replace(
                stored,
                active=False,
                revoked_at=stored.revoked_at or _now(),
                revoked_by=stored.revoked_by or revoked_by,
            )
            if newly_revoked:
                self._record_audit_event(
                    HostedAuditEvent(
                        operation="revoke_key",
                        tenant_id=stored.tenant_id,
                        principal_id=stored.principal_id,
                        status="ok",
                        recorded_at=_now(),
                        key_id=key_id,
                    )
                )
            return
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT tenant_id, principal_id, revoked_at
                FROM hosted_api_keys
                WHERE key_id = ?
                """,
                (key_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted API key.")
            tenant_id, principal_id, already_revoked_at = row[0], row[1], row[2]
            conn.execute(
                """
                UPDATE hosted_api_keys
                SET
                    revoked_at = COALESCE(revoked_at, ?),
                    revoked_by = COALESCE(revoked_by, ?)
                WHERE key_id = ?
                """,
                (_now(), revoked_by, key_id),
            )
            # Audit only the NULL -> revoked transition, on the same transaction
            # as the revoke, so a real delete never lands without its audit row
            # and a repeated (no-op) revoke does not forge a second one.
            if already_revoked_at is None:
                _insert_audit_event(
                    conn,
                    HostedAuditEvent(
                        operation="revoke_key",
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        status="ok",
                        recorded_at=_now(),
                        key_id=key_id,
                    ),
                )
            conn.commit()

    def create_control_plane_key(
        self,
        *,
        tenant_id: str,
        project_id: str,
        name: str,
        agent_scope: str = "shared",
    ) -> tuple[ProvisionedApiKey, HostedApiKeyRecord]:
        if not name.strip():
            raise ValueError("name must not be blank.")
        return self._mint_control_plane_key(
            tenant_id=tenant_id,
            project_id=project_id,
            name=name.strip(),
            agent_scope=agent_scope,
            created_via="console",
        )

    def _mint_control_plane_key(
        self,
        *,
        tenant_id: str,
        project_id: str,
        name: str,
        agent_scope: str,
        created_via: str,
    ) -> tuple[ProvisionedApiKey, HostedApiKeyRecord]:
        agent_scope = agent_scope.strip() or "shared"
        provisioned = self.create_key(
            tenant_id=tenant_id,
            principal_id=agent_scope,
            capabilities=_CONTROL_PLANE_AGENT_CAPABILITIES,
            project_ids={project_id},
            # "shared" is the null-agent scope ONLY. Bind it to {None} so the
            # membership check in hosted.py rejects any explicit agent_id; an
            # empty set would read as "no agent restriction" (a wildcard).
            agent_ids=frozenset({None}) if agent_scope == "shared" else {agent_scope},
        )
        stored = self._load_key(provisioned.key_id)
        prefix = provisioned.raw_key[:16]
        last4 = provisioned.raw_key[-4:]
        record = HostedApiKeyRecord(
            key_id=provisioned.key_id,
            tenant_id=tenant_id,
            project_id=project_id,
            name=name,
            capability="v1-memory",
            agent_scope=agent_scope,
            prefix=prefix,
            last4=last4,
            display=f"{prefix}...{last4}",
            created_at=stored.created_at or _now(),
            created_via=created_via,
        )
        if self._control_target is None:
            self._control_metadata[record.key_id] = record
        else:
            try:
                with closing(self._connect_control()) as conn:
                    conn.execute(
                        """
                        INSERT INTO hosted_api_key_metadata (
                            key_id, tenant_id, project_id, name, capability, agent_scope,
                            key_prefix, last4, display, created_at, created_via
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.key_id,
                            record.tenant_id,
                            record.project_id,
                            record.name,
                            record.capability,
                            record.agent_scope,
                            record.prefix,
                            record.last4,
                            record.display,
                            record.created_at,
                            record.created_via,
                        ),
                    )
                    conn.commit()
            except Exception:
                # NOTE(alpha): compensate here instead of threading a shared transaction through create_key.
                with suppress(PermissionError):
                    self.revoke_key(
                        provisioned.key_id,
                        revoked_by="control-plane-metadata-failure",
                    )
                raise
        return provisioned, record

    def list_control_plane_keys(
        self,
        *,
        tenant_id: str,
        project_id: str,
        include_revoked: bool = False,
    ) -> list[HostedApiKeyRecord]:
        if self._control_target is None:
            records = []
            for record in self._control_metadata.values():
                if record.tenant_id != tenant_id or record.project_id != project_id:
                    continue
                stored = self._keys[record.key_id]
                if stored.revoked_at is not None and not include_revoked:
                    continue
                records.append(
                    replace(
                        record,
                        revoked_at=stored.revoked_at,
                        last_used_at=stored.last_used_at,
                    )
                )
            return records
        revoked_filter = "" if include_revoked else "AND keys.revoked_at IS NULL"
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    meta.key_id, meta.tenant_id, meta.project_id, meta.name,
                    meta.capability, meta.agent_scope, meta.key_prefix,
                    meta.last4, meta.display, meta.created_at, keys.revoked_at,
                    keys.last_used_at, meta.created_via
                FROM hosted_api_key_metadata AS meta
                JOIN hosted_api_keys AS keys ON keys.key_id = meta.key_id
                WHERE meta.tenant_id = ? AND meta.project_id = ? {revoked_filter}
                ORDER BY meta.created_at, meta.key_id
                """,
                (tenant_id, project_id),
            ).fetchall()
        return [
            HostedApiKeyRecord(
                key_id=row[0],
                tenant_id=row[1],
                project_id=row[2],
                name=row[3],
                capability=row[4],
                agent_scope=row[5],
                prefix=row[6],
                last4=row[7],
                display=row[8],
                created_at=row[9],
                revoked_at=row[10],
                last_used_at=row[11],
                created_via=row[12],
            )
            for row in rows
        ]

    def revoke_control_plane_key(
        self,
        *,
        tenant_id: str,
        project_id: str,
        key_id: str,
        revoked_by: str | None = None,
    ) -> None:
        if self._control_target is None:
            record = self._control_metadata.get(key_id)
            if record is None or record.tenant_id != tenant_id or record.project_id != project_id:
                raise PermissionError("Unknown hosted API key.")
            self.revoke_key(key_id, revoked_by=revoked_by)
            return
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM hosted_api_key_metadata
                WHERE key_id = ? AND tenant_id = ? AND project_id = ?
                """,
                (key_id, tenant_id, project_id),
            ).fetchone()
        if row is None:
            raise PermissionError("Unknown hosted API key.")
        self.revoke_key(key_id, revoked_by=revoked_by)

    def create_setup_token(
        self,
        *,
        tenant_id: str,
        project_id: str,
        agent_scope: str = "shared",
        session_id: str = "",
        ttl_seconds: int = 600,
    ) -> tuple[ProvisionedSetupToken, HostedSetupTokenRecord]:
        agent_scope = agent_scope.strip() or "shared"
        session_id = session_id.strip() or f"setup-{secrets.token_hex(4)}"
        token_id = secrets.token_hex(8)
        raw_token = f"vxsetup_{token_id}_{secrets.token_urlsafe(32)}"
        now = datetime.now(UTC)
        created_at = _to_z(now)
        expires_at = _to_z(now + timedelta(seconds=ttl_seconds))
        stored = _HostedSetupToken(
            token_id=token_id,
            token_hash=self._hash(raw_token),
            tenant_id=tenant_id,
            project_id=project_id,
            agent_scope=agent_scope,
            session_id=session_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        if self._control_target is None:
            self._setup_tokens[token_id] = stored
        else:
            with closing(self._connect_control()) as conn:
                conn.execute(
                    """
                    INSERT INTO hosted_setup_tokens (
                        token_id, token_hash, tenant_id, project_id, agent_scope,
                        session_id, created_at, expires_at, consumed_at,
                        consumed_key_id, revoked_at, revoked_by
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                    """,
                    (
                        stored.token_id,
                        stored.token_hash,
                        stored.tenant_id,
                        stored.project_id,
                        stored.agent_scope,
                        stored.session_id,
                        stored.created_at,
                        stored.expires_at,
                    ),
                )
                conn.commit()
        record = HostedSetupTokenRecord(
            token_id=token_id,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_scope=agent_scope,
            session_id=session_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        return ProvisionedSetupToken(token_id=token_id, raw_token=raw_token), record

    def exchange_setup_token(self, raw_token: str) -> SetupTokenExchange:
        parts = raw_token.split("_", 2)
        if len(parts) != 3 or parts[0] != "vxsetup" or not parts[1] or not parts[2]:
            raise PermissionError("Invalid setup token.")
        token_id = parts[1]
        token_hash = self._hash(raw_token)
        now = _now()
        if self._control_target is None:
            stored = self._setup_tokens.get(token_id)
            if stored is None or not hmac.compare_digest(stored.token_hash, token_hash):
                raise PermissionError("Invalid setup token.")
            if (
                stored.consumed_at is not None
                or stored.revoked_at is not None
                or stored.expires_at <= now
            ):
                raise PermissionError("Invalid setup token.")
            self._setup_tokens[token_id] = replace(stored, consumed_at=now)
            tenant_id = stored.tenant_id
            project_id = stored.project_id
            agent_scope = stored.agent_scope
            session_id = stored.session_id
        else:
            with closing(self._connect_control()) as conn:
                row = conn.execute(
                    """
                    SELECT token_hash, tenant_id, project_id, agent_scope, session_id
                    FROM hosted_setup_tokens
                    WHERE token_id = ?
                    """,
                    (token_id,),
                ).fetchone()
                if row is None or not hmac.compare_digest(row[0], token_hash):
                    raise PermissionError("Invalid setup token.")
                cursor = conn.execute(
                    """
                    UPDATE hosted_setup_tokens
                    SET consumed_at = ?
                    WHERE token_id = ?
                      AND consumed_at IS NULL
                      AND revoked_at IS NULL
                      AND expires_at > ?
                    """,
                    (now, token_id, now),
                )
                conn.commit()
                if cursor.rowcount != 1:
                    raise PermissionError("Invalid setup token.")
            tenant_id, project_id, agent_scope, session_id = row[1], row[2], row[3], row[4]
        # Fail closed: consumption commits before key mint, so a mint failure
        # leaves the token consumed rather than replayable. Recovery is a fresh
        # console-minted token, never a retry of a partially executed exchange.
        provisioned, record = self._mint_control_plane_key(
            tenant_id=tenant_id,
            project_id=project_id,
            name=f"setup-{token_id}",
            agent_scope=agent_scope,
            created_via="setup",
        )
        if self._control_target is None:
            self._setup_tokens[token_id] = replace(
                self._setup_tokens[token_id], consumed_key_id=provisioned.key_id
            )
        else:
            with closing(self._connect_control()) as conn:
                conn.execute(
                    """
                    UPDATE hosted_setup_tokens
                    SET consumed_key_id = ?
                    WHERE token_id = ?
                    """,
                    (provisioned.key_id, token_id),
                )
                conn.commit()
        return SetupTokenExchange(
            provisioned=provisioned,
            key_record=record,
            project_id=project_id,
            session_id=session_id,
            agent_scope=record.agent_scope,
        )

    def revoke_setup_token(
        self,
        *,
        tenant_id: str,
        project_id: str,
        token_id: str,
        revoked_by: str | None = None,
    ) -> None:
        now = _now()
        audit = HostedAuditEvent(
            operation="revoke_setup_token",
            tenant_id=tenant_id,
            principal_id=None,
            status="ok",
            recorded_at=now,
            project_id=project_id,
        )
        if self._control_target is None:
            stored = self._setup_tokens.get(token_id)
            if (
                stored is None
                or stored.tenant_id != tenant_id
                or stored.project_id != project_id
            ):
                raise PermissionError("Unknown setup token.")
            newly_revoked = stored.revoked_at is None
            self._setup_tokens[token_id] = replace(
                stored,
                revoked_at=stored.revoked_at or now,
                revoked_by=stored.revoked_by or revoked_by,
            )
            if newly_revoked:
                self._record_audit_event(audit)
            return
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT revoked_at
                FROM hosted_setup_tokens
                WHERE token_id = ? AND tenant_id = ? AND project_id = ?
                """,
                (token_id, tenant_id, project_id),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown setup token.")
            already_revoked_at = row[0]
            conn.execute(
                """
                UPDATE hosted_setup_tokens
                SET
                    revoked_at = COALESCE(revoked_at, ?),
                    revoked_by = COALESCE(revoked_by, ?)
                WHERE token_id = ?
                """,
                (now, revoked_by, token_id),
            )
            # Same-transaction audit, only on the NULL -> revoked transition.
            if already_revoked_at is None:
                _insert_audit_event(conn, audit)
            conn.commit()

    def list_setup_tokens(
        self,
        *,
        tenant_id: str,
        project_id: str,
        include_consumed: bool = True,
        include_revoked: bool = True,
    ) -> list[HostedSetupTokenRecord]:
        if self._control_target is None:
            records = []
            for stored in self._setup_tokens.values():
                if stored.tenant_id != tenant_id or stored.project_id != project_id:
                    continue
                if stored.consumed_at is not None and not include_consumed:
                    continue
                if stored.revoked_at is not None and not include_revoked:
                    continue
                records.append(
                    HostedSetupTokenRecord(
                        token_id=stored.token_id,
                        tenant_id=stored.tenant_id,
                        project_id=stored.project_id,
                        agent_scope=stored.agent_scope,
                        session_id=stored.session_id,
                        created_at=stored.created_at,
                        expires_at=stored.expires_at,
                        consumed_at=stored.consumed_at,
                        consumed_key_id=stored.consumed_key_id,
                        revoked_at=stored.revoked_at,
                    )
                )
            records.sort(key=lambda record: (record.created_at, record.token_id))
            return records
        filters = ""
        if not include_consumed:
            filters += " AND consumed_at IS NULL"
        if not include_revoked:
            filters += " AND revoked_at IS NULL"
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    token_id, tenant_id, project_id, agent_scope, session_id,
                    created_at, expires_at, consumed_at, consumed_key_id, revoked_at
                FROM hosted_setup_tokens
                WHERE tenant_id = ? AND project_id = ? {filters}
                ORDER BY created_at, token_id
                """,
                (tenant_id, project_id),
            ).fetchall()
        return [
            HostedSetupTokenRecord(
                token_id=row[0],
                tenant_id=row[1],
                project_id=row[2],
                agent_scope=row[3],
                session_id=row[4],
                created_at=row[5],
                expires_at=row[6],
                consumed_at=row[7],
                consumed_key_id=row[8],
                revoked_at=row[9],
            )
            for row in rows
        ]

    @staticmethod
    def _hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_key_id(raw_key: str) -> str:
        parts = raw_key.split("_", 2)
        if len(parts) != 3 or parts[0] != "vx" or not parts[1] or not parts[2]:
            raise PermissionError("Invalid hosted API key.")
        return parts[1]

    def _connect_control(self) -> sqlite3.Connection:
        if self._control_target is None:
            raise RuntimeError("Hosted API key store is not durable.")
        return _connect_control_db(self._control_target)

    def _record_audit_event(self, event: HostedAuditEvent) -> None:
        # In-memory (non-durable) key stores have no control-plane ledger to
        # write to; audit is a hosted-durability concern, so skip silently.
        if self._control_target is None:
            return
        with closing(self._connect_control()) as conn:
            _insert_audit_event(conn, event)
            conn.commit()

    def _init_control_plane_schema(self) -> None:
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_api_keys (
                    key_id TEXT PRIMARY KEY,
                    key_hash TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    project_ids TEXT NOT NULL,
                    agent_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoked_by TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_api_key_metadata (
                    key_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    agent_scope TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    last4 TEXT NOT NULL,
                    display TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (key_id) REFERENCES hosted_api_keys(key_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_setup_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    agent_scope TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_key_id TEXT,
                    revoked_at TEXT,
                    revoked_by TEXT
                )
                """
            )
            # Shared with HostedTenantCatalog on the same control-plane.db; created
            # here too so a standalone key store can still record audit events.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    tenant_id TEXT,
                    principal_id TEXT,
                    status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    error_type TEXT,
                    project_id TEXT,
                    key_id TEXT
                )
                """
            )
            audit_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_audit_events)").fetchall()
            }
            if "project_id" not in audit_columns:
                conn.execute("ALTER TABLE hosted_audit_events ADD COLUMN project_id TEXT")
            if "key_id" not in audit_columns:
                conn.execute("ALTER TABLE hosted_audit_events ADD COLUMN key_id TEXT")
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_api_keys)").fetchall()
            }
            if "last_used_at" not in columns:
                conn.execute("ALTER TABLE hosted_api_keys ADD COLUMN last_used_at TEXT")
            metadata_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(hosted_api_key_metadata)"
                ).fetchall()
            }
            if "created_via" not in metadata_columns:
                conn.execute(
                    "ALTER TABLE hosted_api_key_metadata "
                    "ADD COLUMN created_via TEXT NOT NULL DEFAULT 'console'"
                )
            conn.commit()

    def _load_key(self, key_id: str) -> _HostedApiKey:
        if self._control_target is None:
            try:
                return self._keys[key_id]
            except KeyError as exc:
                raise PermissionError("Invalid hosted API key.") from exc
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT
                    key_id, key_hash, tenant_id, principal_id, capabilities,
                    project_ids, agent_ids, created_at, revoked_at, revoked_by,
                    last_used_at
                FROM hosted_api_keys
                WHERE key_id = ?
                """,
                (key_id,),
            ).fetchone()
        if row is None:
            raise PermissionError("Invalid hosted API key.")
        revoked_at = row[8]
        try:
            return _HostedApiKey(
                key_id=row[0],
                key_hash=row[1],
                tenant_id=row[2],
                principal_id=row[3],
                capabilities=frozenset(
                    MemoryCapability(value) for value in _load_json_list(row[4])
                ),
                project_ids=frozenset(_load_json_list(row[5])),
                agent_ids=frozenset(_load_json_list(row[6], allow_none=True)),
                created_at=row[7],
                revoked_at=revoked_at,
                revoked_by=row[9],
                active=revoked_at is None,
                last_used_at=row[10],
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PermissionError("Invalid hosted API key.") from exc


def _capabilities_json(capabilities: frozenset[MemoryCapability]) -> str:
    return json.dumps(sorted(capability.value for capability in capabilities))


def _strings_json(values: frozenset[str]) -> str:
    return json.dumps(sorted(values))


def _nullable_strings_json(values: frozenset[str | None]) -> str:
    return json.dumps(
        sorted(values, key=lambda value: (0, "") if value is None else (1, value))
    )


def _connect_control_db(target: str | Path | StorageTarget) -> sqlite3.Connection:
    _ensure_control_db_permissions(target)
    if isinstance(target, StorageTarget):
        conn = connect(target)
    else:
        conn = connect(target, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_control_db_permissions(target: str | Path | StorageTarget) -> None:
    """Enforce owner-only read/write on the LOCAL control-plane database file.

    A `StorageTarget` (libSQL/Turso DSN) names a remote, managed database --
    there is no local file to `os.open`/`os.chmod`, and this is a no-op for
    that case. Filesystem permissions only apply to a local `str`/`Path`
    target (the default, filesystem-rooted control plane).
    """
    if isinstance(target, StorageTarget):
        return
    db_path = target
    try:
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, _CONTROL_DB_MODE)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    os.chmod(db_path, _CONTROL_DB_MODE)


def _load_json_list(raw: str, *, allow_none: bool = False) -> list[str | None]:
    values = json.loads(raw)
    if not isinstance(values, list):
        raise TypeError("Expected JSON list.")
    if not all(isinstance(value, str) or (allow_none and value is None) for value in values):
        raise TypeError("Expected JSON string list.")
    return values


def _now() -> str:
    return _to_z(datetime.now(UTC))


def _to_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _last_used_is_fresh(previous: str | None, now: str) -> bool:
    if previous is None:
        return False
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    try:
        return (parse(now) - parse(previous)).total_seconds() < 60
    except ValueError:
        return False
