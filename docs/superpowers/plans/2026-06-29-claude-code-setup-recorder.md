# Claude Code Setup Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a one-command Claude Code setup flow that records cleaned transcript rows to hosted Vexic through out-of-band HTTP ingest.

**Architecture:** Claude Code hooks invoke a Vexic CLI recorder with hook JSON on stdin. The recorder rereads the hook-provided transcript path, reuses shared Claude JSONL normalization, posts cleaned source rows to `/v1/ingest_source_transcript`, and relies on the hosted source ledger for duplicate protection. No always-on tailer or cross-agent adapter framework ships in this slice.

**Tech Stack:** Python 3.13, stdlib `argparse`/`json`/`urllib.request`, Pydantic contract models, existing hosted HTTP routes, `uv run pytest`.

---

## File Structure

- Create `docs/adr/0015-claude-code-setup-recorder.md`: durable architecture decision for setup-command-first Claude Code recorder.
- Modify `docs/adr/README.md`: add ADR 0015 to the canonical ADR index.
- Create `src/vexic/recorders/__init__.py`: package marker and small public exports.
- Create `src/vexic/recorders/claude_code.py`: shared Claude Code JSONL row normalization and file reader, moved out of the repo-local importer.
- Modify `scripts/import-claude-code-jsonl.py`: call the shared normalizer instead of owning cleaning logic.
- Create `src/vexic/recorders/hosted_ingest.py`: tiny stdlib HTTP client for `/v1/ingest_source_transcript`.
- Create `src/vexic/recorders/status.py`: status file model and secret-safe write helpers.
- Create `src/vexic/recorders/claude_setup.py`: setup/uninstall/status logic for Claude Code settings and user-local Vexic config.
- Create `src/vexic/recorders/cli.py`: recorder/setup command handlers.
- Create `src/vexic/cli.py`: top-level `vexic` command dispatcher.
- Modify `pyproject.toml`: add the `vexic = "vexic.cli:main"` console script.
- Create `tests/test_claude_code_recorder_shared.py`: shared normalizer coverage.
- Modify `tests/test_claude_code_jsonl_importer.py`: prove importer uses shared normalizer behavior.
- Create `tests/test_claude_code_recorder_cli.py`: recorder ingest, status, setup merge, uninstall, and CLI dispatch tests.
- Modify `README.md` and `docs/hosted-mvp.md`: document the setup command and the manual recovery path.

## Task 1: Record ADR 0015

**Files:**
- Create: `docs/adr/0015-claude-code-setup-recorder.md`
- Modify: `docs/adr/README.md`

- [ ] **Step 1: Write ADR 0015**

Create `docs/adr/0015-claude-code-setup-recorder.md` with this content:

```markdown
# Claude Code setup recorder is hook-triggered

Status: accepted

## Context

ADR 0002 says host recorders ingest complete cleaned visible user/assistant
transcript rows and leave extraction to later dream phases. ADR 0014 says
transcript writes are out-of-band hosted HTTP ingest, not MCP writes.

The earlier recorder deliberation considered an external file-tail recorder to
serve no-install and cross-agent goals. Those goals are no longer MVP
requirements. The MVP now optimizes for the most reliable Claude Code first-run
experience after one explicit setup command.

## Decision

Vexic ships a Claude Code setup-command recorder as the MVP baseline:

```powershell
vexic setup claude-code
```

Setup configures Claude Code hooks and user-local Vexic recorder config. The
hook-triggered recorder reads the hook-provided transcript path and session id,
normalizes Claude Code JSONL rows into `SourceTranscriptMessage` records, and
sends cleaned source rows to hosted `/v1/ingest_source_transcript`.

The recorder may reread a transcript on each hook invocation and rely on hosted
source-ledger idempotency. A local cursor can be added for efficiency, but
correctness must not depend on it.

The external file-tail daemon is not the MVP baseline. A bounded manual or
hook-triggered reconcile path may exist for recovery, but Vexic does not install
an always-on watcher in this slice.

## Consequences

- The first supported write loop is Claude Code only.
- Cross-agent recording is deferred until a second agent's transcript and
  trigger surfaces are empirically verified.
- Claude Code hook setup is an install/configuration step, but gives a clearer
  user experience than requiring a resident tail process.
- Setup must keep secrets in user-local config and avoid project-local hook
  files that embed credentials.
- The source-ledger key includes the resolved hosted scope's optional
  `agent_id`; setup must choose a stable `agent_id` policy.

## Deferred

- Codex, OpenClaw, and Hermes recorder adapters.
- Optional external tail mode for agents without hooks.
- Hosted/server-side capture for fully hosted runtimes.
```

- [ ] **Step 2: Update the ADR index**

Add this row to `docs/adr/README.md` in numeric order:

```markdown
| 0015 | Claude Code setup recorder is hook-triggered                   | accepted |
```

Add this bullet to the notes section if the file has an ADR notes list:

```markdown
- 0015 settles the Claude Code auto-record MVP as setup-command hook capture
  rather than an external file-tail daemon.
```

- [ ] **Step 3: Verify ADR drift check**

Run:

```powershell
python scripts\check_doc_drift.py --ci
```

Expected: exit `0`, with the ADR index and LocalMemoryService surface reported as matching.

- [ ] **Step 4: Commit**

```powershell
git add docs/adr/0015-claude-code-setup-recorder.md docs/adr/README.md
git commit -m "Document Claude Code setup recorder decision"
```

## Task 2: Extract Shared Claude Code JSONL Normalization

**Files:**
- Create: `src/vexic/recorders/__init__.py`
- Create: `src/vexic/recorders/claude_code.py`
- Create: `tests/test_claude_code_recorder_shared.py`
- Modify: `scripts/import-claude-code-jsonl.py`
- Modify: `tests/test_claude_code_jsonl_importer.py`

- [ ] **Step 1: Write failing shared-normalizer tests**

Create `tests/test_claude_code_recorder_shared.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse

from vexic.recorders.claude_code import (
    SOURCE_HOST,
    iter_claude_code_source_messages,
    source_message_from_claude_code_row,
)
from vexic.storage import single_message_adapter


class ClaudeCodeRecorderSharedTests(unittest.TestCase):
    def test_source_message_from_row_keeps_visible_text(self) -> None:
        message = source_message_from_claude_code_row(
            {
                "type": "assistant",
                "sessionId": " session-1 ",
                "uuid": " uuid-1 ",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "stored cedar"}],
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.source_host, SOURCE_HOST)
        self.assertEqual(message.source_session_id, "session-1")
        self.assertEqual(message.source_message_id, "uuid-1")
        model_message = single_message_adapter.validate_json(message.message_json)
        self.assertIsInstance(model_message, ModelResponse)

    def test_source_message_from_row_ignores_polluted_rows(self) -> None:
        polluted_rows = [
            {
                "type": "assistant",
                "sessionId": "session-1",
                "uuid": "thinking",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "hidden"}],
                },
            },
            {
                "type": "assistant",
                "sessionId": "session-1",
                "uuid": "tool-use",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "lookup"}],
                },
            },
            {
                "type": "user",
                "isSidechain": True,
                "sessionId": "session-1",
                "uuid": "sidechain",
                "message": {"role": "user", "content": "hidden"},
            },
            {"type": "summary", "sessionId": "session-1", "summary": "summary"},
        ]

        for row in polluted_rows:
            with self.subTest(row=row):
                self.assertIsNone(source_message_from_claude_code_row(row))

    def test_iter_claude_code_source_messages_yields_none_for_bad_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "session-1",
                                "uuid": "uuid-1",
                                "message": {"role": "user", "content": "remember maple"},
                            }
                        ),
                        "not-json",
                        json.dumps({"type": "summary", "summary": "skip"}),
                    ]
                ),
                encoding="utf-8",
            )

            items = list(iter_claude_code_source_messages([path]))

        self.assertEqual(len(items), 3)
        self.assertIsNotNone(items[0])
        assert items[0] is not None
        self.assertIsInstance(
            single_message_adapter.validate_json(items[0].message_json),
            ModelRequest,
        )
        self.assertIsNone(items[1])
        self.assertIsNone(items[2])
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_shared.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'vexic.recorders'`.

- [ ] **Step 3: Add the recorder package marker**

Create `src/vexic/recorders/__init__.py`:

```python
from vexic.recorders.claude_code import (
    SOURCE_HOST,
    iter_claude_code_source_messages,
    source_message_from_claude_code_row,
)

__all__ = [
    "SOURCE_HOST",
    "iter_claude_code_source_messages",
    "source_message_from_claude_code_row",
]
```

- [ ] **Step 4: Move Claude JSONL normalization into the package**

Create `src/vexic/recorders/claude_code.py`:

```python
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from vexic.contract import SourceTranscriptMessage
from vexic.storage import single_message_adapter

SOURCE_HOST = "claude-code"


def _content_text(content: object) -> str | None:
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if not isinstance(content, list):
        return None
    parts = [
        part["text"].strip()
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
        and part["text"].strip()
    ]
    return "\n".join(parts) or None


def source_message_from_claude_code_row(
    row: dict[str, Any],
) -> SourceTranscriptMessage | None:
    if row.get("type") not in {"user", "assistant"}:
        return None
    if row.get("isMeta") or row.get("isSidechain"):
        return None
    source_session_id = row.get("sessionId")
    source_message_id = row.get("uuid")
    message = row.get("message")
    if not (
        isinstance(source_session_id, str)
        and isinstance(source_message_id, str)
        and isinstance(message, dict)
    ):
        return None
    source_session_id = source_session_id.strip()
    source_message_id = source_message_id.strip()
    if not source_session_id or not source_message_id:
        return None

    text = _content_text(message.get("content"))
    if text is None:
        return None

    role = message.get("role")
    if row["type"] == "user" and role == "user":
        model_message = ModelRequest(parts=[UserPromptPart(content=text)])
    elif row["type"] == "assistant" and role == "assistant":
        model_message = ModelResponse(parts=[TextPart(content=text)])
    else:
        return None

    try:
        return SourceTranscriptMessage(
            source_host=SOURCE_HOST,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            message_json=single_message_adapter.dump_json(model_message).decode(),
        )
    except ValueError:
        return None


def iter_claude_code_source_messages(
    paths: list[Path],
) -> Iterator[SourceTranscriptMessage | None]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    yield None
                    continue
                if not isinstance(row, dict):
                    yield None
                    continue
                yield source_message_from_claude_code_row(row)
```

- [ ] **Step 5: Update the existing importer to reuse shared code**

In `scripts/import-claude-code-jsonl.py`, remove imports of `Iterator`, `Any`,
`ModelRequest`, `ModelResponse`, `TextPart`, `UserPromptPart`, `SourceTranscriptMessage`,
and `single_message_adapter` if they are no longer used.

Add this import:

```python
from vexic.recorders.claude_code import iter_claude_code_source_messages
```

Delete `SOURCE_HOST`, `_content_text`, `_source_message`, and `_read_messages`.

Change the loop in `_run` from:

```python
    for message in _read_messages(args.jsonl_path):
```

to:

```python
    for message in iter_claude_code_source_messages(args.jsonl_path):
```

- [ ] **Step 6: Run normalizer and importer tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_shared.py tests/test_claude_code_jsonl_importer.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/vexic/recorders/__init__.py src/vexic/recorders/claude_code.py scripts/import-claude-code-jsonl.py tests/test_claude_code_recorder_shared.py tests/test_claude_code_jsonl_importer.py
git commit -m "Share Claude Code transcript normalization"
```

## Task 3: Add Hosted Ingest Client And Status Files

**Files:**
- Create: `src/vexic/recorders/hosted_ingest.py`
- Create: `src/vexic/recorders/status.py`
- Create: `tests/test_claude_code_recorder_cli.py`

- [ ] **Step 1: Write failing HTTP/status tests**

Create `tests/test_claude_code_recorder_cli.py` with these initial tests:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
from vexic.recorders.status import RecorderStatus, write_status


class ClaudeCodeRecorderCliTests(unittest.TestCase):
    def test_post_source_messages_sends_scope_headers_without_agent_id(self) -> None:
        calls = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return _Response()

        config = HostedIngestConfig(
            base_url="https://api.example.test/",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
            timeout_seconds=7.0,
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen):
            result = post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(result, {"items": []})
        request, timeout = calls[0]
        self.assertEqual(timeout, 7.0)
        self.assertEqual(request.full_url, "https://api.example.test/v1/ingest_source_transcript")
        self.assertEqual(request.get_header("Authorization"), "Bearer vx_secret")
        self.assertEqual(request.get_header("X-vexic-project-id"), "project-a")
        self.assertEqual(request.get_header("X-vexic-session-id"), "session-a")
        self.assertIsNone(request.get_header("X-vexic-agent-id"))
        body = json.loads(request.data.decode())
        self.assertEqual(body, {"messages": [], "redaction": {"forbidden_values": []}})

    def test_post_source_messages_includes_agent_id_when_configured(self) -> None:
        calls = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return b'{"items":[]}'

        def fake_urlopen(request, timeout):
            calls.append(request)
            return _Response()

        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id="agent-a",
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", fake_urlopen):
            post_source_messages(config, messages=[], forbidden_values=())

        self.assertEqual(calls[0].get_header("X-vexic-agent-id"), "agent-a")

    def test_post_source_messages_raises_sanitized_http_error(self) -> None:
        config = HostedIngestConfig(
            base_url="https://api.example.test",
            api_key="vx_secret",
            project_id="project-a",
            session_id="session-a",
            agent_id=None,
        )
        error = HTTPError(
            url="https://api.example.test/v1/ingest_source_transcript",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=None,
        )

        with patch("vexic.recorders.hosted_ingest.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "hosted ingest failed: HTTP 403"):
                post_source_messages(config, messages=[], forbidden_values=())

    def test_write_status_does_not_leak_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status_path = Path(temp) / "status.json"
            write_status(
                status_path,
                RecorderStatus(
                    ok=False,
                    operation="ingest",
                    source_session_id="session-1",
                    transcript_path="C:/tmp/session.jsonl",
                    inserted=1,
                    skipped=2,
                    rejected=3,
                    ignored=4,
                    error="hosted ingest failed: HTTP 403",
                ),
            )
            payload = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "ingest")
        self.assertEqual(payload["inserted"], 1)
        self.assertEqual(payload["skipped"], 2)
        self.assertEqual(payload["rejected"], 3)
        self.assertEqual(payload["ignored"], 4)
        self.assertNotIn("vx_secret", json.dumps(payload))
```

- [ ] **Step 2: Run failing HTTP/status tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py -q
```

Expected: FAIL with `ModuleNotFoundError` for `vexic.recorders.hosted_ingest`.

- [ ] **Step 3: Implement the hosted ingest client**

Create `src/vexic/recorders/hosted_ingest.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from vexic.contract import SourceTranscriptMessage


@dataclass(frozen=True)
class HostedIngestConfig:
    base_url: str
    api_key: str
    project_id: str
    session_id: str
    agent_id: str | None
    timeout_seconds: float = 10.0


def post_source_messages(
    config: HostedIngestConfig,
    *,
    messages: list[SourceTranscriptMessage],
    forbidden_values: tuple[str, ...],
) -> dict[str, object]:
    payload = {
        "messages": [message.model_dump(mode="json") for message in messages],
        "redaction": {"forbidden_values": list(forbidden_values)},
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "X-Vexic-Project-Id": config.project_id,
        "X-Vexic-Session-Id": config.session_id,
    }
    if config.agent_id is not None:
        headers["X-Vexic-Agent-Id"] = config.agent_id

    request = Request(
        urljoin(config.base_url.rstrip("/") + "/", "v1/ingest_source_transcript"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"hosted ingest failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"hosted ingest failed: {type(exc.reason).__name__}") from exc
```

- [ ] **Step 4: Implement status writing**

Create `src/vexic/recorders/status.py`:

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecorderStatus:
    ok: bool
    operation: str
    source_session_id: str | None
    transcript_path: str | None
    inserted: int = 0
    skipped: int = 0
    rejected: int = 0
    ignored: int = 0
    error: str | None = None


def write_status(path: Path, status: RecorderStatus) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(status), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
```

- [ ] **Step 5: Run tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/vexic/recorders/hosted_ingest.py src/vexic/recorders/status.py tests/test_claude_code_recorder_cli.py
git commit -m "Add hosted recorder ingest client"
```

## Task 4: Add Recorder Ingest CLI

**Files:**
- Create: `src/vexic/recorders/cli.py`
- Create: `src/vexic/cli.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_claude_code_recorder_cli.py`

- [ ] **Step 1: Add failing recorder ingest tests**

Append these tests to `tests/test_claude_code_recorder_cli.py`:

```python
import sys

from vexic.recorders.cli import main as recorder_main


class ClaudeCodeRecorderIngestCommandTests(unittest.TestCase):
    def test_ingest_from_hook_payload_posts_clean_rows_and_writes_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "claude-session",
                                "uuid": "uuid-1",
                                "message": {"role": "user", "content": "remember cedar"},
                            }
                        ),
                        json.dumps({"type": "summary", "summary": "ignore cedar"}),
                    ]
                ),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "claude-session",
                        "transcript_path": str(transcript),
                    }
                ),
                encoding="utf-8",
            )
            status_path = root / "status.json"
            calls = []

            def fake_post(config, *, messages, forbidden_values):
                calls.append((config, messages, forbidden_values))
                return {
                    "items": [
                        {
                            "source_host": "claude-code",
                            "source_session_id": "claude-session",
                            "source_message_id": "uuid-1",
                            "status": "inserted",
                        }
                    ]
                }

            with patch("vexic.recorders.cli.post_source_messages", fake_post):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--agent-id",
                        "agent-a",
                        "--status-path",
                        str(status_path),
                    ]
                )

            self.assertEqual(code, 0)
            config, messages, forbidden_values = calls[0]
            self.assertEqual(config.session_id, "vexic-session")
            self.assertEqual(config.agent_id, "agent-a")
            self.assertEqual(forbidden_values, ())
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].source_message_id, "uuid-1")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(status["ok"])
            self.assertEqual(status["inserted"], 1)
            self.assertEqual(status["ignored"], 1)

    def test_ingest_failure_writes_status_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-session",
                        "uuid": "uuid-1",
                        "message": {"role": "user", "content": "remember cedar"},
                    }
                ),
                encoding="utf-8",
            )
            hook_payload = root / "hook.json"
            hook_payload.write_text(
                json.dumps({"session_id": "claude-session", "transcript_path": str(transcript)}),
                encoding="utf-8",
            )
            status_path = root / "status.json"

            with patch(
                "vexic.recorders.cli.post_source_messages",
                side_effect=RuntimeError("hosted ingest failed: HTTP 403"),
            ):
                code = recorder_main(
                    [
                        "ingest",
                        "--hook-input",
                        str(hook_payload),
                        "--base-url",
                        "https://api.example.test",
                        "--api-key",
                        "vx_secret",
                        "--project-id",
                        "project-a",
                        "--session-id",
                        "vexic-session",
                        "--status-path",
                        str(status_path),
                    ]
                )

            self.assertEqual(code, 2)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"], "hosted ingest failed: HTTP 403")
            self.assertNotIn("vx_secret", json.dumps(status))
```

- [ ] **Step 2: Run failing recorder ingest tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py::ClaudeCodeRecorderIngestCommandTests -q
```

Expected: FAIL with `ModuleNotFoundError` for `vexic.recorders.cli`.

- [ ] **Step 3: Implement recorder CLI ingest command**

Create `src/vexic/recorders/cli.py`:

```python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vexic.recorders.claude_code import iter_claude_code_source_messages
from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
from vexic.recorders.status import RecorderStatus, write_status


def _hook_payload(path: Path | None) -> dict[str, object]:
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(sys.stdin.read())


def _count_items(result: dict[str, object]) -> dict[str, int]:
    counts = {"inserted": 0, "skipped": 0, "rejected": 0}
    for item in result.get("items", []):
        if isinstance(item, dict):
            status = item.get("status")
            if status in counts:
                counts[status] += 1
    return counts


def _ingest(args: argparse.Namespace) -> int:
    payload = _hook_payload(args.hook_input)
    transcript_path = payload.get("transcript_path")
    source_session_id = payload.get("session_id")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        raise ValueError("hook input must include transcript_path")
    if source_session_id is not None and not isinstance(source_session_id, str):
        raise ValueError("hook input session_id must be a string when present")

    messages = []
    ignored = 0
    for message in iter_claude_code_source_messages([Path(transcript_path)]):
        if message is None:
            ignored += 1
            continue
        messages.append(message)

    config = HostedIngestConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        project_id=args.project_id,
        session_id=args.session_id,
        agent_id=args.agent_id,
        timeout_seconds=args.timeout_seconds,
    )
    result = post_source_messages(
        config,
        messages=messages,
        forbidden_values=tuple(args.forbidden_value),
    )
    counts = _count_items(result)
    write_status(
        args.status_path,
        RecorderStatus(
            ok=True,
            operation="ingest",
            source_session_id=source_session_id,
            transcript_path=transcript_path,
            inserted=counts["inserted"],
            skipped=counts["skipped"],
            rejected=counts["rejected"],
            ignored=ignored,
        ),
    )
    print(json.dumps({"ok": True, **counts, "ignored": ignored}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vexic recorder helper.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    ingest = subcommands.add_parser("ingest")
    ingest.add_argument("--hook-input", type=Path)
    ingest.add_argument("--base-url", required=True)
    ingest.add_argument("--api-key", required=True)
    ingest.add_argument("--project-id", required=True)
    ingest.add_argument("--session-id", required=True)
    ingest.add_argument("--agent-id")
    ingest.add_argument("--status-path", type=Path, required=True)
    ingest.add_argument("--timeout-seconds", type=float, default=10.0)
    ingest.add_argument("--forbidden-value", action="append", default=[])

    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            return _ingest(args)
        raise ValueError(f"unknown command: {args.command}")
    except Exception as exc:
        if hasattr(args, "status_path"):
            write_status(
                args.status_path,
                RecorderStatus(
                    ok=False,
                    operation=str(getattr(args, "command", "unknown")),
                    source_session_id=None,
                    transcript_path=None,
                    error=str(exc),
                ),
            )
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

- [ ] **Step 4: Add top-level CLI dispatcher**

Create `src/vexic/cli.py`:

```python
from __future__ import annotations

import argparse

from vexic.recorders import cli as recorder_cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vexic command line tools.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    recorder = subcommands.add_parser("recorder")
    recorder.add_argument("recorder_args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.command == "recorder":
        return recorder_cli.main(args.recorder_args)
    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Add console script**

In `pyproject.toml`, add this section after `[project.urls]`:

```toml
[project.scripts]
vexic = "vexic.cli:main"
```

- [ ] **Step 6: Run recorder tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/vexic/recorders/cli.py src/vexic/cli.py pyproject.toml tests/test_claude_code_recorder_cli.py
git commit -m "Add Claude Code recorder ingest command"
```

## Task 5: Add Claude Code Setup, Status, And Uninstall

**Files:**
- Create: `src/vexic/recorders/claude_setup.py`
- Modify: `src/vexic/recorders/cli.py`
- Modify: `src/vexic/cli.py`
- Modify: `tests/test_claude_code_recorder_cli.py`

- [ ] **Step 1: Add failing setup merge tests**

Append these tests to `tests/test_claude_code_recorder_cli.py`:

```python
from vexic.recorders.claude_setup import (
    install_claude_code_setup,
    uninstall_claude_code_setup,
)


class ClaudeCodeSetupTests(unittest.TestCase):
    def test_setup_merges_user_settings_without_raw_secret_in_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "echo existing",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id="agent-a",
                command="python -m vexic.cli recorder ingest",
            )

            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            stop_groups = settings["hooks"]["Stop"]
            commands = [
                hook["command"]
                for group in stop_groups
                for hook in group["hooks"]
            ]
            self.assertIn("echo existing", commands)
            vexic_commands = [command for command in commands if "vexic" in command]
            self.assertEqual(len(vexic_commands), 1)
            self.assertNotIn("vx_secret", vexic_commands[0])
            self.assertIn(str(result.config_path), vexic_commands[0])
            config = json.loads(result.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["api_key"], "vx_secret")
            self.assertEqual(config["agent_id"], "agent-a")

    def test_setup_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for _ in range(2):
                install_claude_code_setup(
                    home=home,
                    base_url="https://api.example.test",
                    api_key="vx_secret",
                    project_id="project-a",
                    session_id="session-a",
                    agent_id=None,
                    command="python -m vexic.cli recorder ingest",
                )

            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for group in settings["hooks"]["Stop"]
                for hook in group["hooks"]
                if "vexic" in hook["command"]
            ]
            self.assertEqual(len(commands), 1)

    def test_uninstall_removes_only_vexic_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            install_claude_code_setup(
                home=home,
                base_url="https://api.example.test",
                api_key="vx_secret",
                project_id="project-a",
                session_id="session-a",
                agent_id=None,
                command="python -m vexic.cli recorder ingest",
            )
            settings_path = home / ".claude" / "settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["hooks"]["Stop"].append(
                {"hooks": [{"type": "command", "command": "echo keep"}]}
            )
            settings_path.write_text(json.dumps(settings), encoding="utf-8")

            removed = uninstall_claude_code_setup(home=home)

            self.assertTrue(removed)
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for group in settings["hooks"]["Stop"]
                for hook in group["hooks"]
            ]
            self.assertEqual(commands, ["echo keep"])
```

- [ ] **Step 2: Run failing setup tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py::ClaudeCodeSetupTests -q
```

Expected: FAIL with `ModuleNotFoundError` for `vexic.recorders.claude_setup`.

- [ ] **Step 3: Implement setup merge/uninstall helpers**

Create `src/vexic/recorders/claude_setup.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

VEXIC_HOOK_ID = "vexic-claude-code-recorder"


@dataclass(frozen=True)
class ClaudeCodeSetupResult:
    settings_path: Path
    config_path: Path
    status_path: Path
    command: str


def _settings_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _config_path(home: Path) -> Path:
    return home / ".vexic" / "claude-code-recorder.json"


def _status_path(home: Path) -> Path:
    return home / ".vexic" / "claude-code-recorder-status.json"


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _vexic_hook(command: str) -> dict[str, object]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "async": True,
                "timeout": 120,
                "vexicHookId": VEXIC_HOOK_ID,
            }
        ]
    }


def _without_vexic_hooks(groups: object) -> list[object]:
    if not isinstance(groups, list):
        return []
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            kept.append(group)
            continue
        filtered = [
            hook
            for hook in hooks
            if not (isinstance(hook, dict) and hook.get("vexicHookId") == VEXIC_HOOK_ID)
        ]
        if filtered:
            next_group = dict(group)
            next_group["hooks"] = filtered
            kept.append(next_group)
    return kept


def install_claude_code_setup(
    *,
    home: Path,
    base_url: str,
    api_key: str,
    project_id: str,
    session_id: str,
    agent_id: str | None,
    command: str,
) -> ClaudeCodeSetupResult:
    if not api_key.strip():
        raise ValueError("api_key must not be blank")
    if not project_id.strip():
        raise ValueError("project_id must not be blank")
    if not session_id.strip():
        raise ValueError("session_id must not be blank")

    config_path = _config_path(home)
    status_path = _status_path(home)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "api_key": api_key,
                "project_id": project_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "status_path": str(status_path),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    hook_command = f"{command} --config {config_path}"
    settings_path = _settings_path(home)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _load_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude Code settings hooks must be an object")
    stop_groups = _without_vexic_hooks(hooks.get("Stop"))
    stop_groups.append(_vexic_hook(hook_command))
    hooks["Stop"] = stop_groups
    settings_path.write_text(
        json.dumps(settings, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return ClaudeCodeSetupResult(
        settings_path=settings_path,
        config_path=config_path,
        status_path=status_path,
        command=hook_command,
    )


def uninstall_claude_code_setup(*, home: Path) -> bool:
    settings_path = _settings_path(home)
    if not settings_path.exists():
        return False
    settings = _load_json(settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    before = json.dumps(hooks.get("Stop"), sort_keys=True)
    hooks["Stop"] = _without_vexic_hooks(hooks.get("Stop"))
    after = json.dumps(hooks.get("Stop"), sort_keys=True)
    settings_path.write_text(
        json.dumps(settings, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return before != after
```

- [ ] **Step 4: Wire setup/uninstall/status commands into recorder CLI**

Modify `src/vexic/recorders/cli.py`:

Add imports:

```python
from vexic.recorders.claude_setup import (
    install_claude_code_setup,
    uninstall_claude_code_setup,
)
```

Add this helper:

```python
def _config_args(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("recorder config must be a JSON object")
    return value
```

At the start of `_ingest`, before reading hook payload, add:

```python
    if args.config is not None:
        config = _config_args(args.config)
        args.base_url = str(config["base_url"])
        args.api_key = str(config["api_key"])
        args.project_id = str(config["project_id"])
        args.session_id = str(config["session_id"])
        args.agent_id = config.get("agent_id")
        args.status_path = Path(str(config["status_path"]))
```

Make the `ingest` parser accept `--config` and make hosted args optional:

```python
    ingest.add_argument("--config", type=Path)
    ingest.add_argument("--base-url")
    ingest.add_argument("--api-key")
    ingest.add_argument("--project-id")
    ingest.add_argument("--session-id")
```

After parsing, enforce missing args in `_ingest`:

```python
    for name in ("base_url", "api_key", "project_id", "session_id", "status_path"):
        if getattr(args, name) is None:
            raise ValueError(f"--{name.replace('_', '-')} is required")
```

Add setup/uninstall parsers:

```python
    setup = subcommands.add_parser("setup-claude-code")
    setup.add_argument("--home", type=Path, default=Path.home())
    setup.add_argument("--base-url", required=True)
    setup.add_argument("--api-key", required=True)
    setup.add_argument("--project-id", required=True)
    setup.add_argument("--session-id", required=True)
    setup.add_argument("--agent-id")
    setup.add_argument(
        "--command",
        default=f"{sys.executable} -m vexic.cli recorder ingest",
    )

    uninstall = subcommands.add_parser("uninstall-claude-code")
    uninstall.add_argument("--home", type=Path, default=Path.home())
```

Handle them in `main`:

```python
        if args.command == "setup-claude-code":
            result = install_claude_code_setup(
                home=args.home,
                base_url=args.base_url,
                api_key=args.api_key,
                project_id=args.project_id,
                session_id=args.session_id,
                agent_id=args.agent_id,
                command=args.command,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "settings_path": str(result.settings_path),
                        "config_path": str(result.config_path),
                        "status_path": str(result.status_path),
                        "hook_command": result.command,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "uninstall-claude-code":
            removed = uninstall_claude_code_setup(home=args.home)
            print(json.dumps({"ok": True, "removed": removed}, sort_keys=True))
            return 0
```

Use a different local variable name for `setup.add_argument("--command")`, because `args.command` already stores the subcommand. Use `setup.add_argument("--hook-command", dest="hook_command", default=f"{sys.executable} -m vexic.cli recorder ingest")` and pass `command=args.hook_command`.

- [ ] **Step 5: Add top-level `vexic setup claude-code` dispatch**

Modify `src/vexic/cli.py` to support the product command while preserving `vexic recorder ...`:

```python
from __future__ import annotations

import argparse

from vexic.recorders import cli as recorder_cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vexic command line tools.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    recorder = subcommands.add_parser("recorder")
    recorder.add_argument("recorder_args", nargs=argparse.REMAINDER)

    setup = subcommands.add_parser("setup")
    setup_subcommands = setup.add_subparsers(dest="setup_command", required=True)
    claude_code = setup_subcommands.add_parser("claude-code")
    claude_code.add_argument("setup_args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.command == "recorder":
        return recorder_cli.main(args.recorder_args)
    if args.command == "setup" and args.setup_command == "claude-code":
        return recorder_cli.main(["setup-claude-code", *args.setup_args])
    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Add CLI setup command tests**

Append this test to `ClaudeCodeSetupTests`:

```python
    def test_top_level_setup_claude_code_dispatches(self) -> None:
        from vexic.cli import main as vexic_main

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            code = vexic_main(
                [
                    "setup",
                    "claude-code",
                    "--home",
                    str(home),
                    "--base-url",
                    "https://api.example.test",
                    "--api-key",
                    "vx_secret",
                    "--project-id",
                    "project-a",
                    "--session-id",
                    "session-a",
                ]
            )

        self.assertEqual(code, 0)
```

- [ ] **Step 7: Run setup tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py::ClaudeCodeSetupTests -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add src/vexic/recorders/claude_setup.py src/vexic/recorders/cli.py src/vexic/cli.py tests/test_claude_code_recorder_cli.py
git commit -m "Add Claude Code setup command"
```

## Task 6: Add Hosted Round Trip And Docs

**Files:**
- Modify: `tests/test_claude_code_recorder_cli.py`
- Modify: `README.md`
- Modify: `docs/hosted-mvp.md`

- [ ] **Step 1: Add hosted round-trip test**

Append this test to `tests/test_claude_code_recorder_cli.py`:

```python
class ClaudeCodeRecorderHostedRoundTripTests(unittest.TestCase):
    def test_recorder_ingest_round_trips_through_hosted_service(self) -> None:
        from fastapi.testclient import TestClient

        from vexic.contract import MemoryCapability, SearchTranscriptRequest
        from vexic.hosted import HostedMemoryService
        from vexic.hosted_http import create_app
        from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
        from vexic.recorders.hosted_ingest import HostedIngestConfig, post_source_messages
        from vexic.recorders.claude_code import source_message_from_claude_code_row

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = HostedTenantCatalog(root)
            keys = HostedApiKeyStore(root)
            catalog.provision_tenant("tenant-a", project_ids={"project-a"})
            raw_key = keys.create_key(
                tenant_id="tenant-a",
                principal_id="agent-a",
                capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
                project_ids={"project-a"},
                agent_ids={"agent-a"},
            ).raw_key
            client = TestClient(create_app(HostedMemoryService(catalog, keys)))
            message = source_message_from_claude_code_row(
                {
                    "type": "user",
                    "sessionId": "claude-session",
                    "uuid": "uuid-1",
                    "message": {"role": "user", "content": "hosted recorder cedar"},
                }
            )
            assert message is not None

            with patch(
                "vexic.recorders.hosted_ingest.urlopen",
                lambda request, timeout: client.post(
                    "/v1/ingest_source_transcript",
                    headers=dict(request.header_items()),
                    content=request.data,
                ),
            ):
                post_source_messages(
                    HostedIngestConfig(
                        base_url="http://testserver",
                        api_key=raw_key,
                        project_id="project-a",
                        session_id="session-a",
                        agent_id="agent-a",
                    ),
                    messages=[message],
                    forbidden_values=(),
                )

            search = client.post(
                "/v1/search_transcript",
                headers={"Authorization": f"Bearer {raw_key}"},
                json=SearchTranscriptRequest(
                    scope={
                        "tenant_id": "tenant-a",
                        "project_id": "project-a",
                        "session_id": "session-a",
                        "agent_id": "agent-a",
                        "principal": {
                            "principal_id": "agent-a",
                            "principal_type": "service",
                        },
                        "trust_boundary": "networked",
                        "capabilities": ["search"],
                    },
                    query="cedar",
                ).model_dump(mode="json"),
            )

        self.assertEqual(search.status_code, 200)
        self.assertEqual(
            [hit["body"] for hit in search.json()["hits"]],
            ["User: hosted recorder cedar"],
        )
```

If `TestClient` responses do not implement the context-manager API expected by `urlopen`, replace the patch lambda with a tiny adapter class:

```python
class _UrlopenResponse:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self.response.content
```

and return `_UrlopenResponse(client.post(...))`.

- [ ] **Step 2: Run the hosted round-trip test**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py::ClaudeCodeRecorderHostedRoundTripTests -q
```

Expected: PASS.

- [ ] **Step 3: Update README**

In `README.md`, replace or extend the Claude Code transcript import section with:

```markdown
## Claude Code Auto-Record Setup

Hosted Claude Code recording is configured with one command:

```powershell
vexic setup claude-code `
  --base-url https://api.vexic.dev `
  --api-key <raw-agent-key> `
  --project-id <project-id> `
  --session-id <session-id> `
  --agent-id <agent-id>
```

The command installs a user-local Claude Code hook that invokes the Vexic
recorder with Claude Code's hook-provided transcript path. The recorder keeps
visible user/assistant text, rejects or ignores non-transcript rows, and writes
cleaned source rows through hosted `/v1/ingest_source_transcript`. It does not
add MCP write tools.

To remove the hook:

```powershell
vexic recorder uninstall-claude-code
```

For manual recovery or local imports, use the JSONL importer:
```

Keep the existing manual importer command immediately below that text.

- [ ] **Step 4: Update hosted MVP docs**

In `docs/hosted-mvp.md`, update the hosted auto-recording note to say:

```markdown
Claude Code hosted auto-recording uses `vexic setup claude-code`. The setup
command installs user-local Claude Code hook configuration and Vexic recorder
config. Hook-triggered recorder runs send cleaned rows to
`/v1/ingest_source_transcript`; Claude Code reads through read-only MCP.
```

Keep the existing `/v1/ingest_source_transcript` curl example.

- [ ] **Step 5: Run docs and recorder tests**

Run:

```powershell
uv run pytest tests/test_claude_code_recorder_cli.py tests/test_claude_code_jsonl_importer.py tests/test_hosted_http.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add tests/test_claude_code_recorder_cli.py README.md docs/hosted-mvp.md
git commit -m "Document Claude Code recorder setup"
```

## Task 7: Final Verification And Project Tracking

**Files:**
- No new product files unless a verification failure requires a targeted fix.
- Project-tracking comments for the affected implementation issue.

- [ ] **Step 1: Run full tests**

Run:

```powershell
uv run pytest
```

Expected: PASS.

- [ ] **Step 2: Run boundary scans**

Run:

```powershell
rg -n "^(from|import) engine\\." src/vexic tests
rg -n "C[o]alescent|A[g]entOS|T[e]legram|B[lo]g Writer|t[e]ammate" docs/ai/AGENTS.md README.md docs src/vexic tests console
rg -n "C[O]A-[0-9]|L[inear]" src/vexic tests console docs --glob '!docs/adr/**' --glob '!docs/runbooks/**' --glob '!docs/provenance.md'
```

Expected:

- first command: no matches;
- second command: only pre-existing allowed compatibility/provenance hits;
- third command: no matches outside allowed paths.

- [ ] **Step 3: Re-run doc drift hook**

Run:

```powershell
python scripts\check_doc_drift.py --ci
```

Expected: exit `0`.

- [ ] **Step 4: Update project tracking**

Add a project-tracking comment to the implementation issue with:

```markdown
Implemented Claude Code setup-command recorder on `dev`.

Commits:
- <commit list from this plan>

Verification:
- `uv run pytest` passed.
- Boundary scans passed with only pre-existing allowed compatibility/provenance hits.
- ADR index/doc drift check passed.

Notes:
- MVP is Claude Code only.
- External tailing is deferred.
- Stable `agent_id` policy is documented in ADR 0015 and setup config.
```

- [ ] **Step 5: Push `dev`**

Run:

```powershell
git push origin dev
```

Expected: push succeeds.

## Self-Review

- Spec coverage: setup command, hook-triggered recorder, bounded reconcile stance, hosted ingest, shared cleaner, status/error reporting, security posture, tests, docs, and ADR are all covered.
- Placeholder scan: no placeholder markers or vague edge-case instructions remain.
- Type consistency: `HostedIngestConfig`, `RecorderStatus`, `install_claude_code_setup`, `uninstall_claude_code_setup`, `iter_claude_code_source_messages`, and `source_message_from_claude_code_row` are named consistently across tasks.
- Deliberate simplification: no local cursor in MVP. The recorder rereads transcript rows and relies on hosted source-ledger idempotency; add a cursor only when transcript size makes reread cost measurable.
