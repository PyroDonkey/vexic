import contextlib
import hashlib
import importlib.util
import os
from datetime import datetime

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
    c.execute("INSERT INTO t (v) VALUES (?)", ("x",))
    c.commit()
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


def test_dream_lease_contends_correctly_against_fake_libsql(monkeypatch, tmp_path):
    # The lease decides a race by reading `cursor.rowcount` off a conditional
    # upsert. The control plane runs on libSQL in production, so pin the
    # contended path against the libSQL driver shape, not just sqlite3: if
    # rowcount did not survive the backend swap, every container would think it
    # won the scope and the collision this lease exists to stop would return.
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")

    catalog = HostedTenantCatalog(tmp_path, control_target=target)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})

    assert catalog.acquire_dream_lease(
        "tenant-a",
        None,
        holder="container-1",
        now="2026-07-12T00:00:00+00:00",
        expires_at="2026-07-12T00:20:00+00:00",
    )
    assert not catalog.acquire_dream_lease(
        "tenant-a",
        None,
        holder="container-2",
        now="2026-07-12T00:05:00+00:00",
        expires_at="2026-07-12T00:25:00+00:00",
    )

    catalog.release_dream_lease("tenant-a", None, holder="container-1")

    assert catalog.acquire_dream_lease(
        "tenant-a",
        None,
        holder="container-2",
        now="2026-07-12T00:06:00+00:00",
        expires_at="2026-07-12T00:26:00+00:00",
    )


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


# ---------------------------------------------------------------------------
# Single-use setup tokens minted by the console, exchanged once by an agent
# for a project-scoped control-plane API key (ADR 0026).


def _setup_token_store(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    _patch_connect_to_fake(monkeypatch, fake_conn)
    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")
    return HostedApiKeyStore(tmp_path, control_target=target), fake_conn


def _parse_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_setup_token_mint_returns_raw_token_and_stores_only_hash(monkeypatch, tmp_path):
    keys, fake_conn = _setup_token_store(monkeypatch, tmp_path)

    provisioned, record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="sess-1"
    )

    assert provisioned.raw_token.startswith("vxsetup_")
    assert record.token_id == provisioned.token_id
    assert record.tenant_id == "tenant-a"
    assert record.project_id == "project-a"
    assert record.agent_scope == "shared"
    assert record.session_id == "sess-1"
    assert record.consumed_at is None
    assert record.consumed_key_id is None
    assert record.revoked_at is None
    ttl = (_parse_z(record.expires_at) - _parse_z(record.created_at)).total_seconds()
    assert abs(ttl - 600) < 1

    row = fake_conn.execute(
        "SELECT token_hash FROM hosted_setup_tokens WHERE token_id = ?",
        (record.token_id,),
    ).fetchone()
    assert row[0] == hashlib.sha256(provisioned.raw_token.encode("utf-8")).hexdigest()
    stored_cells = fake_conn.execute("SELECT * FROM hosted_setup_tokens").fetchall()
    assert all(
        provisioned.raw_token not in str(cell)
        for stored_row in stored_cells
        for cell in stored_row
    )


def test_setup_token_mint_generates_session_id_when_blank(monkeypatch, tmp_path):
    keys, fake_conn = _setup_token_store(monkeypatch, tmp_path)

    _, blank_record = keys.create_setup_token(tenant_id="tenant-a", project_id="project-a")
    assert blank_record.session_id.startswith("setup-")
    assert len(blank_record.session_id) > len("setup-")
    stored = fake_conn.execute(
        "SELECT session_id FROM hosted_setup_tokens WHERE token_id = ?",
        (blank_record.token_id,),
    ).fetchone()
    assert stored[0] == blank_record.session_id

    _, explicit_record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="sess-explicit"
    )
    assert explicit_record.session_id == "sess-explicit"


def test_setup_token_exchange_mints_scoped_key_once(monkeypatch, tmp_path):
    from vexic.hosted_local import _CONTROL_PLANE_AGENT_CAPABILITIES

    keys, fake_conn = _setup_token_store(monkeypatch, tmp_path)
    provisioned_token, token_record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="sess-1"
    )

    exchange = keys.exchange_setup_token(provisioned_token.raw_token)

    assert exchange.project_id == "project-a"
    assert exchange.session_id == "sess-1"
    assert exchange.agent_scope == "shared"
    assert exchange.key_record.key_id == exchange.provisioned.key_id
    auth = keys.authenticate(exchange.provisioned.raw_key)
    assert auth.tenant_id == "tenant-a"
    assert auth.project_ids == frozenset({"project-a"})
    assert auth.capabilities == _CONTROL_PLANE_AGENT_CAPABILITIES
    row = fake_conn.execute(
        "SELECT consumed_at, consumed_key_id FROM hosted_setup_tokens WHERE token_id = ?",
        (token_record.token_id,),
    ).fetchone()
    assert row[0] is not None
    assert row[1] == exchange.provisioned.key_id


def test_setup_token_exchange_replay_fails(monkeypatch, tmp_path):
    keys, _ = _setup_token_store(monkeypatch, tmp_path)
    provisioned_token, _ = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a"
    )
    exchange = keys.exchange_setup_token(provisioned_token.raw_token)

    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(provisioned_token.raw_token)

    auth = keys.authenticate(exchange.provisioned.raw_key)
    assert auth.key_id == exchange.provisioned.key_id


def test_setup_token_exchange_rejects_expired_token(monkeypatch, tmp_path):
    keys, _ = _setup_token_store(monkeypatch, tmp_path)
    provisioned_token, _ = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", ttl_seconds=0
    )

    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(provisioned_token.raw_token)


def test_setup_token_exchange_rejects_wrong_secret_and_malformed(monkeypatch, tmp_path):
    keys, _ = _setup_token_store(monkeypatch, tmp_path)
    provisioned_token, record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a"
    )

    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(f"vxsetup_{record.token_id}_wrong-secret")
    for malformed in (f"vx_{record.token_id}_secret", "", "vxsetup", "vxsetup_", "no-underscores"):
        with pytest.raises(PermissionError, match="Invalid setup token."):
            keys.exchange_setup_token(malformed)

    # A wrong secret must not have consumed the token.
    exchange = keys.exchange_setup_token(provisioned_token.raw_token)
    assert keys.authenticate(exchange.provisioned.raw_key).tenant_id == "tenant-a"


def test_setup_token_revoke_before_use_blocks_exchange(monkeypatch, tmp_path):
    keys, _ = _setup_token_store(monkeypatch, tmp_path)
    provisioned_token, record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a"
    )

    with pytest.raises(PermissionError):
        keys.revoke_setup_token(
            tenant_id="tenant-b", project_id="project-a", token_id=record.token_id
        )
    with pytest.raises(PermissionError):
        keys.revoke_setup_token(
            tenant_id="tenant-a", project_id="project-b", token_id=record.token_id
        )
    with pytest.raises(PermissionError):
        keys.revoke_setup_token(
            tenant_id="tenant-a", project_id="project-a", token_id="no-such-token"
        )

    keys.revoke_setup_token(
        tenant_id="tenant-a", project_id="project-a", token_id=record.token_id
    )
    keys.revoke_setup_token(
        tenant_id="tenant-a", project_id="project-a", token_id=record.token_id
    )

    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(provisioned_token.raw_token)


def test_setup_token_list_filters_by_scope_and_reflects_state(monkeypatch, tmp_path):
    keys, fake_conn = _setup_token_store(monkeypatch, tmp_path)

    pending, _ = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="pending"
    )
    consumed_token, consumed_record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="consumed"
    )
    revoked_token, revoked_record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="revoked"
    )
    expired_token, expired_record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="expired", ttl_seconds=0
    )
    # Same tenant, different project, and a different tenant: both excluded.
    keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-b", session_id="other-project"
    )
    keys.create_setup_token(
        tenant_id="tenant-b", project_id="project-a", session_id="other-tenant"
    )

    keys.exchange_setup_token(consumed_token.raw_token)
    keys.revoke_setup_token(
        tenant_id="tenant-a", project_id="project-a", token_id=revoked_record.token_id
    )

    listed = keys.list_setup_tokens(tenant_id="tenant-a", project_id="project-a")
    by_id = {record.token_id: record for record in listed}

    assert set(by_id) == {
        pending.token_id,
        consumed_record.token_id,
        revoked_record.token_id,
        expired_record.token_id,
    }
    # ORDER BY created_at, token_id -> creation order is preserved.
    assert [record.session_id for record in listed] == [
        "pending",
        "consumed",
        "revoked",
        "expired",
    ]

    assert by_id[pending.token_id].consumed_at is None
    assert by_id[pending.token_id].revoked_at is None
    assert by_id[consumed_record.token_id].consumed_at is not None
    assert by_id[consumed_record.token_id].consumed_key_id is not None
    assert by_id[revoked_record.token_id].revoked_at is not None
    # ttl_seconds=0 -> already expired (expires_at not in the future).
    assert by_id[expired_record.token_id].expires_at == expired_record.created_at

    # include flags narrow the result to active (unconsumed, unrevoked) tokens.
    active_only = keys.list_setup_tokens(
        tenant_id="tenant-a",
        project_id="project-a",
        include_consumed=False,
        include_revoked=False,
    )
    assert {record.token_id for record in active_only} == {
        pending.token_id,
        expired_record.token_id,
    }

    # No raw token material ever persists or surfaces; the DTO has no hash field.
    stored_cells = fake_conn.execute("SELECT * FROM hosted_setup_tokens").fetchall()
    for raw in (pending.raw_token, consumed_token.raw_token, revoked_token.raw_token):
        assert all(raw not in str(cell) for row in stored_cells for cell in row)
    assert all(not hasattr(record, "token_hash") for record in listed)


def test_exchanged_key_metadata_carries_setup_provenance(monkeypatch, tmp_path):
    keys, fake_conn = _setup_token_store(monkeypatch, tmp_path)

    # Pre-seed a legacy metadata table WITHOUT created_via on a second store's
    # connection to prove the guarded ALTER backfills it (mirrors the
    # last_used_at column-migration guard on hosted_api_keys).
    legacy_conn = FakeLibsqlConn()
    legacy_conn.execute(
        """
        CREATE TABLE hosted_api_key_metadata (
            key_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            capability TEXT NOT NULL,
            agent_scope TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            last4 TEXT NOT NULL,
            display TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    legacy_conn.commit()

    provisioned_token, _ = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a"
    )
    exchange = keys.exchange_setup_token(provisioned_token.raw_token)
    console_provisioned, _ = keys.create_control_plane_key(
        tenant_id="tenant-a", project_id="project-a", name="console-key"
    )

    listed = {
        record.key_id: record
        for record in keys.list_control_plane_keys(
            tenant_id="tenant-a", project_id="project-a"
        )
    }
    assert listed[exchange.provisioned.key_id].created_via == "setup"
    assert listed[console_provisioned.key_id].created_via == "console"

    _patch_connect_to_fake(monkeypatch, legacy_conn)
    legacy_keys = HostedApiKeyStore(
        tmp_path,
        control_target=StorageTarget(
            "libsql://fake-control-plane", auth_token="s3cr3t-token"
        ),
    )
    columns = {
        str(row[1])
        for row in legacy_conn.execute(
            "PRAGMA table_info(hosted_api_key_metadata)"
        ).fetchall()
    }
    assert "created_via" in columns
    legacy_provisioned, _ = legacy_keys.create_control_plane_key(
        tenant_id="tenant-a", project_id="project-a", name="legacy-key"
    )
    legacy_listed = legacy_keys.list_control_plane_keys(
        tenant_id="tenant-a", project_id="project-a"
    )
    assert [r.created_via for r in legacy_listed if r.key_id == legacy_provisioned.key_id] == [
        "console"
    ]


def test_setup_token_store_in_memory_branch_parity():
    keys = HostedApiKeyStore()

    provisioned_token, record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", session_id="sess-1"
    )
    assert provisioned_token.raw_token.startswith("vxsetup_")
    assert record.session_id == "sess-1"

    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(f"vxsetup_{record.token_id}_wrong-secret")

    exchange = keys.exchange_setup_token(provisioned_token.raw_token)
    assert exchange.project_id == "project-a"
    assert exchange.session_id == "sess-1"
    auth = keys.authenticate(exchange.provisioned.raw_key)
    assert auth.project_ids == frozenset({"project-a"})
    listed = keys.list_control_plane_keys(tenant_id="tenant-a", project_id="project-a")
    assert [r.created_via for r in listed if r.key_id == exchange.provisioned.key_id] == [
        "setup"
    ]

    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(provisioned_token.raw_token)

    expired_token, _ = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a", ttl_seconds=0
    )
    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(expired_token.raw_token)

    revocable_token, revocable_record = keys.create_setup_token(
        tenant_id="tenant-a", project_id="project-a"
    )
    with pytest.raises(PermissionError):
        keys.revoke_setup_token(
            tenant_id="tenant-b",
            project_id="project-a",
            token_id=revocable_record.token_id,
        )
    keys.revoke_setup_token(
        tenant_id="tenant-a", project_id="project-a", token_id=revocable_record.token_id
    )
    keys.revoke_setup_token(
        tenant_id="tenant-a", project_id="project-a", token_id=revocable_record.token_id
    )
    with pytest.raises(PermissionError, match="Invalid setup token."):
        keys.exchange_setup_token(revocable_token.raw_token)

    # In-memory list branch parity: scoped, and reflects consumed/revoked state.
    listed = {
        r.token_id: r
        for r in keys.list_setup_tokens(tenant_id="tenant-a", project_id="project-a")
    }
    assert listed[record.token_id].consumed_at is not None
    assert listed[revocable_record.token_id].revoked_at is not None
    assert listed[expired_token.token_id].consumed_at is None
    assert keys.list_setup_tokens(tenant_id="tenant-a", project_id="project-b") == []
    active_only = keys.list_setup_tokens(
        tenant_id="tenant-a",
        project_id="project-a",
        include_consumed=False,
        include_revoked=False,
    )
    assert {r.token_id for r in active_only} == {expired_token.token_id}


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


# ---------------------------------------------------------------------------
# Remote (StorageTarget) authentication. Each authenticate() deliberately
# re-reads the revocable record so a CLI process or peer replica's revoke takes
# effect immediately; last-used writes are still throttled independently.


def _patch_connect_counting(monkeypatch, fake_conn: FakeLibsqlConn) -> dict:
    import vexic.hosted_local as hosted_local

    counter = {"n": 0}

    def _fake_connect(target, *, auth_token=None, **kwargs):
        counter["n"] += 1
        return _NonClosingFakeConnHandle(fake_conn)

    monkeypatch.setattr(hosted_local, "connect", _fake_connect)
    return counter


def _remote_key_store(monkeypatch, tmp_path):
    fake_conn = FakeLibsqlConn()
    counter = _patch_connect_counting(monkeypatch, fake_conn)
    _forbid_local_permission_ops(monkeypatch)
    target = StorageTarget("libsql://fake-control-plane", auth_token="s3cr3t-token")
    keys = HostedApiKeyStore(tmp_path, control_target=target)
    provisioned = keys.create_key(
        tenant_id="tenant-a",
        principal_id="agent-a",
        capabilities={MemoryCapability.SEARCH},
        project_ids={"project-a"},
    )
    return keys, provisioned, counter


def test_remote_auth_rechecks_control_plane_on_every_request(monkeypatch, tmp_path):
    keys, provisioned, counter = _remote_key_store(monkeypatch, tmp_path)

    before = counter["n"]
    auth = keys.authenticate(provisioned.raw_key)
    assert auth.key_id == provisioned.key_id
    after_first = counter["n"]
    assert after_first > before  # first auth reached the control plane

    # Every authentication re-reads revocable state. This intentionally costs
    # one remote read per request so another process's revoke is authoritative.
    keys.authenticate(provisioned.raw_key)
    after_second = counter["n"]
    keys.authenticate(provisioned.raw_key)
    assert after_second > after_first
    assert counter["n"] > after_second


def test_remote_auth_observes_revoke_from_another_store(monkeypatch, tmp_path):
    keys, provisioned, _counter = _remote_key_store(monkeypatch, tmp_path)
    target = keys._control_target
    assert isinstance(target, StorageTarget)
    revoker = HostedApiKeyStore(control_target=target)

    keys.authenticate(provisioned.raw_key)
    revoker.revoke_key(provisioned.key_id, revoked_by="operator-cli")
    with pytest.raises(PermissionError):
        keys.authenticate(provisioned.raw_key)


def test_same_store_revoke_denies_authentication(monkeypatch, tmp_path):
    keys, provisioned, _counter = _remote_key_store(monkeypatch, tmp_path)

    keys.authenticate(provisioned.raw_key)
    keys.revoke_key(provisioned.key_id)
    with pytest.raises(PermissionError):
        keys.authenticate(provisioned.raw_key)


def test_local_control_plane_authenticates_without_remote_state(tmp_path):
    keys = HostedApiKeyStore(tmp_path)
    provisioned = keys.create_key(
        tenant_id="tenant-a",
        principal_id="agent-a",
        capabilities={MemoryCapability.SEARCH},
        project_ids={"project-a"},
    )
    assert keys.authenticate(provisioned.raw_key).key_id == provisioned.key_id


# ---------------------------------------------------------------------------
# `_connect_control_db` first-round-trip retry. `libsql.connect()`
# is lazy (no network I/O), so a transient Turso edge fault first surfaces on
# the setup PRAGMA. The acquisition must retry once on a fresh handle for a
# classified retryable fault, and must always close a handle whose PRAGMA
# raised (previously leaked).


_UPSTREAM_502 = ValueError(
    "Hrana: `api error: `status=502 Bad Gateway, "
    'body={"error":"connect to upstream failed"}``'
)


class _PragmaFaultConn:
    """Fake remote connection whose first execute (the PRAGMA) may raise."""

    def __init__(self, fault: BaseException | None) -> None:
        self._fault = fault
        self.closed = False
        self.executed: list[str] = []

    def execute(self, sql, parameters=(), /):
        self.executed.append(sql)
        if self._fault is not None:
            raise self._fault
        return None

    def close(self) -> None:
        self.closed = True


def _control_target() -> StorageTarget:
    return StorageTarget(target="libsql://control.example.turso.io", auth_token="t")


def test_connect_control_db_retries_once_on_transient_pragma_fault(monkeypatch):
    from vexic import hosted_local

    conns = [_PragmaFaultConn(_UPSTREAM_502), _PragmaFaultConn(None)]
    handed_out: list[_PragmaFaultConn] = []

    def fake_connect(target, *args, **kwargs):
        handed_out.append(conns[len(handed_out)])
        return handed_out[-1]

    monkeypatch.setattr(hosted_local, "connect", fake_connect)
    conn = hosted_local._connect_control_db(_control_target())

    assert conn is conns[1]
    assert conns[0].closed is True
    assert conns[1].closed is False
    assert conns[1].executed == ["PRAGMA foreign_keys = ON"]


def test_connect_control_db_does_not_retry_nonretryable_fault(monkeypatch):
    from vexic import hosted_local

    fault = ValueError(
        'Hrana: `api error: `status=404 Not Found, body={"error":"database not found"}``'
    )
    conns = [_PragmaFaultConn(fault), _PragmaFaultConn(None)]
    handed_out: list[_PragmaFaultConn] = []

    def fake_connect(target, *args, **kwargs):
        handed_out.append(conns[len(handed_out)])
        return handed_out[-1]

    monkeypatch.setattr(hosted_local, "connect", fake_connect)
    with pytest.raises(ValueError, match="database not found"):
        hosted_local._connect_control_db(_control_target())

    assert len(handed_out) == 1
    assert conns[0].closed is True


def test_connect_control_db_second_transient_fault_propagates(monkeypatch):
    from vexic import hosted_local

    conns = [_PragmaFaultConn(_UPSTREAM_502), _PragmaFaultConn(_UPSTREAM_502)]
    handed_out: list[_PragmaFaultConn] = []

    def fake_connect(target, *args, **kwargs):
        handed_out.append(conns[len(handed_out)])
        return handed_out[-1]

    monkeypatch.setattr(hosted_local, "connect", fake_connect)
    with pytest.raises(ValueError, match="connect to upstream failed"):
        hosted_local._connect_control_db(_control_target())

    assert len(handed_out) == 2
    assert conns[0].closed is True
    assert conns[1].closed is True
