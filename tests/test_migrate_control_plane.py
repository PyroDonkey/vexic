"""Control-plane catalog migration between storage targets. Drilled
local file -> local file (the real cutover swaps the target for a Turso
StorageTarget, exercised live)."""

from vexic.contract import MemoryCapability
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.migrate_control_plane import migrate_control_plane


def _seed_source(root):
    """Populate a local control-plane.db with tenants, keys, and telemetry."""
    catalog = HostedTenantCatalog(root)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    catalog.provision_tenant("tenant-b", project_ids={"project-b"})
    keys = HostedApiKeyStore(root)
    keys.create_key(
        tenant_id="tenant-a",
        principal_id="agent-a",
        capabilities={MemoryCapability.SEARCH},
        project_ids={"project-a"},
    )
    return root / "control-plane.db"


def test_migrate_copies_tenants_and_keys(tmp_path):
    source_db = _seed_source(tmp_path / "src")
    target_db = tmp_path / "dst" / "control-plane.db"

    results = migrate_control_plane(source_db, target_db)

    by_table = {r.table: r for r in results}
    assert by_table["tenants"].source_rows == 2
    assert by_table["tenants"].target_rows_after == 2
    assert by_table["hosted_api_keys"].source_rows == 1
    assert by_table["hosted_api_keys"].target_rows_after == 1
    assert all(r.complete for r in results)

    # The migrated catalog resolves the same tenants.
    target_catalog = HostedTenantCatalog(target_db.parent, control_target=None)
    assert set(target_catalog.list_active_tenant_ids()) == {"tenant-a", "tenant-b"}


def test_migrate_preserves_key_authentication(tmp_path):
    root = tmp_path / "src"
    catalog = HostedTenantCatalog(root)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    keys = HostedApiKeyStore(root)
    provisioned = keys.create_key(
        tenant_id="tenant-a",
        principal_id="agent-a",
        capabilities={MemoryCapability.SEARCH},
        project_ids={"project-a"},
    )
    source_db = root / "control-plane.db"
    target_db = tmp_path / "dst" / "control-plane.db"

    migrate_control_plane(source_db, target_db)

    # The same raw key authenticates against the migrated store.
    migrated_keys = HostedApiKeyStore(target_db.parent)
    auth = migrated_keys.authenticate(provisioned.raw_key)
    assert auth.tenant_id == "tenant-a"
    assert auth.key_id == provisioned.key_id


def test_migrate_is_idempotent(tmp_path):
    source_db = _seed_source(tmp_path / "src")
    target_db = tmp_path / "dst" / "control-plane.db"

    first = migrate_control_plane(source_db, target_db)
    second = migrate_control_plane(source_db, target_db)

    first_tenants = next(r for r in first if r.table == "tenants")
    second_tenants = next(r for r in second if r.table == "tenants")
    assert first_tenants.target_rows_after == second_tenants.target_rows_after == 2
    assert all(r.complete for r in second)


def test_migrate_reports_every_source_table(tmp_path):
    source_db = _seed_source(tmp_path / "src")
    target_db = tmp_path / "dst" / "control-plane.db"

    results = migrate_control_plane(source_db, target_db)

    tables = {r.table for r in results}
    # Identity/routing tables that must survive the cutover.
    assert {"tenants", "tenant_projects", "hosted_api_keys"} <= tables
