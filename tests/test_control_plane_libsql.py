import contextlib
import importlib.util
import os

import pytest

from tests.fakes.libsql import FakeLibsqlConn
from vexic.contract import MemoryCapability
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.hosted import HostedUsageEvent
from vexic.storage import StorageTarget
from vexic.storage.connection import connect as storage_connect

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
# Task 10 (COA-273 P3): control-plane connection routed through
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
