import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vexic.contract import (
    IngestSourceTranscriptResult,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    SearchTranscriptRequest,
    SourceTranscriptIngestItemResult,
    TrustBoundary,
)
from vexic.service import LocalMemoryService


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import-claude-code-jsonl.py"


def _load_importer():
    spec = importlib.util.spec_from_file_location("claude_code_jsonl_importer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scope() -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant-a",
        session_id="default",
        principal=Principal(
            principal_id="test-operator",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities={MemoryCapability.SEARCH},
    )


class ClaudeCodeJsonlImporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "memory.db"
        self.jsonl_path = self.root / "session.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_module_import_does_not_mutate_sys_path(self) -> None:
        original_path = sys.path.copy()
        try:
            _load_importer()
            after_import = sys.path.copy()
        finally:
            sys.path[:] = original_path

        self.assertEqual(after_import, original_path)

    async def test_imports_clean_user_and_assistant_text(self) -> None:
        rows = [
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "uuid-user",
                "message": {"role": "user", "content": "remember cedar"},
            },
            {
                "type": "assistant",
                "sessionId": "session-1",
                "uuid": "uuid-assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "stored birch"}],
                },
            },
        ]
        self.jsonl_path.write_text(
            "\n".join(json.dumps(row) for row in rows),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--tenant-id",
                "tenant-a",
                "--session-id",
                "default",
                str(self.jsonl_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["inserted"], 2)

        service = LocalMemoryService(db_path=str(self.db_path), tenant_id="tenant-a")
        cedar = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="cedar")
        )
        birch = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="birch")
        )
        self.assertEqual([hit.body for hit in cedar.hits], ["User: remember cedar"])
        self.assertEqual([hit.body for hit in birch.hits], ["Assistant: stored birch"])

    async def test_ignores_non_transcript_rows(self) -> None:
        rows = [
            {
                "type": "assistant",
                "sessionId": "session-1",
                "uuid": "thinking",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "hidden cedar"}],
                },
            },
            {
                "type": "assistant",
                "sessionId": "session-1",
                "uuid": "tool-use",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "lookup", "input": {}}],
                },
            },
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "tool-result",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "tool cedar"}],
                },
            },
            {"type": "summary", "sessionId": "session-1", "summary": "summary cedar"},
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "clean",
                "message": {"role": "user", "content": "clean cedar"},
            },
        ]
        self.jsonl_path.write_text(
            "\n".join(json.dumps(row) for row in rows),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--tenant-id",
                "tenant-a",
                str(self.jsonl_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["ignored"], 4)

        service = LocalMemoryService(db_path=str(self.db_path), tenant_id="tenant-a")
        clean = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="clean")
        )
        polluted = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="cedar")
        )
        self.assertEqual([hit.body for hit in clean.hits], ["User: clean cedar"])
        self.assertEqual([hit.body for hit in polluted.hits], ["User: clean cedar"])

    async def test_ignores_rows_with_blank_source_identifiers(self) -> None:
        rows = [
            {
                "type": "user",
                "sessionId": "   ",
                "uuid": "blank-session",
                "message": {"role": "user", "content": "ignored cedar"},
            },
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "\t",
                "message": {"role": "user", "content": "ignored birch"},
            },
            {
                "type": "user",
                "sessionId": " session-1 ",
                "uuid": " uuid-clean ",
                "message": {"role": "user", "content": "clean maple"},
            },
        ]
        self.jsonl_path.write_text(
            "\n".join(json.dumps(row) for row in rows),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--tenant-id",
                "tenant-a",
                str(self.jsonl_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["ignored"], 2)

        service = LocalMemoryService(db_path=str(self.db_path), tenant_id="tenant-a")
        clean = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="maple")
        )
        self.assertEqual([hit.body for hit in clean.hits], ["User: clean maple"])

    async def test_run_ingests_configured_batches(self) -> None:
        rows = [
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": f"uuid-{index}",
                "message": {"role": "user", "content": f"clean {index}"},
            }
            for index in range(5)
        ]
        self.jsonl_path.write_text(
            "\n".join([*(json.dumps(row) for row in rows), "not-json"]),
            encoding="utf-8",
        )
        importer = _load_importer()
        batch_sizes = []

        class FakeMemoryService:
            def __init__(self, **_kwargs) -> None:
                pass

            def init_schema(self) -> None:
                pass

            async def ingest_source_transcript(self, request):
                batch_sizes.append(len(request.messages))
                return IngestSourceTranscriptResult(
                    items=[
                        SourceTranscriptIngestItemResult(
                            source_host=message.source_host,
                            source_session_id=message.source_session_id,
                            source_message_id=message.source_message_id,
                            status="inserted",
                        )
                        for message in request.messages
                    ]
                )

        importer.LocalMemoryService = FakeMemoryService

        summary = await importer._run(
            importer._parse_args(
                [
                    "--db-path",
                    str(self.db_path),
                    "--tenant-id",
                    "tenant-a",
                    "--batch-size",
                    "2",
                    str(self.jsonl_path),
                ]
            )
        )

        self.assertEqual(batch_sizes, [2, 2, 1])
        self.assertEqual(
            summary,
            {"inserted": 5, "skipped": 0, "rejected": 0, "ignored": 1},
        )

    async def test_run_uses_request_redaction_for_forbidden_values(self) -> None:
        row = {
            "type": "user",
            "sessionId": "session-1",
            "uuid": "uuid-clean",
            "message": {"role": "user", "content": "clean cedar"},
        }
        self.jsonl_path.write_text(json.dumps(row), encoding="utf-8")
        importer = _load_importer()
        service_kwargs = {}
        request_redactions = []

        class FakeMemoryService:
            def __init__(self, **kwargs) -> None:
                service_kwargs.update(kwargs)

            def init_schema(self) -> None:
                pass

            async def ingest_source_transcript(self, request):
                request_redactions.append(request.redaction.forbidden_values)
                return IngestSourceTranscriptResult(items=[])

        importer.LocalMemoryService = FakeMemoryService

        await importer._run(
            importer._parse_args(
                [
                    "--db-path",
                    str(self.db_path),
                    "--tenant-id",
                    "tenant-a",
                    "--forbidden-value",
                    "cedar-secret",
                    str(self.jsonl_path),
                ]
            )
        )

        self.assertEqual(service_kwargs["forbidden_secret_values"], ())
        self.assertEqual(request_redactions, [("cedar-secret",)])

    def test_missing_file_returns_clean_error(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--tenant-id",
                "tenant-a",
                str(self.root / "missing.jsonl"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    async def test_forbidden_value_is_rejected_before_persistence(self) -> None:
        row = {
            "type": "user",
            "sessionId": "session-1",
            "uuid": "secret",
            "message": {"role": "user", "content": "cedar-secret"},
        }
        self.jsonl_path.write_text(json.dumps(row), encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db-path",
                str(self.db_path),
                "--tenant-id",
                "tenant-a",
                "--forbidden-value",
                "cedar-secret",
                str(self.jsonl_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["inserted"], 0)
        self.assertEqual(summary["rejected"], 1)

        service = LocalMemoryService(db_path=str(self.db_path), tenant_id="tenant-a")
        result = await service.search_transcript(
            SearchTranscriptRequest(scope=_scope(), query="cedar")
        )
        self.assertEqual(result.hits, [])
