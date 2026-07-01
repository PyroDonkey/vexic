from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from vexic.contract import MemoryCapability, Principal, PrincipalType
from vexic.hosted import (
    HostedAuditEvent,
    HostedAuthContext,
    HostedJobEvent,
    HostedTenant,
    HostedUsageEvent,
)
from vexic.service import LocalMemoryService
from vexic.storage.connection import StorageTarget, connect


_CONTROL_DB_MODE = 0o600
_CONTROL_PLANE_AGENT_CAPABILITIES = frozenset(
    {
        MemoryCapability.WRITE,
        MemoryCapability.SEARCH,
        MemoryCapability.EXPAND_HISTORY,
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
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        error_type TEXT
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
)


@dataclass(frozen=True)
class ProvisionedApiKey:
    key_id: str
    raw_key: str


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


class HostedTenantCatalog:
    def __init__(
        self,
        root_path: str | Path,
        *,
        control_target: StorageTarget | None = None,
    ) -> None:
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)
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
                SELECT db_filename, active
                FROM tenants
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
            if row is None:
                db_filename = self._allocate_db_filename(conn)
                conn.execute(
                    """
                    INSERT INTO tenants (tenant_id, db_filename, active)
                    VALUES (?, ?, 0)
                    """,
                    (tenant_id, db_filename),
                )
                conn.commit()
                needs_customer_init = True
            else:
                db_filename = row[0]
                needs_customer_init = not bool(row[1])
            if needs_customer_init:
                tenant_db_path = self.root_path / db_filename
                LocalMemoryService(db_path=str(tenant_db_path), tenant_id=tenant_id).init_schema()
            conn.execute(
                """
                UPDATE tenants
                SET active = 1
                WHERE tenant_id = ?
                """,
                (tenant_id,),
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
                conn.execute(
                    """
                    INSERT INTO tenants (tenant_id, db_filename, active)
                    VALUES (?, ?, 0)
                    """,
                    (tenant_id, db_filename),
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
                WHERE tenant_id = ?
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
                WHERE tenant_id = ? AND project_id = ?
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
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be blank.")
        candidate = Path(replacement_db_path)
        if not candidate.is_absolute():
            candidate = self.root_path / candidate
        root = self.root_path.resolve()
        replacement = candidate.resolve()
        try:
            relative = replacement.relative_to(root)
        except ValueError as exc:
            raise ValueError("replacement database must be under hosted root.") from exc
        if len(relative.parts) != 1 or relative.name == "control-plane.db":
            raise ValueError("replacement database must be a customer database file.")
        if not replacement.is_file():
            raise FileNotFoundError(f"Replacement database does not exist: {replacement}")

        db_filename = relative.name
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
            migration_scope = self._replacement_migration_scope(replacement)
            if migration_scope is None:
                raise PermissionError("Replacement database has no migration metadata.")
            imported_tenant_id, imported_project_id = migration_scope
            if imported_tenant_id != tenant_id:
                raise PermissionError("Replacement database tenant does not match catalog tenant.")
            if imported_project_id not in project_ids:
                raise PermissionError("Replacement database project is outside catalog tenant projects.")
            LocalMemoryService(db_path=str(replacement), tenant_id=tenant_id).init_schema()
            conn.execute(
                """
                UPDATE tenants
                SET db_filename = ?, active = 1
                WHERE tenant_id = ?
                """,
                (db_filename, tenant_id),
            )
            conn.commit()
            return self._tenant_from_filename(conn, tenant_id, db_filename)

    def _replacement_migration_scope(self, db_path: Path) -> tuple[str, str | None] | None:
        try:
            with closing(connect(db_path)) as conn:
                row = conn.execute(
                    """
                    SELECT tenant_id, project_id
                    FROM canonical_migration_imports
                    WHERE id = 1
                    """
                ).fetchone()
        except sqlite3.DatabaseError:
            return None
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

    def record_audit_event(self, event: HostedAuditEvent) -> None:
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                INSERT INTO hosted_audit_events (
                    operation, tenant_id, principal_id, status, recorded_at, error_type
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.operation,
                    event.tenant_id,
                    event.principal_id,
                    event.status,
                    event.recorded_at,
                    event.error_type,
                ),
            )
            conn.commit()

    def record_usage_event(self, event: HostedUsageEvent) -> None:
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                INSERT INTO hosted_usage_events (
                    kind, operation, tenant_id, principal_id, status, recorded_at,
                    model_requests, input_tokens, output_tokens, total_tokens,
                    estimated_cost_micros, error_type, project_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

    def record_job_event(self, event: HostedJobEvent) -> None:
        with closing(self._connect_control()) as conn:
            conn.execute(
                """
                INSERT INTO hosted_job_events (
                    job_id, operation, tenant_id, principal_id, status, recorded_at,
                    phase, error_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

    def audit_events(self, tenant_id: str | None) -> list[HostedAuditEvent]:
        with closing(self._connect_control()) as conn:
            if tenant_id is None:
                rows = conn.execute(
                    """
                    SELECT operation, tenant_id, principal_id, status, recorded_at, error_type
                    FROM hosted_audit_events
                    WHERE tenant_id IS NULL
                    ORDER BY id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT operation, tenant_id, principal_id, status, recorded_at, error_type
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
                       estimated_cost_micros, error_type, project_id
                FROM hosted_usage_events
                WHERE {where_clause}
                ORDER BY id
                """,
                tuple(params),
            ).fetchall()
        return [HostedUsageEvent(*row) for row in rows]

    def job_events(self, tenant_id: str) -> list[HostedJobEvent]:
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                """
                SELECT
                    job_id, operation, tenant_id, principal_id, status,
                    recorded_at, phase, error_type
                FROM hosted_job_events
                WHERE tenant_id = ?
                ORDER BY id
                """,
                (tenant_id,),
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
            conn.commit()

    def _allocate_db_filename(self, conn: sqlite3.Connection) -> str:
        # ponytail: local staging assumes serialized provisioning; retry INSERT on
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
        return HostedTenant(
            tenant_id=tenant_id,
            db_path=self.root_path / db_filename,
            project_ids=frozenset(row[0] for row in project_rows),
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

    def revoke_key(self, key_id: str, *, revoked_by: str | None = None) -> None:
        if self._control_target is None:
            try:
                stored = self._keys[key_id]
            except KeyError as exc:
                raise PermissionError("Unknown hosted API key.") from exc
            self._keys[key_id] = replace(
                stored,
                active=False,
                revoked_at=stored.revoked_at or _now(),
                revoked_by=stored.revoked_by or revoked_by,
            )
            return
        with closing(self._connect_control()) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM hosted_api_keys
                WHERE key_id = ?
                """,
                (key_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Unknown hosted API key.")
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
        agent_scope = agent_scope.strip() or "shared"
        provisioned = self.create_key(
            tenant_id=tenant_id,
            principal_id=agent_scope,
            capabilities=_CONTROL_PLANE_AGENT_CAPABILITIES,
            project_ids={project_id},
            agent_ids=frozenset() if agent_scope == "shared" else {agent_scope},
        )
        stored = self._load_key(provisioned.key_id)
        prefix = provisioned.raw_key[:16]
        last4 = provisioned.raw_key[-4:]
        record = HostedApiKeyRecord(
            key_id=provisioned.key_id,
            tenant_id=tenant_id,
            project_id=project_id,
            name=name.strip(),
            capability="v1-memory",
            agent_scope=agent_scope,
            prefix=prefix,
            last4=last4,
            display=f"{prefix}...{last4}",
            created_at=stored.created_at or _now(),
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
                            key_prefix, last4, display, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        ),
                    )
                    conn.commit()
            except Exception:
                # ponytail: compensate here instead of threading a shared transaction through create_key.
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
    ) -> list[HostedApiKeyRecord]:
        if self._control_target is None:
            return [
                replace(record, revoked_at=self._keys[record.key_id].revoked_at)
                for record in self._control_metadata.values()
                if record.tenant_id == tenant_id
                and record.project_id == project_id
                and self._keys[record.key_id].revoked_at is None
            ]
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                """
                SELECT
                    meta.key_id, meta.tenant_id, meta.project_id, meta.name,
                    meta.capability, meta.agent_scope, meta.key_prefix,
                    meta.last4, meta.display, meta.created_at, keys.revoked_at
                FROM hosted_api_key_metadata AS meta
                JOIN hosted_api_keys AS keys ON keys.key_id = meta.key_id
                WHERE meta.tenant_id = ? AND meta.project_id = ? AND keys.revoked_at IS NULL
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
                    project_ids, agent_ids, created_at, revoked_at, revoked_by
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
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
