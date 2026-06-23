from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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
    created_at: str | None = None
    revoked_at: str | None = None
    revoked_by: str | None = None
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
    def __init__(self, root_path: str | Path | None = None) -> None:
        self._keys: dict[str, _HostedApiKey] = {}
        self.root_path = Path(root_path) if root_path is not None else None
        self._control_db_path: Path | None = None
        if self.root_path is not None:
            self.root_path.mkdir(parents=True, exist_ok=True)
            self._control_db_path = self.root_path / "control-plane.db"
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
        if self._control_db_path is None:
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
        if self._control_db_path is None:
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
        if self._control_db_path is None:
            raise RuntimeError("Hosted API key store is not durable.")
        conn = sqlite3.connect(self._control_db_path, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

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
            conn.commit()

    def _load_key(self, key_id: str) -> _HostedApiKey:
        if self._control_db_path is None:
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
        return _HostedApiKey(
            key_id=row[0],
            key_hash=row[1],
            tenant_id=row[2],
            principal_id=row[3],
            capabilities=frozenset(MemoryCapability(value) for value in json.loads(row[4])),
            project_ids=frozenset(json.loads(row[5])),
            agent_ids=frozenset(json.loads(row[6])),
            created_at=row[7],
            revoked_at=revoked_at,
            revoked_by=row[9],
            active=revoked_at is None,
        )


def _capabilities_json(capabilities: frozenset[MemoryCapability]) -> str:
    return json.dumps(sorted(capability.value for capability in capabilities))


def _strings_json(values: frozenset[str]) -> str:
    return json.dumps(sorted(values))


def _nullable_strings_json(values: frozenset[str | None]) -> str:
    return json.dumps(
        sorted(values, key=lambda value: (0, "") if value is None else (1, value))
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
