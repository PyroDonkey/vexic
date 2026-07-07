"""ADR 0028: control-plane destructive ops must record audit events.

Before this change only data-plane memory operations emitted
``hosted_audit_events`` (via ``HostedMemoryService._record_request``); the
control-plane *destructive* mutators (API-key and setup-token revocation,
control-plane-key revocation) left no audit trail. These tests pin those
deletes to the shared ``hosted_audit_events`` ledger, including the
control-plane-scoped ``project_id``/``key_id`` columns. Non-destructive
provisioning/creation is intentionally out of scope (see ADR 0028).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from vexic.contract import MemoryCapability
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


class ControlPlaneAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        # Catalog and key store share one control-plane.db under the same root,
        # so audit rows written by either are readable via catalog.audit_events.
        self.catalog = HostedTenantCatalog(self.root)
        self.keys = HostedApiKeyStore(self.root)
        self.catalog.provision_tenant("tenant-a", project_ids={"project-a"})

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _audit(self, tenant_id: str = "tenant-a"):
        return self.catalog.audit_events(tenant_id)

    def _create_key(self):
        return self.keys.create_key(
            tenant_id="tenant-a",
            principal_id="agent-1",
            capabilities={MemoryCapability.SEARCH},
            project_ids={"project-a"},
        )

    def test_revoke_key_records_audit_event(self) -> None:
        provisioned = self._create_key()

        self.keys.revoke_key(provisioned.key_id, revoked_by="admin")

        events = [e for e in self._audit() if e.operation == "revoke_key"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tenant_id, "tenant-a")
        self.assertEqual(events[0].key_id, provisioned.key_id)
        self.assertEqual(events[0].status, "ok")

    def test_repeated_revoke_audits_once(self) -> None:
        # Revocation is idempotent; a no-op second revoke must not forge a
        # second destructive audit row.
        provisioned = self._create_key()
        self.keys.revoke_key(provisioned.key_id)
        self.keys.revoke_key(provisioned.key_id)

        events = [e for e in self._audit() if e.operation == "revoke_key"]
        self.assertEqual(len(events), 1)

    def test_audit_event_never_contains_raw_credential(self) -> None:
        provisioned = self._create_key()
        self.keys.revoke_key(provisioned.key_id)

        blob = repr(self._audit())
        self.assertNotIn(provisioned.raw_key, blob)

    def test_revoke_setup_token_records_audit_event(self) -> None:
        provisioned, _ = self.keys.create_setup_token(
            tenant_id="tenant-a", project_id="project-a"
        )

        self.keys.revoke_setup_token(
            tenant_id="tenant-a", project_id="project-a", token_id=provisioned.token_id
        )

        events = [e for e in self._audit() if e.operation == "revoke_setup_token"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tenant_id, "tenant-a")
        self.assertEqual(events[0].project_id, "project-a")
        self.assertEqual(events[0].status, "ok")

    def test_hosted_audit_events_has_project_and_key_columns(self) -> None:
        db_path = self.root / "control-plane.db"
        with closing(sqlite3.connect(db_path)) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(hosted_audit_events)").fetchall()
            }
        self.assertIn("project_id", cols)
        self.assertIn("key_id", cols)


class ControlPlaneAuditMigrationTests(unittest.TestCase):
    """The project_id/key_id columns must be added by ALTER on a pre-existing
    control-plane.db that predates ADR 0028 (idempotent, non-destructive)."""

    def test_init_alters_legacy_audit_table_and_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "control-plane.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE hosted_audit_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        operation TEXT NOT NULL,
                        tenant_id TEXT,
                        principal_id TEXT,
                        status TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        error_type TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO hosted_audit_events "
                    "(operation, tenant_id, principal_id, status, recorded_at) "
                    "VALUES ('legacy', 't', 'p', 'ok', '2020-01-01T00:00:00Z')"
                )
                conn.commit()

            catalog = HostedTenantCatalog(root)  # runs the migration

            with closing(sqlite3.connect(db_path)) as conn:
                cols = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(hosted_audit_events)"
                    ).fetchall()
                }
                legacy = conn.execute(
                    "SELECT operation FROM hosted_audit_events WHERE operation = 'legacy'"
                ).fetchall()
            self.assertIn("project_id", cols)
            self.assertIn("key_id", cols)
            self.assertEqual(len(legacy), 1)  # non-destructive
            # Reconstructing again must not error (idempotent).
            HostedTenantCatalog(root)
