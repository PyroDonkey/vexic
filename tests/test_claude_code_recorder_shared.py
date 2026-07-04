import json
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse

from vexic.contract import PRIME_CONTEXT_HEADER
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

    def test_source_message_from_row_ignores_echoed_prime_context(self) -> None:
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "echoed-prime",
                "message": {
                    "role": "user",
                    "content": f"{PRIME_CONTEXT_HEADER}\nLong-term memory:\n- cedar",
                },
            }
        )

        self.assertIsNone(message)

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
