from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from vexic.contract import MemoryCapability, Principal, PrincipalType
from vexic.hosted import HostedAuditEvent, HostedAuthContext, HostedTenant, HostedUsageEvent
from vexic.service import LocalMemoryService


@dataclass(frozen=True)
class ProvisionedApiKey:
    key_id: str
    raw_key: str


@dataclass(frozen=True)
class _HostedApiKey:
    key_id: str
    key_hash: str
    tenant_id: str
    principal_id: str
    capabilities: frozenset[MemoryCapability]
    project_ids: frozenset[str]
    agent_ids: frozenset[str | None]
    active: bool = True


class HostedTenantCatalog:
    def __init__(self, root_path: str | Path) -> None:
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)
        self._control_db_path = self.root_path / "control-plane.db"
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
                self._init_telemetry_schema(tenant_db_path)
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
        if event.tenant_id is None:
            return
        tenant = self.get_tenant(event.tenant_id)
        with closing(sqlite3.connect(tenant.db_path)) as conn:
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
        if event.tenant_id is None:
            return
        tenant = self.get_tenant(event.tenant_id)
        with closing(sqlite3.connect(tenant.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO hosted_usage_events (
                    kind, operation, tenant_id, principal_id, status, recorded_at,
                    model_requests, input_tokens, output_tokens, total_tokens,
                    estimated_cost_micros, error_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

    def audit_events(self, tenant_id: str) -> list[HostedAuditEvent]:
        tenant = self.get_tenant(tenant_id)
        with closing(sqlite3.connect(tenant.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT operation, tenant_id, principal_id, status, recorded_at, error_type
                FROM hosted_audit_events
                ORDER BY id
                """
            ).fetchall()
        return [HostedAuditEvent(*row) for row in rows]

    def usage_events(self, tenant_id: str) -> list[HostedUsageEvent]:
        tenant = self.get_tenant(tenant_id)
        with closing(sqlite3.connect(tenant.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT kind, operation, tenant_id, principal_id, status, recorded_at,
                       model_requests, input_tokens, output_tokens, total_tokens,
                       estimated_cost_micros, error_type
                FROM hosted_usage_events
                ORDER BY id
                """
            ).fetchall()
        return [HostedUsageEvent(*row) for row in rows]

    def _connect_control(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._control_db_path, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_control_plane_schema(self) -> None:
        with closing(self._connect_control()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    db_filename TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS tenant_projects (
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, project_id),
                    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
                );
                """
            )
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

    @staticmethod
    def _init_telemetry_schema(db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS hosted_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT,
                    status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    error_type TEXT
                );

                CREATE TABLE IF NOT EXISTS hosted_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT,
                    status TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    model_requests INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_micros INTEGER NOT NULL DEFAULT 0,
                    error_type TEXT
                );
                """
            )
            conn.commit()


class HostedApiKeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, _HostedApiKey] = {}

    def create_key(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        capabilities: set[MemoryCapability] | frozenset[MemoryCapability],
        project_ids: set[str] | frozenset[str] = frozenset(),
        agent_ids: set[str | None] | frozenset[str | None] = frozenset(),
    ) -> ProvisionedApiKey:
        raw_key = f"vx_{secrets.token_urlsafe(32)}"
        key_id = secrets.token_hex(8)
        self._keys[key_id] = _HostedApiKey(
            key_id=key_id,
            key_hash=self._hash(raw_key),
            tenant_id=tenant_id,
            principal_id=principal_id,
            capabilities=frozenset(capabilities),
            project_ids=frozenset(project_ids),
            agent_ids=frozenset(agent_ids),
        )
        return ProvisionedApiKey(key_id=key_id, raw_key=raw_key)

    def authenticate(self, raw_key: str) -> HostedAuthContext:
        key_hash = self._hash(raw_key)
        # ponytail: linear scan is fine for MVP; index by hash when key counts matter.
        for stored in self._keys.values():
            if stored.active and hmac.compare_digest(stored.key_hash, key_hash):
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

    def revoke_key(self, key_id: str) -> None:
        try:
            stored = self._keys[key_id]
        except KeyError as exc:
            raise PermissionError("Unknown hosted API key.") from exc
        self._keys[key_id] = replace(stored, active=False)

    @staticmethod
    def _hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
