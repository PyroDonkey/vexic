import pytest
from adapters.turso_adapter import control_plane_target, reconcile_tenant_databases
from vexic.storage.connection import StorageTarget

def test_reads_env_into_redacted_target():
    env = {"TURSO_DATABASE_URL": "libsql://db.turso.io", "TURSO_AUTH_TOKEN": "JWT"}
    t = control_plane_target(env)
    assert isinstance(t, StorageTarget) and t.target == "libsql://db.turso.io"
    assert "JWT" not in repr(t)

def test_missing_env_raises():
    with pytest.raises(ValueError):
        control_plane_target({})


def test_reconcile_flags_platform_db_with_no_referencing_tenant_as_orphan():
    report = reconcile_tenant_databases(
        platform_db_targets=["libsql://orphan.turso.io"],
        catalog_targets={},
    )
    assert report.orphan_databases == frozenset({"libsql://orphan.turso.io"})
    assert report.matched == {}
    assert report.dangling_targets == {}


def test_reconcile_matches_tenant_target_present_on_platform():
    report = reconcile_tenant_databases(
        platform_db_targets=["libsql://tenant-a.turso.io"],
        catalog_targets={"tenant-a": "libsql://tenant-a.turso.io"},
    )
    assert report.matched == {"tenant-a": "libsql://tenant-a.turso.io"}
    assert report.orphan_databases == frozenset()
    assert report.dangling_targets == {}


def test_reconcile_flags_tenant_target_absent_from_platform_as_dangling():
    report = reconcile_tenant_databases(
        platform_db_targets=[],
        catalog_targets={"tenant-b": "libsql://missing.turso.io"},
    )
    assert report.dangling_targets == {"tenant-b": "libsql://missing.turso.io"}
    assert report.matched == {}
    assert report.orphan_databases == frozenset()


def test_reconcile_ignores_tenant_with_none_customer_target():
    report = reconcile_tenant_databases(
        platform_db_targets=["libsql://some-db.turso.io"],
        catalog_targets={"tenant-local": None},
    )
    assert report.matched == {}
    assert report.dangling_targets == {}
    assert report.orphan_databases == frozenset({"libsql://some-db.turso.io"})
