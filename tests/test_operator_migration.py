import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.embeddings import EMBEDDING_DIM
from vexic.models import FactCandidate
from vexic.storage import (
    SourceTranscriptInput,
    commit_deep_cycle,
    commit_dream_cycle,
    ingest_source_messages,
    init_db,
    save_messages,
    single_message_adapter,
)
from vexic.storage.promotion import PromotionDecision


def _unit_vector() -> list[float]:
    return [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def _scope(*, tenant_id: str = "tenant-a", agent_id: str = "agent-a") -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant_id,
        project_id="project-a",
        session_id="session-a",
        agent_id=agent_id,
        principal=Principal(
            principal_id="operator-a",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities={MemoryCapability.SEARCH},
    )


class OperatorMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source_db = self.root / "source.db"
        self.target_db = self.root / "target.db"
        self.artifact = self.root / "canonical-migration.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _source_message(self) -> SourceTranscriptInput:
        message_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="cedar migration transcript")])
        ).decode()
        return SourceTranscriptInput(
            source_host="local-vexic",
            source_session_id="session-a",
            source_message_id="message-a",
            message_json=message_json,
        )

    def _seed_source(self) -> None:
        init_db(str(self.source_db))
        result = ingest_source_messages(
            str(self.source_db),
            [self._source_message()],
            session_id="session-a",
            agent_id="agent-a",
        )
        message_id = result[0].message_id
        assert message_id is not None
        commit_dream_cycle(
            str(self.source_db),
            [
                FactCandidate(
                    fact_text="cedar migration durable fact",
                    subject="Ryan",
                    category="fact",
                    importance=7,
                    confidence=0.9,
                    source_message_ids=[message_id],
                )
            ],
            candidate_embeddings=[_unit_vector()],
            agent_id="agent-a",
            status="ok",
            started_at="2026-06-25T00:00:00+00:00",
            finished_at="2026-06-25T00:00:01+00:00",
            messages_processed=1,
            last_processed_message_id=message_id,
        )
        commit_deep_cycle(
            str(self.source_db),
            [PromotionDecision(1, _unit_vector())],
            agent_id="agent-a",
            started_at="2026-06-25T00:01:00+00:00",
            finished_at="2026-06-25T00:01:01+00:00",
        )

    async def test_canonical_migration_imports_source_memory_into_replacement_db(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )
        from vexic.service import LocalMemoryService

        self._seed_source()

        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        service = LocalMemoryService(
            db_path=str(self.target_db),
            tenant_id="tenant-a",
            embed=lambda texts: [_unit_vector() for _ in texts],
        )
        transcript = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="cedar")
        )
        long_term = await service.search_long_term(
            SearchLongTermRequest(scope=_scope(), query="cedar migration")
        )
        duplicate_ingest = ingest_source_messages(
            str(self.target_db),
            [self._source_message()],
            session_id="session-a",
            agent_id="agent-a",
        )

        self.assertEqual([hit.body for hit in transcript.hits], ["User: cedar migration transcript"])
        self.assertEqual([fact.fact_text for fact in long_term.facts], ["cedar migration durable fact"])
        self.assertEqual(duplicate_ingest[0].status, "skipped")

    async def test_canonical_migration_import_uses_schema_column_order_for_values(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )
        from vexic.service import LocalMemoryService

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        payload = json.loads(self.artifact.read_text())
        payload["tables"]["messages"] = [
            dict(reversed(list(row.items()))) for row in payload["tables"]["messages"]
        ]
        self.artifact.write_text(json.dumps(payload))

        import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        service = LocalMemoryService(db_path=str(self.target_db), tenant_id="tenant-a")
        transcript = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="cedar")
        )

        self.assertEqual([hit.body for hit in transcript.hits], ["User: cedar migration transcript"])

    def test_canonical_migration_export_does_not_create_vector_projection_tables(self) -> None:
        from vexic.migration import export_canonical_migration

        init_db(str(self.source_db))
        before = self._table_names(self.source_db)

        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )

        self.assertEqual(self._table_names(self.source_db), before)

    def test_canonical_migration_export_fails_closed_on_forbidden_values(self) -> None:
        from vexic.migration import export_canonical_migration

        self._seed_source()

        with self.assertRaisesRegex(ValueError, "forbidden"):
            export_canonical_migration(
                str(self.source_db),
                self.artifact,
                tenant_id="tenant-a",
                project_id="project-a",
                forbidden_secret_values=("cedar migration",),
            )

        self.assertFalse(self.artifact.exists())

    def test_canonical_migration_overwrite_removes_stale_artifact_on_redaction_failure(self) -> None:
        from vexic.migration import export_canonical_migration

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )

        with self.assertRaisesRegex(ValueError, "forbidden"):
            export_canonical_migration(
                str(self.source_db),
                self.artifact,
                tenant_id="tenant-a",
                project_id="project-a",
                forbidden_secret_values=("cedar migration",),
                overwrite=True,
            )

        self.assertFalse(self.artifact.exists())

    def test_canonical_migration_import_is_idempotent_for_same_artifact(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )

        first = import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )
        second = import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        self.assertGreater(first.rows_imported, 0)
        self.assertEqual(second.rows_imported, 0)

    def test_canonical_migration_import_rejects_extra_target_canonical_rows(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )
        save_messages(
            str(self.target_db),
            [ModelRequest(parts=[UserPromptPart(content="cedar target contaminant")])],
            session_id="session-a",
            agent_id="agent-a",
        )

        with self.assertRaisesRegex(ValueError, "outside the artifact"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

    def test_canonical_migration_import_rejects_extra_artifact_columns(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        payload = json.loads(self.artifact.read_text())
        payload["tables"]["messages"][0]['id" FROM messages WHERE id = ? --'] = 1
        self.artifact.write_text(json.dumps(payload))

        with self.assertRaisesRegex(ValueError, "columns"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

    def test_canonical_migration_export_fails_closed_on_host_owned_extension_tables(self) -> None:
        from vexic.migration import export_canonical_migration

        self._seed_source()
        with closing(sqlite3.connect(self.source_db)) as conn:
            conn.execute("CREATE TABLE background_tool_audit (id INTEGER PRIMARY KEY)")
            conn.commit()

        with self.assertRaisesRegex(ValueError, "host-owned extension table"):
            export_canonical_migration(
                str(self.source_db),
                self.artifact,
                tenant_id="tenant-a",
                project_id="project-a",
            )

        self.assertFalse(self.artifact.exists())

    def test_canonical_migration_import_fails_closed_on_target_host_owned_tables(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        with closing(sqlite3.connect(self.target_db)) as conn:
            conn.execute("CREATE TABLE background_tool_audit (id INTEGER PRIMARY KEY)")
            conn.commit()

        with self.assertRaisesRegex(ValueError, "host-owned extension table"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

    def test_hosted_catalog_repoints_to_imported_replacement_database(self) -> None:
        from vexic.hosted_local import HostedTenantCatalog
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        catalog = HostedTenantCatalog(self.root)
        old_tenant = catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        replacement_db = self.root / "replacement.db"
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        import_canonical_migration(
            self.artifact,
            str(replacement_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        self.assertEqual(catalog.get_tenant("tenant-a").db_path, old_tenant.db_path)
        activated = catalog.activate_replacement_database("tenant-a", replacement_db)

        self.assertEqual(activated.db_path, replacement_db)
        self.assertEqual(catalog.get_tenant("tenant-a").db_path, replacement_db)
        self.assertEqual(activated.project_ids, frozenset({"project-a"}))
        with closing(sqlite3.connect(self.root / "control-plane.db")) as conn:
            rows = conn.execute(
                "SELECT db_filename, active FROM tenants WHERE tenant_id = ?",
                ("tenant-a",),
            ).fetchall()
        self.assertEqual(rows, [(replacement_db.name, 1)])

    def test_hosted_catalog_rejects_replacement_imported_for_another_tenant(self) -> None:
        from vexic.hosted_local import HostedTenantCatalog
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        catalog = HostedTenantCatalog(self.root)
        old_tenant = catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        replacement_db = self.root / "replacement.db"
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-b",
            project_id="project-b",
        )
        import_canonical_migration(
            self.artifact,
            str(replacement_db),
            tenant_id="tenant-b",
            project_id="project-b",
        )

        with self.assertRaisesRegex(PermissionError, "tenant"):
            catalog.activate_replacement_database("tenant-a", replacement_db)

        self.assertEqual(catalog.get_tenant("tenant-a").db_path, old_tenant.db_path)

    def test_hosted_catalog_rejects_project_scoped_replacement_without_catalog_project(self) -> None:
        from vexic.hosted_local import HostedTenantCatalog
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        catalog = HostedTenantCatalog(self.root)
        old_tenant = catalog.provision_tenant("tenant-a")
        replacement_db = self.root / "replacement.db"
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        import_canonical_migration(
            self.artifact,
            str(replacement_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        with self.assertRaisesRegex(PermissionError, "project"):
            catalog.activate_replacement_database("tenant-a", replacement_db)

        self.assertEqual(catalog.get_tenant("tenant-a").db_path, old_tenant.db_path)

    def test_hosted_catalog_rejects_invalid_replacement_database(self) -> None:
        from vexic.hosted_local import HostedTenantCatalog

        catalog = HostedTenantCatalog(self.root)
        old_tenant = catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        replacement_db = self.root / "replacement.db"
        replacement_db.write_bytes(b"not sqlite")

        with self.assertRaisesRegex(PermissionError, "migration metadata"):
            catalog.activate_replacement_database("tenant-a", replacement_db)

        self.assertEqual(catalog.get_tenant("tenant-a").db_path, old_tenant.db_path)

    def test_canonical_migration_import_rejects_artifact_scope_spoofing(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        payload = json.loads(self.artifact.read_text())
        payload["scope"]["tenant_id"] = "tenant-b"
        self.artifact.write_text(json.dumps(payload))

        with self.assertRaisesRegex(PermissionError, "scope"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

        self.assertFalse(self.target_db.exists())

    def test_canonical_migration_import_rejects_schema_version_mismatch(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        payload = json.loads(self.artifact.read_text())
        payload["artifact_version"] = "vexic.canonical-migration.v999"
        self.artifact.write_text(json.dumps(payload))

        with self.assertRaisesRegex(ValueError, "version"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

        self.assertFalse(self.target_db.exists())

    def test_canonical_migration_import_rejects_malformed_artifact_before_db_creation(self) -> None:
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        payload = json.loads(self.artifact.read_text())
        payload["tables"].pop("messages")
        self.artifact.write_text(json.dumps(payload))

        with self.assertRaisesRegex(ValueError, "artifact"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

        self.assertFalse(self.target_db.exists())

    def test_failed_import_leaves_catalog_on_existing_database(self) -> None:
        from vexic.hosted_local import HostedTenantCatalog
        from vexic.migration import (
            export_canonical_migration,
            import_canonical_migration,
        )

        self._seed_source()
        catalog = HostedTenantCatalog(self.root)
        old_tenant = catalog.provision_tenant("tenant-a", project_ids={"project-a"})
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )

        with self.assertRaisesRegex(ValueError, "forbidden"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
                forbidden_secret_values=("cedar migration",),
            )

        self.assertEqual(catalog.get_tenant("tenant-a").db_path, old_tenant.db_path)
        self.assertFalse(self.target_db.exists())

    def _export_artifact_payload(self) -> dict:
        from vexic.migration import export_canonical_migration

        self._seed_source()
        export_canonical_migration(
            str(self.source_db),
            self.artifact,
            tenant_id="tenant-a",
            project_id="project-a",
        )
        return json.loads(self.artifact.read_text())

    def _strip_additive_columns(self, payload: dict) -> dict:
        # Simulates a v1 artifact exported before the additive `_ensure_column`
        # migrations that added dream_runs.candidates_dropped (NOT NULL
        # DEFAULT 0) and long_term_memory.occurred_at (nullable, no default).
        for row in payload["tables"]["dream_runs"]:
            del row["candidates_dropped"]
        for row in payload["tables"]["long_term_memory"]:
            del row["occurred_at"]
        return payload

    def test_import_fills_missing_additive_columns_from_schema_defaults(self) -> None:
        from vexic.migration import import_canonical_migration

        payload = self._strip_additive_columns(self._export_artifact_payload())
        self.assertTrue(payload["tables"]["dream_runs"])
        self.assertTrue(payload["tables"]["long_term_memory"])
        self.artifact.write_text(json.dumps(payload))

        report = import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        self.assertGreater(report.rows_imported, 0)
        with closing(sqlite3.connect(self.target_db)) as conn:
            dropped = conn.execute("SELECT candidates_dropped FROM dream_runs").fetchall()
            occurred = conn.execute("SELECT occurred_at FROM long_term_memory").fetchall()
        self.assertEqual({row[0] for row in dropped}, {0})
        self.assertEqual({row[0] for row in occurred}, {None})

    def test_import_backfills_mentioned_at_for_pre_adr0037_artifacts(self) -> None:
        # codex audit F3: import init_db-and-memoizes the target before rows
        # land, so the ensure backfill cannot heal pre-ADR-0037 rows in this
        # process. The import path must run the targeted backfill explicitly
        # after inserting rows, or imported undated events stay sunk in
        # Tier 2 until a process restart.
        from vexic.migration import import_canonical_migration

        payload = self._export_artifact_payload()
        for row in payload["tables"]["memory_candidates"]:
            del row["mentioned_at"]
        for row in payload["tables"]["long_term_memory"]:
            del row["mentioned_at"]
        self.artifact.write_text(json.dumps(payload))

        report = import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        self.assertGreater(report.rows_imported, 0)
        with closing(sqlite3.connect(self.target_db)) as conn:
            expected = conn.execute(
                "SELECT DATE(MIN(timestamp)) FROM messages"
            ).fetchone()[0]
            candidate_dates = conn.execute(
                "SELECT mentioned_at FROM memory_candidates"
            ).fetchall()
            fact_dates = conn.execute(
                "SELECT mentioned_at FROM long_term_memory"
            ).fetchall()
        self.assertIsNotNone(expected)
        self.assertEqual({row[0] for row in candidate_dates}, {expected})
        self.assertEqual({row[0] for row in fact_dates}, {expected})

    def test_import_of_column_stripped_artifact_is_idempotent(self) -> None:
        from vexic.migration import import_canonical_migration

        payload = self._strip_additive_columns(self._export_artifact_payload())
        self.artifact.write_text(json.dumps(payload))

        first = import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )
        second = import_canonical_migration(
            self.artifact,
            str(self.target_db),
            tenant_id="tenant-a",
            project_id="project-a",
        )

        self.assertGreater(first.rows_imported, 0)
        self.assertEqual(second.rows_imported, 0)

    def test_import_fails_closed_on_missing_not_null_column_without_default(self) -> None:
        from vexic.migration import import_canonical_migration

        payload = self._export_artifact_payload()
        for row in payload["tables"]["messages"]:
            del row["message_json"]
        self.artifact.write_text(json.dumps(payload))

        with self.assertRaisesRegex(ValueError, r"messages.*message_json"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

    def test_import_rejects_mixed_column_sets_within_table(self) -> None:
        from vexic.migration import import_canonical_migration

        payload = self._export_artifact_payload()
        extra_row = dict(payload["tables"]["messages"][0])
        extra_row["id"] = 999
        del extra_row["agent_id"]
        payload["tables"]["messages"].append(extra_row)
        self.artifact.write_text(json.dumps(payload))

        with self.assertRaisesRegex(ValueError, r"messages.*mixed column sets"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

    def test_failed_import_rolls_back_all_canonical_rows(self) -> None:
        from vexic.migration import import_canonical_migration

        payload = self._export_artifact_payload()
        # messages imports cleanly; long_term_memory (a later canonical table)
        # then fails validation, which must roll back the whole import.
        self.assertTrue(payload["tables"]["long_term_memory"])
        payload["tables"]["long_term_memory"][0]["bogus_column"] = 1
        self.artifact.write_text(json.dumps(payload))

        with self.assertRaisesRegex(ValueError, "columns"):
            import_canonical_migration(
                self.artifact,
                str(self.target_db),
                tenant_id="tenant-a",
                project_id="project-a",
            )

        with closing(sqlite3.connect(self.target_db)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 0)

    def _table_names(self, db_path: Path) -> set[str]:
        with closing(sqlite3.connect(db_path)) as conn:
            return {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                        AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
