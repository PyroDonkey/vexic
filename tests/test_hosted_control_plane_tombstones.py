"""ADR 0028: control-plane projects/tenants get a recoverable soft-delete.

Extends the ADR 0022 tombstone posture to control-plane rows. ``hosted_projects``
had no soft-delete columns at all; ``tenants`` had a dead ``active`` flag. These
tests pin an in-place, non-destructive ``retire`` surface (row survives, hidden
from active listings, audited) so any future removal path retires rather than
hard-deletes.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from vexic.hosted_local import HostedTenantCatalog


class ControlPlaneTombstoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(self.root)
        self.catalog.provision_tenant("tenant-a")
        self.project = self.catalog.create_control_project("tenant-a", name="Alpha")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _raw_count(self, sql: str, *params: object) -> int:
        with closing(sqlite3.connect(self.root / "control-plane.db")) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def test_hosted_projects_and_tenants_have_retire_columns(self) -> None:
        with closing(sqlite3.connect(self.root / "control-plane.db")) as conn:
            project_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(hosted_projects)").fetchall()
            }
            tenant_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(tenants)").fetchall()
            }
        self.assertLessEqual({"retired_at", "retired_by"}, project_cols)
        self.assertLessEqual({"retired_at", "retired_by"}, tenant_cols)

    def test_retire_control_project_hides_from_listing_but_row_survives(self) -> None:
        self.catalog.retire_control_project(
            "tenant-a", self.project.project_id, retired_by="admin"
        )

        active = [p.project_id for p in self.catalog.list_control_projects("tenant-a")]
        self.assertNotIn(self.project.project_id, active)
        # Non-destructive: the row is still physically present.
        self.assertEqual(
            self._raw_count(
                "SELECT COUNT(*) FROM hosted_projects WHERE project_id = ?",
                self.project.project_id,
            ),
            1,
        )

    def test_retire_control_project_records_audit_event(self) -> None:
        self.catalog.retire_control_project("tenant-a", self.project.project_id)

        events = [
            e for e in self.catalog.audit_events("tenant-a")
            if e.operation == "retire_project"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tenant_id, "tenant-a")
        self.assertEqual(events[0].project_id, self.project.project_id)
        self.assertEqual(events[0].status, "ok")

    def test_retire_unknown_project_raises(self) -> None:
        with self.assertRaises(PermissionError):
            self.catalog.retire_control_project("tenant-a", "no-such-project")

    def test_retire_tenant_marks_inactive_but_row_survives_and_audits(self) -> None:
        self.catalog.retire_tenant("tenant-a", retired_by="admin")

        # Row survives, marked inactive with a retirement stamp.
        with closing(sqlite3.connect(self.root / "control-plane.db")) as conn:
            row = conn.execute(
                "SELECT active, retired_at FROM tenants WHERE tenant_id = ?",
                ("tenant-a",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0)
        self.assertIsNotNone(row[1])

        events = [
            e for e in self.catalog.audit_events("tenant-a")
            if e.operation == "retire_tenant"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "ok")

    def test_reprovision_unretires_tenant(self) -> None:
        self.catalog.retire_tenant("tenant-a")
        self.catalog.provision_tenant("tenant-a")

        with closing(sqlite3.connect(self.root / "control-plane.db")) as conn:
            row = conn.execute(
                "SELECT active, retired_at FROM tenants WHERE tenant_id = ?",
                ("tenant-a",),
            ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertIsNone(row[1])

    def test_retire_unknown_tenant_raises(self) -> None:
        with self.assertRaises(PermissionError):
            self.catalog.retire_tenant("no-such-tenant")
