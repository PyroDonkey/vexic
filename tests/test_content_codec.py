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
from vexic.storage import init_db, single_message_adapter

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


if __name__ == "__main__":
    unittest.main()
