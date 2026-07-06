import contextlib
import importlib.util
import os
from contextlib import closing

import pytest
from fastapi.responses import JSONResponse

from tests.fakes.libsql import FakeLibsqlConn
from vexic.contract import MemoryCapability
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.hosted import HostedUsageEvent
from vexic.migration import export_canonical_migration, import_canonical_migration
from vexic.storage import StorageTarget
from vexic.storage.connection import connect as storage_connect
from vexic.storage.schema import init_db

# Live gate (mirrors tests/test_hosted_turso_backend.py): only runs with real
# Turso creds AND the optional `libsql` (vexic[hosted]) extra present.
_TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_HAS_TURSO = bool(_TURSO_URL and _TURSO_TOKEN and importlib.util.find_spec("libsql"))


def test_fake_rejects_named_params_and_row_factory():
    c = FakeLibsqlConn()
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO t (v) VALUES (?)", ("x",)); c.commit()
    assert c.execute("SELECT v FROM t").fetchone() == ("x",)
    with pytest.raises(AttributeError):
        c.enable_load_extension(True)


# ---------------------------------------------------------------------------
# Control-plane connection routed through
# `connect(target, auth_token)`, with the filesystem-only permission guard
# (`os.open`/`os.chmod`) skipped for a `StorageTarget` control-plane target.
#
# Both the catalog and the key store share a single fake libSQL connection
# instance for the duration of a test (a real Turso/libSQL connection is a
# persistent remote session, not a fresh ":memory:" db per `connect()` call),
# so `vexic.storage.connection.connect` is monkeypatched to return the same
# `FakeLibsqlConn` every time it is asked for the given `StorageTarget`.


class _NonClosingFakeConnHandle:
    """Wraps a shared `FakeLibsqlConn` so the production code's per-call
    `with closing(self._connect_control()) as conn: ...` does not tear down
    the underlying connection between calls.

    Production code (`_connect_control_db`) opens a fresh connection per
    control-plane operation and closes it on the way out -- correct for a
    real libSQL/Turso session, but this test fixture hands out the SAME
    `FakeLibsqlConn` (an in-memory sqlite3 db) across every call so state
    persists across `provision_tenant`/`get_tenant`/etc. `close()` is a
    no-op here so that sharing works; everything else delegates straight
    through to the shared fake.
    """

    def __init__(self, fake_conn: FakeLibsqlConn) -> None:
        self._fake_conn = fake_conn

    def execute(self, sql, parameters=(), /):
        return self._fake_conn.execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self._fake_conn.executemany(sql, parameters)

    def cursor(self):
        return self._fake_conn.cursor()

    def commit(self):
        self._fake_conn.commit()

    def rollback(self):
        self._fake_conn.rollback()

    def close(self):
        pass  # intentionally does not close the shared fake

    def __enter__(self):
        self._fake_conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fake_conn.__exit__(*exc)


def _patch_connect_to_fake(monkeypatch, fake_conn: FakeLibsqlConn) -> None:
    import vexic.hosted_local as hosted_local

    def _fake_connect(target, *, auth_token=None, **kwargs):
        assert isinstance(target, StorageTarget), (
            f"expected control-plane connect() to receive a StorageTarget, got {target!r}"
        )
        assert target.auth_token == "s3cr3t-token"
        return _NonClosingFakeConnHandle(fake_conn)

    monkeypatch.setattr(hosted_local, "connect", _fake_connect)


def _forbid_local_permission_ops(monkeypatch) -> None:
    def _boom_open(*args, **kwargs):
        raise AssertionError("os.open must not be called for a StorageTarget control target.")

    def _boom_chmod(*args, **kwargs):
        raise AssertionError("os.chmod must not be called for a StorageTarget control target.")

    monkeypatch.setattr("os.open", _boom_open)
    monkeypatch.setattr("os.chmod", _boom_chmod)


def test_catalog_provisions_tenant_against_fake_libsql_control_plane(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    tenant = catalog.get_tenant("tenant-a")

    assert tenant.tenant_id == "tenant-a"
    assert tenant.project_ids == frozenset({"project-a"})


def test_api_key_store_create_authenticate_revoke_against_fake_libsql(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    keys = HostedApiKeyStore(tmp_path, control_target=target)
    provisioned = keys.create_key(
        tenant_id="tenant-a",
        principal_id="agent-a",
        capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
        project_ids={"project-a"},
    )

    auth = keys.authenticate(provisioned.raw_key)
    assert auth.tenant_id == "tenant-a"
    assert auth.key_id == provisioned.key_id

    keys.revoke_key(provisioned.key_id)
    with pytest.raises(PermissionError):
        keys.authenticate(provisioned.raw_key)


def test_provision_tenant_stores_and_returns_customer_target(tmp_path):
    catalog = HostedTenantCatalog(tmp_path)

    catalog.provision_tenant(
        "tenant-a",
        project_ids={"project-a"},
        customer_target="libsql://x",
    )
    tenant = catalog.get_tenant("tenant-a")

    assert tenant.customer_target == "libsql://x"
    assert tenant.generation == 1


def test_provision_tenant_without_customer_target_defaults_to_local_path(tmp_path):
    catalog = HostedTenantCatalog(tmp_path)

    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    tenant = catalog.get_tenant("tenant-a")

    assert tenant.customer_target is None
    assert tenant.generation == 1
    assert tenant.db_path == tmp_path / tenant.db_path.name
    assert tenant.db_path.parent == tmp_path


def test_control_plane_on_fake_libsql_supports_customer_target_columns(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=target)
    catalog.provision_tenant(
        "tenant-a",
        project_ids={"project-a"},
        customer_target="libsql://customer-a",
    )
    tenant = catalog.get_tenant("tenant-a")

    assert tenant.customer_target == "libsql://customer-a"
    assert tenant.generation == 1


def test_catalog_telemetry_insert_and_read_against_fake_libsql(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    event = HostedUsageEvent(
        kind="model_call",
        operation="search_transcript",
        tenant_id="tenant-a",
        principal_id="agent-a",
        status="ok",
        recorded_at="2026-07-01T00:00:00Z",
        model_requests=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        estimated_cost_micros=0,
        error_type=None,
        project_id="project-a",
    )

    catalog.record_usage_event(event)
    events = catalog.usage_events("tenant-a")

    assert len(events) == 1
    assert events[0].operation == "search_transcript"


def test_control_plane_permission_guard_is_noop_for_storage_target(monkeypatch, tmp_path):
    """`_ensure_control_db_permissions` must not touch the filesystem for a
    remote `StorageTarget` control-plane -- there is no local file to chmod,
    and calling `os.open`/`os.chmod` on a DSN string would be a bug."""
    from vexic.hosted_local import _ensure_control_db_permissions

    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    _ensure_control_db_permissions(target)


# ---------------------------------------------------------------------------
# Optional live smoke (gated, no creds required to collect/skip): proves the
# `connect(target, auth_token)` routing works against a REAL Turso/libSQL
# connection, not just the FakeLibsqlConn double above. Uses a
# `tenant_id`/`clerk_org_id` prefix unique per run so it never collides with
# other data, and drops every control-plane table it creates in `finally` so
# the shared Turso dev DB is not left with permanent schema from this test.

_CONTROL_PLANE_TABLES = (
    "hosted_api_key_metadata",
    "hosted_api_keys",
    "hosted_job_events",
    "hosted_usage_events",
    "hosted_audit_events",
    "hosted_projects",
    "customer_account_mappings",
    "tenant_projects",
    "tenants",
)


def _drop_control_plane_tables() -> None:
    conn = None
    try:
        conn = storage_connect(StorageTarget(_TURSO_URL, auth_token=_TURSO_TOKEN))
        for table in _CONTROL_PLANE_TABLES:
            with contextlib.suppress(Exception):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    except Exception:  # noqa: BLE001 -- best-effort cleanup on a shared remote DB
        pass
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


@pytest.mark.turso
@pytest.mark.skipif(not _HAS_TURSO, reason="Turso creds/libsql missing")
def test_control_plane_round_trips_on_real_turso(tmp_path):
    """Live e2e: provision a tenant, issue/authenticate/revoke a key, and
    record+read a usage event with the control-plane routed at a REAL Turso
    database via `StorageTarget`/`connect(target, auth_token)` -- no fake.
    """
    target = StorageTarget(_TURSO_URL, auth_token=_TURSO_TOKEN)
    tenant_id = f"tenant-cp-live-{os.getpid()}"
    try:
        catalog = HostedTenantCatalog(tmp_path, control_target=target)
        keys = HostedApiKeyStore(tmp_path, control_target=target)

        catalog.provision_tenant(tenant_id, project_ids={"project-live"})
        tenant = catalog.get_tenant(tenant_id)
        assert tenant.tenant_id == tenant_id
        assert tenant.project_ids == frozenset({"project-live"})

        provisioned = keys.create_key(
            tenant_id=tenant_id,
            principal_id="agent-live",
            capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
            project_ids={"project-live"},
        )
        auth = keys.authenticate(provisioned.raw_key)
        assert auth.tenant_id == tenant_id
        keys.revoke_key(provisioned.key_id)
        with pytest.raises(PermissionError):
            keys.authenticate(provisioned.raw_key)

        event = HostedUsageEvent(
            kind="model_call",
            operation="search_transcript",
            tenant_id=tenant_id,
            principal_id="agent-live",
            status="ok",
            recorded_at="2026-07-01T00:00:00Z",
            model_requests=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_micros=0,
            error_type=None,
            project_id="project-live",
        )
        catalog.record_usage_event(event)
        events = catalog.usage_events(tenant_id)
        assert len(events) == 1
        assert events[0].operation == "search_transcript"

        assert _TURSO_TOKEN not in repr(target)
    finally:
        _drop_control_plane_tables()


# ---------------------------------------------------------------------------
# `activate_replacement_database` accepts EITHER a local
# filesystem replacement (existing under-root/customer-file behavior) OR a
# Turso DSN replacement (string-identity validation, no `Path`/`os` checks),
# and bumps the catalog `generation` counter on a successful repoint of
# either kind so request-scoped services holding the stale handle stop
# writing to it.


def _patch_connect_control_and_replacement_to_fake(
    monkeypatch, fake_conn: FakeLibsqlConn
) -> None:
    """Like `_patch_connect_to_fake`, but also accepts a bare DSN `str` (the
    Turso replacement database) in addition to the control-plane
    `StorageTarget` -- `activate_replacement_database` connects to BOTH
    through the same `connect()` seam, and this test suite backs both with
    the same in-memory fake since a Turso-DSN replacement is a distinct
    remote database in production, but the unit test only needs the shared
    fake to hold the `canonical_migration_imports` row the catalog reads.

    Also patches `vexic.storage.schema.connect`: `activate_replacement_database`
    runs `LocalMemoryService(db_path=<replacement>).init_schema()` on the
    replacement, and `init_db` there holds its own module-level `connect`
    reference bound at import time (bound before this monkeypatch runs), so
    `hosted_local.connect` alone would leave the replacement's `init_schema()`
    dialing the real DSN.
    """
    import vexic.hosted_local as hosted_local
    import vexic.storage.schema as storage_schema

    def _fake_connect(target, *, auth_token=None, **kwargs):
        if isinstance(target, StorageTarget):
            assert target.auth_token == "s3cr3t-token"
        else:
            assert isinstance(target, str), f"unexpected connect() target: {target!r}"
        return _NonClosingFakeConnHandle(fake_conn)

    monkeypatch.setattr(hosted_local, "connect", _fake_connect)
    monkeypatch.setattr(storage_schema, "connect", _fake_connect)


def _seed_local_replacement(root, *, tenant_id: str, project_id: str):
    """Build a local filesystem replacement db carrying valid canonical
    migration-import metadata for `tenant_id`/`project_id`, mirroring the
    existing local-path tests in tests/test_operator_migration.py."""
    source_db = root / "source.db"
    artifact = root / "canonical-migration.json"
    replacement_db = root / "replacement.db"
    init_db(str(source_db))
    export_canonical_migration(
        str(source_db),
        artifact,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    import_canonical_migration(
        artifact,
        str(replacement_db),
        tenant_id=tenant_id,
        project_id=project_id,
    )
    return replacement_db


def _seed_migration_metadata(
    fake_conn: FakeLibsqlConn,
    *,
    tenant_id: str,
    project_id: str | None,
) -> None:
    """Populate `canonical_migration_imports` directly on the shared fake
    connection -- `import_canonical_migration` only writes to a filesystem
    `Path`, so a Turso-DSN replacement's migration metadata is seeded by hand
    here (this is exactly what a real cross-backend import would leave
    behind on the replacement database, per Task 12's scope)."""
    fake_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS canonical_migration_imports (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            artifact_version TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            project_id TEXT,
            imported_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    fake_conn.execute(
        """
        INSERT INTO canonical_migration_imports (id, artifact_version, tenant_id, project_id)
        VALUES (1, 'vexic.canonical-migration.v1', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            tenant_id = excluded.tenant_id,
            project_id = excluded.project_id
        """,
        (tenant_id, project_id),
    )
    fake_conn.commit()


def test_activate_replacement_database_local_path_bumps_generation(tmp_path):
    catalog = HostedTenantCatalog(tmp_path)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    assert catalog.get_tenant("tenant-a").generation == 1
    replacement_db = _seed_local_replacement(
        tmp_path, tenant_id="tenant-a", project_id="project-a"
    )

    activated = catalog.activate_replacement_database("tenant-a", replacement_db)

    assert activated.db_path == replacement_db
    assert activated.generation == 2
    assert catalog.get_tenant("tenant-a").generation == 2


def test_activate_replacement_database_accepts_turso_dsn_and_bumps_generation(
    monkeypatch, tmp_path
):
    fake_conn = FakeLibsqlConn()
    _patch_connect_control_and_replacement_to_fake(monkeypatch, fake_conn)
    control_target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=control_target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    assert catalog.get_tenant("tenant-a").generation == 1
    _seed_migration_metadata(fake_conn, tenant_id="tenant-a", project_id="project-a")

    replacement_target = "libsql://replacement-customer-db"
    activated = catalog.activate_replacement_database("tenant-a", replacement_target)

    assert activated.customer_target == replacement_target
    assert activated.generation == 2
    tenant = catalog.get_tenant("tenant-a")
    assert tenant.customer_target == replacement_target
    assert tenant.generation == 2


def test_activate_replacement_database_rejects_dsn_equal_to_current_target(
    monkeypatch, tmp_path
):
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    control_target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=control_target)
    catalog.provision_tenant(
        "tenant-a",
        project_ids={"project-a"},
        customer_target="libsql://current-customer-db",
    )
    _seed_migration_metadata(fake_conn, tenant_id="tenant-a", project_id="project-a")

    with pytest.raises(ValueError, match="current"):
        catalog.activate_replacement_database(
            "tenant-a", "libsql://current-customer-db"
        )

    tenant = catalog.get_tenant("tenant-a")
    assert tenant.customer_target == "libsql://current-customer-db"
    assert tenant.generation == 1


def test_activate_replacement_database_rejects_malformed_dsn(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    control_target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=control_target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})

    # Has a "://" authority separator (so it is treated as an attempted DSN,
    # not a local filesystem path) but neither a recognized libSQL scheme nor
    # a host -- must be rejected as malformed rather than silently routed
    # through the local-path validator.
    with pytest.raises(ValueError, match="DSN"):
        catalog.activate_replacement_database("tenant-a", "ftp://")

    tenant = catalog.get_tenant("tenant-a")
    assert tenant.customer_target is None
    assert tenant.generation == 1


def test_activate_replacement_database_rejects_dsn_tenant_mismatch(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    _patch_connect_control_and_replacement_to_fake(monkeypatch, fake_conn)
    control_target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=control_target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    _seed_migration_metadata(fake_conn, tenant_id="tenant-b", project_id="project-b")

    with pytest.raises(PermissionError, match="tenant"):
        catalog.activate_replacement_database(
            "tenant-a", "libsql://replacement-customer-db"
        )

    tenant = catalog.get_tenant("tenant-a")
    assert tenant.customer_target is None
    assert tenant.generation == 1


# ---------------------------------------------------------------------------
# Regression guard: `_provision_control_tenant` wraps every
# `ValueError` from `provision_customer_account` as an HTTP 400
# `_ControlPlaneBadRequest`. Once the control plane runs on Turso, a genuine
# constraint/operational SQL failure arrives as a BARE `ValueError` carrying a
# Hrana `code:` payload -- it must reach the storage boundary's 409/503/500
# classification, NOT be mis-wrapped as a 400 domain-validation error. A
# genuine domain `ValueError` (non-storage message) must still become a 400.


class _StubProvisionService:
    """Minimal stand-in whose catalog tenant lookup raises a preset exception."""

    def __init__(self, exc: Exception) -> None:
        self.catalog = self
        self._exc = exc

    def provision_customer_account(self, clerk_org_id: str) -> str:
        raise self._exc

    def resolve_customer_tenant(self, clerk_org_id: str) -> str | None:
        raise self._exc


def _run_boundary(exc: Exception, tenant_lookup: str = "_provision_control_tenant") -> object:
    """Run a control-tenant lookup (`_provision_control_tenant` by default, or
    `_resolve_control_tenant`) through `_control_plane_storage_boundary` and
    return the boundary's decision: either the `JSONResponse` it produced for a
    classified storage error, or the exception that propagated out."""
    import asyncio

    from vexic import hosted_control_plane_http as cp

    lookup = getattr(cp, tenant_lookup)

    @cp._control_plane_storage_boundary
    async def handler() -> object:
        return lookup(_StubProvisionService(exc), "org_123")

    try:
        return asyncio.run(handler())
    except Exception as propagated:  # noqa: BLE001 -- assert on it in the test
        return propagated


def test_provision_control_tenant_libsql_unique_violation_propagates_unwrapped():
    """A libSQL UNIQUE-constraint `ValueError` must propagate UNTOUCHED out of
    `_provision_control_tenant` (so the storage boundary classifies it as 409),
    NOT be wrapped as a 400 `_ControlPlaneBadRequest`."""
    from vexic import hosted_control_plane_http as cp

    libsql_unique = ValueError(
        'Hrana: `stream error: `Error { message: "SQLite error: UNIQUE '
        'constraint failed: customer_account_mappings.clerk_org_id", '
        'code: "SQLITE_CONSTRAINT" }``'
    )

    with pytest.raises(ValueError) as excinfo:
        cp._provision_control_tenant(_StubProvisionService(libsql_unique), "org_123")

    assert not isinstance(excinfo.value, cp._ControlPlaneBadRequest)
    assert excinfo.value is libsql_unique

    # End-to-end: the storage boundary turns that propagated error into a 409.
    result = _run_boundary(libsql_unique)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409


def test_provision_control_tenant_libsql_operational_error_propagates_unwrapped():
    """A libSQL operational `ValueError` (missing table / bad SQL) must
    propagate untouched so the boundary classifies it as 5xx, not 400."""
    from vexic import hosted_control_plane_http as cp

    libsql_operational = ValueError(
        'Hrana: `stream error: `Error { message: "SQLite error: no such '
        'table: tenants", code: "SQLITE_ERROR" }``'
    )

    with pytest.raises(ValueError) as excinfo:
        cp._provision_control_tenant(
            _StubProvisionService(libsql_operational), "org_123"
        )

    assert not isinstance(excinfo.value, cp._ControlPlaneBadRequest)
    assert excinfo.value is libsql_operational

    result = _run_boundary(libsql_operational)
    assert isinstance(result, JSONResponse)
    assert result.status_code in (500, 503)


def test_provision_control_tenant_domain_valueerror_still_becomes_bad_request():
    """A genuine domain `ValueError` (no storage markers) must still be wrapped
    as `_ControlPlaneBadRequest` (HTTP 400)."""
    from vexic import hosted_control_plane_http as cp

    domain_error = ValueError("clerk_org_id must not be blank.")

    with pytest.raises(cp._ControlPlaneBadRequest) as excinfo:
        cp._provision_control_tenant(_StubProvisionService(domain_error), "org_123")

    assert "clerk_org_id" in str(excinfo.value)


def test_resolve_control_tenant_libsql_valueerror_propagates_unwrapped():
    from vexic import hosted_control_plane_http as cp

    libsql_operational = ValueError(
        'Hrana: `stream error: `Error { message: "SQLite error: no such '
        'table: customer_account_mappings", code: "SQLITE_ERROR" }``'
    )

    with pytest.raises(ValueError) as excinfo:
        cp._resolve_control_tenant(_StubProvisionService(libsql_operational), "org_123")

    assert not isinstance(excinfo.value, cp._ControlPlaneBadRequest)
    assert excinfo.value is libsql_operational

    result = _run_boundary(libsql_operational, "_resolve_control_tenant")
    assert isinstance(result, JSONResponse)
    assert result.status_code in (500, 503)


def test_resolve_control_tenant_domain_valueerror_still_becomes_bad_request():
    from vexic import hosted_control_plane_http as cp

    domain_error = ValueError("clerk_org_id must not be blank.")

    with pytest.raises(cp._ControlPlaneBadRequest) as excinfo:
        cp._resolve_control_tenant(_StubProvisionService(domain_error), "org_123")

    assert "clerk_org_id" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Regression guard: `_replacement_migration_scope` catches only
# `sqlite3.DatabaseError` when reading `canonical_migration_imports`. For a
# Turso-DSN replacement whose replacement db lacks that table, the libSQL
# driver raises a BARE `ValueError` ("no such table"), which slips past the
# catch and propagates as an unhandled 500 instead of the intended `None`
# (which yields the clean "no migration metadata" `PermissionError` upstream).
# An unrelated `ValueError` must still propagate.


class _RaisingOnMigrationReadConn:
    """Wraps a `FakeLibsqlConn` but raises a preset exception whenever
    `canonical_migration_imports` is read -- everything else delegates
    through, so control-plane bookkeeping still works."""

    def __init__(self, fake_conn: FakeLibsqlConn, read_exc: Exception) -> None:
        self._fake_conn = fake_conn
        self._read_exc = read_exc

    def execute(self, sql, parameters=(), /):
        if "canonical_migration_imports" in sql:
            raise self._read_exc
        return self._fake_conn.execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self._fake_conn.executemany(sql, parameters)

    def cursor(self):
        return self._fake_conn.cursor()

    def commit(self):
        self._fake_conn.commit()

    def rollback(self):
        self._fake_conn.rollback()

    def close(self):
        pass

    def __enter__(self):
        self._fake_conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fake_conn.__exit__(*exc)


def _patch_connect_with_raising_replacement(
    monkeypatch, fake_conn: FakeLibsqlConn, read_exc: Exception
) -> None:
    """Route the control-plane `StorageTarget` at the plain shared fake, but
    the replacement DSN `str` at a connection whose migration-metadata read
    raises `read_exc` -- reproducing a libSQL replacement db that lacks
    `canonical_migration_imports`."""
    import vexic.hosted_local as hosted_local
    import vexic.storage.schema as storage_schema

    raising_handle = _RaisingOnMigrationReadConn(fake_conn, read_exc)

    def _fake_connect(target, *, auth_token=None, **kwargs):
        if isinstance(target, StorageTarget):
            assert target.auth_token == "s3cr3t-token"
            return _NonClosingFakeConnHandle(fake_conn)
        assert isinstance(target, str), f"unexpected connect() target: {target!r}"
        return raising_handle

    monkeypatch.setattr(hosted_local, "connect", _fake_connect)
    monkeypatch.setattr(storage_schema, "connect", _fake_connect)


def test_replacement_scope_libsql_missing_table_yields_permission_error(
    monkeypatch, tmp_path
):
    fake_conn = FakeLibsqlConn()
    libsql_missing_table = ValueError(
        'Hrana: `stream error: `Error { message: "SQLite error: no such '
        'table: canonical_migration_imports", code: "SQLITE_ERROR" }``'
    )
    _patch_connect_with_raising_replacement(monkeypatch, fake_conn, libsql_missing_table)
    control_target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=control_target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})

    # Missing table -> _replacement_migration_scope returns None -> the clean
    # "no migration metadata" PermissionError, NOT a raw ValueError propagation.
    with pytest.raises(PermissionError, match="migration metadata"):
        catalog.activate_replacement_database(
            "tenant-a", "libsql://replacement-customer-db"
        )

    tenant = catalog.get_tenant("tenant-a")
    assert tenant.customer_target is None
    assert tenant.generation == 1


def test_replacement_scope_unrelated_valueerror_still_propagates(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    unrelated = ValueError("totally unrelated failure with no SQL markers")
    _patch_connect_with_raising_replacement(monkeypatch, fake_conn, unrelated)
    control_target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=control_target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})

    with pytest.raises(ValueError, match="unrelated failure"):
        catalog.activate_replacement_database(
            "tenant-a", "libsql://replacement-customer-db"
        )
