import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    AppendTranscriptRequest,
    ExpandHistoryRequest,
    ExportScopeRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    ReplayScopeRequest,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.service import LocalMemoryService
from vexic.storage import (
    fetch_session_summary_frontier,
    init_db,
    record_session_summary,
    render_session_recap,
    save_messages,
    single_message_adapter,
)
from vexic.summarize import run_summarize_phase

SENTINEL = "cedarcodecsecret"
PREFIX = "vxtest:"


class Base64Codec:
    """Reversible fake codec proving the seam: prefixed base64, with legacy
    plaintext passthrough on decode (rows written before the codec existed)."""

    def encode(self, plaintext: str) -> str:
        return PREFIX + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")

    def decode(self, stored: str) -> str:
        if not stored.startswith(PREFIX):
            return stored
        return base64.b64decode(stored[len(PREFIX):]).decode("utf-8")


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


class ContentCodecTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)
        self.service = LocalMemoryService(
            db_path=self.db_path,
            tenant_id="tenant-a",
            content_codec=Base64Codec(),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _append(self, text: str) -> int:
        result = await self.service.append_transcript(
            AppendTranscriptRequest(
                scope=_scope({MemoryCapability.WRITE}),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content=text)])
                    ).decode()
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        return result.message_ids[0]

    async def test_transcript_content_is_ciphertext_on_disk_plaintext_on_read(
        self,
    ) -> None:
        message_id = await self._append(f"{SENTINEL} lives in the transcript")

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute(
                "SELECT message_json FROM messages WHERE id = ?", (message_id,)
            ).fetchone()[0]
        self.assertNotIn(SENTINEL, stored)
        self.assertTrue(stored.startswith(PREFIX))

        replay = await self.service.replay_scope(
            ReplayScopeRequest(
                scope=_scope({MemoryCapability.REPLAY}),
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        self.assertIn(SENTINEL, replay.messages[0].body)

        expanded = await self.service.expand_history(
            ExpandHistoryRequest(
                scope=_scope({MemoryCapability.EXPAND_HISTORY}),
                first_message_id=message_id,
                last_message_id=message_id,
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        self.assertIn(SENTINEL, expanded.text)

    async def test_search_still_matches_via_plaintext_fts_projection(self) -> None:
        # The FTS shadow is a documented plaintext residue: search must keep
        # working while the canonical column holds ciphertext.
        await self._append(f"{SENTINEL} lives in the transcript")

        result = await self.service.search_transcript(
            SearchTranscriptRequest(
                scope=_scope({MemoryCapability.SEARCH}),
                query=SENTINEL,
            )
        )

        self.assertEqual(len(result.hits), 1)
        self.assertIn(SENTINEL, result.hits[0].body)

    async def test_export_decodes_to_plaintext_for_privileged_egress(self) -> None:
        await self._append(f"{SENTINEL} lives in the transcript")

        export = await self.service.export_scope(
            ExportScopeRequest(
                scope=_scope({MemoryCapability.EXPORT}),
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        artifact_text = Path(export.artifact_ref).read_text(encoding="utf-8")
        self.assertIn(SENTINEL, artifact_text)
        # Privileged egress ships readable JSON, never codec envelopes.
        self.assertNotIn(PREFIX, artifact_text)
        json.loads(artifact_text)

    async def test_legacy_plaintext_rows_read_back_unchanged(self) -> None:
        plaintext_service = LocalMemoryService(
            db_path=self.db_path, tenant_id="tenant-a"
        )
        legacy_json = single_message_adapter.dump_json(
            ModelRequest(parts=[UserPromptPart(content="legacy plain row")])
        ).decode()
        result = await plaintext_service.append_transcript(
            AppendTranscriptRequest(
                scope=_scope({MemoryCapability.WRITE}),
                messages_json=[legacy_json],
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        message_id = result.message_ids[0]

        expanded = await self.service.expand_history(
            ExpandHistoryRequest(
                scope=_scope({MemoryCapability.EXPAND_HISTORY}),
                first_message_id=message_id,
                last_message_id=message_id,
                redaction=RedactionContext(forbidden_values=()),
            )
        )
        self.assertIn("legacy plain row", expanded.text)

    async def test_fts_rebuild_decodes_encoded_rows(self) -> None:
        # A future FTS schema change drops messages_fts and forces a rebuild.
        # The rebuild re-derives each body from message_json, so it must decode
        # through the codec; a codec-aware init_db keeps search working.
        await self._append(f"{SENTINEL} lives in the transcript")
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("DROP TABLE messages_fts")

        init_db(self.db_path, force=True, content_codec=Base64Codec())

        result = await self.service.search_transcript(
            SearchTranscriptRequest(
                scope=_scope({MemoryCapability.SEARCH}),
                query=SENTINEL,
            )
        )
        self.assertEqual(len(result.hits), 1)
        self.assertIn(SENTINEL, result.hits[0].body)

    async def test_fts_rebuild_without_codec_cannot_decode_encoded_rows(self) -> None:
        # Guards the regression: a codec-blind rebuild feeds ciphertext to
        # json.loads and raises instead of silently corrupting the index.
        await self._append(f"{SENTINEL} lives in the transcript")
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("DROP TABLE messages_fts")

        with self.assertRaises(ValueError):
            init_db(self.db_path, force=True)

    async def test_identity_default_stores_plaintext_json(self) -> None:
        plaintext_service = LocalMemoryService(
            db_path=self.db_path, tenant_id="tenant-a"
        )
        result = await plaintext_service.append_transcript(
            AppendTranscriptRequest(
                scope=_scope({MemoryCapability.WRITE}),
                messages_json=[
                    single_message_adapter.dump_json(
                        ModelRequest(parts=[UserPromptPart(content="plain default")])
                    ).decode()
                ],
                redaction=RedactionContext(forbidden_values=()),
            )
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute(
                "SELECT message_json FROM messages WHERE id = ?",
                (result.message_ids[0],),
            ).fetchone()[0]
        self.assertIn("plain default", stored)
        json.loads(stored)


class _EchoSummaryAgent:
    """Fake AgentFactory-compatible agent that records the plaintext prompt
    it was handed, proving the summarize phase decodes compaction source
    before invoking the agent even when the transcript is codec-encoded."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, prompt: str):
        from types import SimpleNamespace

        self.calls.append(prompt)
        return SimpleNamespace(
            output=f"summary of: {prompt}",
            usage=lambda: SimpleNamespace(
                requests=1, input_tokens=5, output_tokens=3, total_tokens=8
            ),
        )


class SessionSummaryContentCodecTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_record_and_fetch_session_summary_round_trips_through_codec(self) -> None:
        codec = Base64Codec()
        record_session_summary(
            self.db_path,
            session_id="session-1",
            kind="leaf",
            first_message_id=1,
            last_message_id=1,
            summary_text=f"{SENTINEL} plaintext summary",
            content_codec=codec,
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute(
                "SELECT summary_text FROM session_summaries WHERE session_id = ?",
                ("session-1",),
            ).fetchone()[0]
        self.assertNotIn(SENTINEL, stored)
        self.assertTrue(stored.startswith(PREFIX))

        frontier = fetch_session_summary_frontier(
            self.db_path, session_id="session-1", content_codec=codec
        )
        self.assertEqual(len(frontier), 1)
        self.assertIn(SENTINEL, frontier[0].summary_text)

    def test_render_session_recap_decodes_with_codec(self) -> None:
        codec = Base64Codec()
        record_session_summary(
            self.db_path,
            session_id="session-1",
            kind="leaf",
            first_message_id=1,
            last_message_id=1,
            summary_text=f"{SENTINEL} recap body",
            content_codec=codec,
        )

        recap = render_session_recap(
            self.db_path, session_id="session-1", content_codec=codec
        )
        self.assertIn(SENTINEL, recap)

    def test_record_session_summary_redaction_runs_on_plaintext_before_encode(
        self,
    ) -> None:
        codec = Base64Codec()
        with self.assertRaises(ValueError):
            record_session_summary(
                self.db_path,
                session_id="session-1",
                kind="leaf",
                first_message_id=1,
                last_message_id=1,
                summary_text="the secret is s3cr3t-value",
                forbidden_secret_values=("s3cr3t-value",),
                content_codec=codec,
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM session_summaries WHERE session_id = ?",
                ("session-1",),
            ).fetchone()
        self.assertEqual(row[0], 0)

    async def test_run_summarize_phase_encodes_storage_and_decodes_agent_input(
        self,
    ) -> None:
        from datetime import datetime, timedelta, timezone

        codec = Base64Codec()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        save_messages(
            self.db_path,
            [
                single_message_adapter.validate_python(
                    {
                        "parts": [
                            {
                                "part_kind": "user-prompt",
                                "content": f"{SENTINEL} message body padding to add tokens #{i}",
                            }
                        ],
                        "kind": "request",
                    }
                )
                for i in range(3)
            ],
            session_id="default",
            content_codec=codec,
            timestamp=start.isoformat(),
        )

        agent = _EchoSummaryAgent()

        def factory(model_group: str, secrets=None):
            return agent

        await run_summarize_phase(
            self.db_path,
            "glm",
            summary_agent_factory=factory,
            now_utc=start + timedelta(hours=6),
            content_codec=codec,
        )

        # The agent must have seen decoded plaintext compaction source.
        self.assertTrue(agent.calls)
        self.assertTrue(any(SENTINEL in call for call in agent.calls))

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored_messages = conn.execute(
                "SELECT message_json FROM messages WHERE session_id = ?",
                ("default",),
            ).fetchall()
            stored_summaries = conn.execute(
                "SELECT summary_text FROM session_summaries WHERE session_id = ?",
                ("default",),
            ).fetchall()
        self.assertTrue(stored_messages)
        for (message_json,) in stored_messages:
            self.assertNotIn(SENTINEL, message_json)
            self.assertTrue(message_json.startswith(PREFIX))
        self.assertTrue(stored_summaries)
        for (summary_text,) in stored_summaries:
            self.assertNotIn(SENTINEL, summary_text)
            self.assertTrue(summary_text.startswith(PREFIX))

        frontier = fetch_session_summary_frontier(
            self.db_path, session_id="default", content_codec=codec
        )
        self.assertTrue(frontier)
        self.assertTrue(any(SENTINEL in summary.summary_text for summary in frontier))


if __name__ == "__main__":
    unittest.main()
