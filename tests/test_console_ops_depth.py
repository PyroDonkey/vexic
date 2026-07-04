from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vexic.hosted import (
    HostedInMemoryRateLimiter,
    HostedJobEvent,
    HostedMemoryService,
    HostedUsageEvent,
)
from vexic.hosted_control_plane_http import create_app as create_control_plane_app
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


class ConsoleOpsDepthHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(root)
        self.keys = HostedApiKeyStore(root)
        self.service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(),
        )
        self.client = TestClient(
            create_control_plane_app(
                self.service,
                control_plane_tokens=("console-secret",),
            )
        )

    def _control_auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer console-secret"}

    def _create_project(self, org: str = "org_123", name: str = "Alpha") -> dict:
        response = self.client.post(
            f"/control/v1/clerk-orgs/{org}/projects",
            headers=self._control_auth(),
            json={"name": name},
        )
        assert response.status_code == 201, response.text
        return response.json()["project"]

    def _create_key(self, org: str, project_id: str, name: str = "key-a") -> dict:
        response = self.client.post(
            f"/control/v1/clerk-orgs/{org}/projects/{project_id}/keys",
            headers=self._control_auth(),
            json={"name": name},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _control_db(self) -> sqlite3.Connection:
        return sqlite3.connect(Path(self.temp_dir.name) / "control-plane.db")


class LastUsedAtTests(ConsoleOpsDepthHarness):
    def test_authenticate_records_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        self.keys.authenticate(created["rawKey"])

        with contextlib.closing(self._control_db()) as conn:
            row = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()
        self.assertIsNotNone(row[0])

    def test_last_used_at_write_is_throttled_to_one_minute(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        self.keys.authenticate(created["rawKey"])
        with contextlib.closing(self._control_db()) as conn:
            first = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()[0]

        self.keys.authenticate(created["rawKey"])
        with contextlib.closing(self._control_db()) as conn:
            second = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()[0]

        self.assertEqual(first, second)

    def test_failed_authentication_does_not_record_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        with self.assertRaises(PermissionError):
            self.keys.authenticate(f"vx_{created['key']['id']}_wrong-secret")

        with contextlib.closing(self._control_db()) as conn:
            row = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
