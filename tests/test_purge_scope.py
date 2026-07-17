import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    AppendTranscriptRequest,
    DeleteScopeRequest,
    DreamPhase,
    IngestSourceTranscriptRequest,
    MemoryCapability,
    MemoryScope,
    MemoryScopeSelector,
    Principal,
    PrincipalType,
    PurgeScopeRequest,
    RedactionContext,
    RunDreamPhaseRequest,
    SourceTranscriptMessage,
    TrustBoundary,
)
from vexic.embeddings import EMBEDDING_DIM
from vexic.models import FactCandidate
from vexic.service import LocalMemoryService
from vexic.storage import (
    commit_deep_cycle,
    commit_dream_cycle,
    init_db,
    save_messages,
    single_message_adapter,
)
from vexic.storage.promotion import PromotionDecision
from vexic.storage.schema import _load_vec_extension


PURGED_TERM = "cedarpurgedsecret"
SURVIVOR_TERM = "cedarsurvivordetail"


def _basis_vector(axis: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    return vector


def _scope(capabilities: set[MemoryCapability]) -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-a",
        session_id="session-1",
        principal=Principal(
            principal_id="test-operator",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities=capabilities,
    )


def _selector(session_id: str | None) -> MemoryScopeSelector:
    return MemoryScopeSelector(tenant_id="tenant-a", session_id=session_id)


class PurgeScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        self.service = LocalMemoryService(db_path=self.db_path, tenant_id="tenant-a")

        self.purged_message_id = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content=f"{PURGED_TERM} in session one")])],
            session_id="session-1",
        )[0]
        self.survivor_message_id = save_messages(
            self.db_path,
            [ModelRequest(parts=[UserPromptPart(content=f"{SURVIVOR_TERM} in session two")])],
            session_id="session-2",
        )[0]

        commit_dream_cycle(
            self.db_path,
            [
                FactCandidate(
                    fact_text=f"fact about {PURGED_TERM}",
                    subject="Ryan",
                    category="fact",
                    importance=5,
                    confidence=0.8,
                    source_message_ids=[self.purged_message_id],
                ),
                FactCandidate(
                    fact_text=f"fact about {SURVIVOR_TERM}",
                    subject="Ryan",
                    category="fact",
                    importance=5,
                    confidence=0.8,
                    source_message_ids=[self.survivor_message_id],
                ),
            ],
            candidate_embeddings=[_basis_vector(0), _basis_vector(1)],
            agent_id=None,
            status="ok",
            started_at="2026-06-01T00:00:00+00:00",
            finished_at="2026-06-01T00:00:01+00:00",
            messages_processed=2,
            last_processed_message_id=self.survivor_message_id,
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id, fact_text FROM memory_candidates ORDER BY id"
            ).fetchall()
        self.purged_candidate_id = next(
            row[0] for row in rows if PURGED_TERM in row[1]
        )
        self.survivor_candidate_id = next(
            row[0] for row in rows if SURVIVOR_TERM in row[1]
        )
        commit_deep_cycle(
            self.db_path,
            [
                PromotionDecision(self.purged_candidate_id, _basis_vector(0)),
                PromotionDecision(self.survivor_candidate_id, _basis_vector(1)),
            ],
            agent_id=None,
            started_at="2026-06-01T00:01:00+00:00",
            finished_at="2026-06-01T00:01:01+00:00",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            fact_rows = conn.execute(
                "SELECT id, fact_text FROM long_term_memory ORDER BY id"
            ).fetchall()
            self.purged_fact_id = next(
                row[0] for row in fact_rows if PURGED_TERM in row[1]
            )
            self.survivor_fact_id = next(
                row[0] for row in fact_rows if SURVIVOR_TERM in row[1]
            )
            with conn:
                for fact_id, session_id, term in (
                    (self.purged_fact_id, "session-1", PURGED_TERM),
                    (self.survivor_fact_id, "session-2", SURVIVOR_TERM),
                ):
                    conn.execute(
                        """
                        INSERT INTO retrieval_events (fact_id, session_id, query, rewritten_query)
                        VALUES (?, ?, ?, ?)
                        """,
                        (fact_id, session_id, f"query {term}", f"rewritten {term}"),
                    )
                for candidate_id, session_id, term in (
                    (self.purged_candidate_id, "session-1", PURGED_TERM),
                    (self.survivor_candidate_id, "session-2", SURVIVOR_TERM),
                ):
                    conn.execute(
                        """
                        INSERT INTO candidate_retrieval_events (candidate_id, session_id, query)
                        VALUES (?, ?, ?)
                        """,
                        (candidate_id, session_id, f"query {term}"),
                    )
                for session_id, term in (
                    ("session-1", PURGED_TERM),
                    ("session-2", SURVIVOR_TERM),
                ):
                    conn.execute(
                        """
                        INSERT INTO session_summaries
                            (session_id, kind, first_message_id, last_message_id,
                             summary_text, token_estimate)
                        VALUES (?, 'leaf', 1, 1, ?, 10)
                        """,
                        (session_id, f"summary mentioning {term}"),
                    )
                # A dedup event for a DISCARDED duplicate: it references the
                # purged message ids but never became a candidate row itself.
                conn.execute(
                    """
                    INSERT INTO memory_dedup_events
                        (candidate_id, decision, incoming_fact_text, incoming_source_message_ids)
                    VALUES (?, 'merge', ?, ?)
                    """,
                    (
                        self.survivor_candidate_id,
                        f"discarded duplicate about {PURGED_TERM}",
                        f"[{self.purged_message_id}]",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO memory_dedup_events
                        (candidate_id, decision, incoming_fact_text, incoming_source_message_ids)
                    VALUES (?, 'insert', ?, ?)
                    """,
                    (
                        self.survivor_candidate_id,
                        f"kept insert about {SURVIVOR_TERM}",
                        f"[{self.survivor_message_id}]",
                    ),
                )
                for candidate_id, term in (
                    (self.purged_candidate_id, PURGED_TERM),
                    (self.survivor_candidate_id, SURVIVOR_TERM),
                ):
                    conn.execute(
                        """
                        INSERT INTO promotion_labels (candidate_id, fact_text, label)
                        VALUES (?, ?, 'promote')
                        """,
                        (candidate_id, f"label snapshot {term}"),
                    )
                for message_id, source_session in (
                    (self.purged_message_id, "claude-session-1"),
                    (self.survivor_message_id, "claude-session-2"),
                ):
                    conn.execute(
                        """
                        INSERT INTO source_transcript_ledger
                            (source_host, source_session_id, source_message_id, message_id)
                        VALUES ('claude-code', ?, ?, ?)
                        """,
                        (source_session, f"uuid-{message_id}", message_id),
                    )
                conn.execute(
                    "UPDATE dream_runs SET error_detail = ? WHERE error_detail IS NULL",
                    (f"legacy traceback mentioning {PURGED_TERM}",),
                )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _tombstone(self, session_id: str | None) -> None:
        await self.service.delete_scope(
            DeleteScopeRequest(
                scope=_scope({MemoryCapability.ADMIN_LIFECYCLE}),
                target_scope=_selector(session_id),
                reason="user requested erasure",
                redaction=RedactionContext(forbidden_values=()),
            )
        )

    async def _purge(
        self,
        session_id: str | None,
        *,
        dry_run: bool = False,
        confirm_whole_scope: bool = False,
    ):
        return await self.service.purge_scope(
            PurgeScopeRequest(
                scope=_scope({MemoryCapability.ADMIN_LIFECYCLE}),
                target_scope=_selector(session_id),
                reason="user requested erasure",
                redaction=RedactionContext(forbidden_values=()),
                dry_run=dry_run,
                confirm_whole_scope=confirm_whole_scope,
            )
        )

    def _count(self, conn: sqlite3.Connection, sql: str, *params: object) -> int:
        return conn.execute(sql, params).fetchone()[0]

    async def test_partial_session_purge_erases_scope_and_spares_survivors(self) -> None:
        await self._tombstone("session-1")
        result = await self._purge("session-1")

        self.assertFalse(result.dry_run)
        self.assertIsNotNone(result.purged_at)
        self.assertGreaterEqual(result.purged["messages"], 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            _load_vec_extension(conn)
            remaining_messages = conn.execute(
                "SELECT id, session_id FROM messages"
            ).fetchall()
            self.assertEqual(
                remaining_messages, [(self.survivor_message_id, "session-2")]
            )
            for fts_query in (
                f"SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH '{PURGED_TERM}'",
                f"SELECT COUNT(*) FROM memory_candidates_fts WHERE memory_candidates_fts MATCH '{PURGED_TERM}'",
                f"SELECT COUNT(*) FROM long_term_memory_fts WHERE long_term_memory_fts MATCH '{PURGED_TERM}'",
            ):
                self.assertEqual(self._count(conn, fts_query), 0, fts_query)
            self.assertEqual(
                self._count(
                    conn,
                    f"SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH '{SURVIVOR_TERM}'",
                ),
                1,
            )
            self.assertEqual(
                self._count(conn, "SELECT COUNT(*) FROM memory_candidates"), 1
            )
            self.assertEqual(
                self._count(conn, "SELECT COUNT(*) FROM long_term_memory"), 1
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM memory_candidate_embeddings WHERE candidate_id = ?",
                    self.purged_candidate_id,
                ),
                0,
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM long_term_memory_embeddings WHERE fact_id = ?",
                    self.purged_fact_id,
                ),
                0,
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM memory_candidate_embeddings WHERE candidate_id = ?",
                    self.survivor_candidate_id,
                ),
                1,
            )
            # Discarded-duplicate dedup rows referencing purged messages go too.
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM memory_dedup_events WHERE incoming_fact_text LIKE ?",
                    f"%{PURGED_TERM}%",
                ),
                0,
            )
            self.assertEqual(
                self._count(conn, "SELECT COUNT(*) FROM memory_dedup_events"), 1
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM promotion_labels WHERE candidate_id = ?",
                    self.purged_candidate_id,
                ),
                0,
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM retrieval_events WHERE session_id = 'session-1'",
                ),
                0,
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM candidate_retrieval_events WHERE session_id = 'session-1'",
                ),
                0,
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM session_summaries WHERE session_id = 'session-1'",
                ),
                0,
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM source_transcript_ledger WHERE message_id = ?",
                    self.purged_message_id,
                ),
                0,
            )
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM source_transcript_ledger WHERE message_id = ?",
                    self.survivor_message_id,
                ),
                1,
            )
            # dream_runs rows survive (watermarks) but content is scrubbed.
            self.assertEqual(
                self._count(
                    conn,
                    "SELECT COUNT(*) FROM dream_runs WHERE error_detail LIKE ?",
                    f"%{PURGED_TERM}%",
                ),
                0,
            )
            self.assertGreaterEqual(
                self._count(conn, "SELECT COUNT(*) FROM dream_runs"), 1
            )
            deferred, purged_at = conn.execute(
                "SELECT physical_purge_deferred, purged_at FROM scope_tombstones"
            ).fetchone()
            self.assertEqual(deferred, 0)
            self.assertIsNotNone(purged_at)

    async def test_multi_session_fact_is_deleted_on_any_source_overlap(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE long_term_memory SET source_message_ids = ? WHERE id = ?",
                    (
                        f"[{self.purged_message_id}, {self.survivor_message_id}]",
                        self.survivor_fact_id,
                    ),
                )
        await self._tombstone("session-1")
        await self._purge("session-1")

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                self._count(conn, "SELECT COUNT(*) FROM long_term_memory"), 0
            )

    async def test_purge_requires_existing_tombstone(self) -> None:
        with self.assertRaisesRegex(ValueError, "tombstone"):
            await self._purge("session-1")

    async def test_whole_scope_purge_rejected_without_confirmation(self) -> None:
        # A null-session target purges every session for the agent scope in one
        # call (ADR 0028 mass-delete surface); it must not proceed accidentally.
        await self._tombstone(None)
        with self.assertRaisesRegex(ValueError, "confirm_whole_scope"):
            await self._purge(None)

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 2)

    async def test_dry_run_whole_scope_still_requires_confirmation(self) -> None:
        # The confirmation gate precedes dry_run: you cannot even preview a
        # whole-scope purge without opting in.
        await self._tombstone(None)
        with self.assertRaisesRegex(ValueError, "confirm_whole_scope"):
            await self._purge(None, dry_run=True)

    async def test_whole_scope_purge_allowed_with_confirmation(self) -> None:
        await self._tombstone(None)
        result = await self._purge(None, confirm_whole_scope=True)

        self.assertFalse(result.dry_run)
        self.assertIsNotNone(result.purged_at)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 0)

    async def test_session_scoped_purge_needs_no_confirmation(self) -> None:
        # Confirmation is only required for the whole-scope (null-session) path.
        await self._tombstone("session-1")
        result = await self._purge("session-1")

        self.assertFalse(result.dry_run)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 1)

    async def test_purge_requires_admin_lifecycle_capability(self) -> None:
        await self._tombstone("session-1")
        with self.assertRaises(PermissionError):
            await self.service.purge_scope(
                PurgeScopeRequest(
                    scope=_scope({MemoryCapability.SEARCH}),
                    target_scope=_selector("session-1"),
                    reason="user requested erasure",
                    redaction=RedactionContext(forbidden_values=()),
                )
            )

    async def test_dry_run_reports_counts_without_deleting(self) -> None:
        await self._tombstone("session-1")
        result = await self._purge("session-1", dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertGreaterEqual(result.purged["messages"], 1)
        self.assertIsNone(result.purged_at)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 2)
            deferred = conn.execute(
                "SELECT physical_purge_deferred FROM scope_tombstones"
            ).fetchone()[0]
            self.assertEqual(deferred, 1)

    async def test_second_purge_is_idempotent(self) -> None:
        await self._tombstone("session-1")
        await self._purge("session-1")
        result = await self._purge("session-1")

        self.assertEqual(result.purged["messages"], 0)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 1)

    async def _append(self, session_id: str, text: str):
        return await self.service.append_transcript(
            AppendTranscriptRequest(
                scope=_scope({MemoryCapability.WRITE}).model_copy(
                    update={"session_id": session_id}
                ),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content=text)])
                    ).decode()
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

    async def _ingest(self, session_id: str, text: str):
        return await self.service.ingest_source_transcript(
            IngestSourceTranscriptRequest(
                scope=_scope({MemoryCapability.WRITE}).model_copy(
                    update={"session_id": session_id}
                ),
                messages=[
                    SourceTranscriptMessage(
                        source_host="claude-code",
                        source_session_id=f"src-{session_id}",
                        source_message_id=f"uuid-{text[:8]}",
                        message_json=single_message_adapter.dump_json(
                            ModelRequest(parts=[UserPromptPart(content=text)])
                        ).decode(),
                    )
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

    async def test_append_transcript_rejected_while_tombstone_pends_purge(self) -> None:
        await self._tombstone("session-1")

        with self.assertRaisesRegex(PermissionError, "tombstoned for write"):
            await self._append("session-1", "late write into erased scope")

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 2)

    async def test_ingest_source_transcript_rejected_while_tombstone_pends_purge(
        self,
    ) -> None:
        await self._tombstone("session-1")

        with self.assertRaisesRegex(PermissionError, "tombstoned for write"):
            await self._ingest("session-1", "late ingest into erased scope")

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 2)

    async def test_writes_rejected_after_purge_completes(self) -> None:
        # The tombstone survives the purge as the audit record, so the write
        # block persists: re-ingesting would recreate data behind the erasure.
        await self._tombstone("session-1")
        await self._purge("session-1")

        with self.assertRaisesRegex(PermissionError, "tombstoned for write"):
            await self._append("session-1", "resurrection attempt")
        with self.assertRaisesRegex(PermissionError, "tombstoned for write"):
            await self._ingest("session-1", "resurrection attempt")

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 1)

    async def test_writes_to_non_tombstoned_scope_unaffected(self) -> None:
        await self._tombstone("session-1")

        append_result = await self._append("session-2", "survivor session write")
        ingest_result = await self._ingest("session-2", "survivor session ingest")

        self.assertEqual(len(append_result.message_ids), 1)
        self.assertEqual(ingest_result.items[0].status, "inserted")

    async def test_dream_phase_writes_rejected_for_any_tombstone_flags(self) -> None:
        # Candidate/fact writes fail closed on ANY matching tombstone, even one
        # whose lifecycle flags are all zero: every tombstone marks the scope
        # for erasure, so consolidation must not write new rows into it.
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO scope_tombstones
                        (target_tenant_id, target_session_id,
                         created_by_principal_id, created_by_principal_type, reason,
                         retrieval_blocked, export_blocked, replay_blocked,
                         rebuild_blocked, physical_purge_deferred)
                    VALUES ('tenant-a', 'session-1', 'operator', 'operator',
                            'flags all zero', 0, 0, 0, 0, 1)
                    """
                )

        with self.assertRaisesRegex(PermissionError, "tombstoned for write"):
            await self.service.run_dream_phase(
                RunDreamPhaseRequest(
                    scope=_scope({MemoryCapability.ADMIN_REBUILD}),
                    phase=DreamPhase.LIGHT,
                    redaction=RedactionContext(forbidden_values=()),
                )
            )

    async def test_failed_purge_rolls_back_everything(self) -> None:
        await self._tombstone("session-1")

        from vexic.storage import purge as purge_module

        with patch.object(
            purge_module,
            "_scrub_dream_run_error_detail",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                await self._purge("session-1")

        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(self._count(conn, "SELECT COUNT(*) FROM messages"), 2)
            self.assertEqual(
                self._count(conn, "SELECT COUNT(*) FROM long_term_memory"), 2
            )
            self.assertEqual(
                self._count(
                    conn,
                    f"SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH '{PURGED_TERM}'",
                ),
                1,
            )
            deferred = conn.execute(
                "SELECT physical_purge_deferred FROM scope_tombstones"
            ).fetchone()[0]
            self.assertEqual(deferred, 1)


if __name__ == "__main__":
    unittest.main()
