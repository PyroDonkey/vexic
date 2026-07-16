import json
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse

from vexic.contract import PRIME_CONTEXT_HEADER
from vexic.recorders.claude_code import (
    SOURCE_HOST,
    iter_claude_code_source_messages,
    scan_claude_code_transcript,
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

    def test_source_message_from_row_drops_slash_command_envelope(self) -> None:
        envelope_rows = [
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "slash-command",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-name>/clear</command-name>\n"
                        "<command-message>clear</command-message>\n"
                        "<command-args></command-args>"
                    ),
                },
            },
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "command-stdout",
                "message": {
                    "role": "user",
                    "content": (
                        "<local-command-stdout>Set model to sonnet"
                        "</local-command-stdout>"
                    ),
                },
            },
        ]

        for row in envelope_rows:
            with self.subTest(uuid=row["uuid"]):
                self.assertIsNone(source_message_from_claude_code_row(row))

    def test_source_message_from_row_strips_system_reminder_keeps_user_text(
        self,
    ) -> None:
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "mixed-reminder",
                "message": {
                    "role": "user",
                    "content": (
                        "remember cedar\n"
                        "<system-reminder>\nInjected harness context.\n"
                        "</system-reminder>"
                    ),
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        model_message = single_message_adapter.validate_json(message.message_json)
        assert isinstance(model_message, ModelRequest)
        self.assertEqual(model_message.parts[0].content, "remember cedar")

    def test_source_message_from_row_strips_task_notification_keeps_user_text(
        self,
    ) -> None:
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "mixed-notification",
                "message": {
                    "role": "user",
                    "content": (
                        "remember cedar\n"
                        "<task-notification>\nTask abc123 completed.\n"
                        "Full subagent report payload.\n"
                        "</task-notification>"
                    ),
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        model_message = single_message_adapter.validate_json(message.message_json)
        assert isinstance(model_message, ModelRequest)
        self.assertEqual(model_message.parts[0].content, "remember cedar")

    def test_source_message_from_row_drops_pure_task_notification(self) -> None:
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "pure-notification",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>\nTask abc123 completed.\n"
                        "Verbatim subagent report only.\n"
                        "</task-notification>"
                    ),
                },
            }
        )

        self.assertIsNone(message)

    def test_source_message_from_row_drops_pure_system_reminder(self) -> None:
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "pure-reminder",
                "message": {
                    "role": "user",
                    "content": (
                        "<system-reminder>\nInjected harness context only.\n"
                        "</system-reminder>"
                    ),
                },
            }
        )

        self.assertIsNone(message)

    def test_source_message_from_row_drops_unpaired_system_reminder_tag(
        self,
    ) -> None:
        unpaired_contents = [
            "<system-reminder>\ndangling open tag, no close",
            "dangling close tag only\n</system-reminder>",
            "remember cedar\n<system-reminder>\nunterminated block",
        ]

        for content in unpaired_contents:
            with self.subTest(content=content):
                message = source_message_from_claude_code_row(
                    {
                        "type": "user",
                        "sessionId": "session-1",
                        "uuid": "unpaired-reminder",
                        "message": {"role": "user", "content": content},
                    }
                )
                self.assertIsNone(message)

    def test_source_message_from_row_strips_multiple_task_notification_blocks(
        self,
    ) -> None:
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "multi-notification",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>\nTask one done.\n</task-notification>\n"
                        "remember cedar\n"
                        "<task-notification>\nTask two done.\n</task-notification>"
                    ),
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        model_message = single_message_adapter.validate_json(message.message_json)
        assert isinstance(model_message, ModelRequest)
        self.assertEqual(model_message.parts[0].content, "remember cedar")

    def test_source_message_from_row_drops_unbalanced_nested_task_notification(
        self,
    ) -> None:
        # A dangling nested open tag must not let inner payload survive the
        # non-greedy strip as apparent user text (fail closed on imbalance).
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "nested-dangling-open",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>outer "
                        "<task-notification>inner</task-notification>"
                        " leaked report"
                    ),
                },
            }
        )

        self.assertIsNone(message)

    def test_source_message_from_row_drops_balanced_nested_task_notification(
        self,
    ) -> None:
        # Balanced nesting leaves a dangling close tag after the non-greedy
        # strip; the surviving tag must fail closed.
        message = source_message_from_claude_code_row(
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "nested-balanced",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>outer "
                        "<task-notification>inner</task-notification>"
                        " trailing</task-notification>"
                    ),
                },
            }
        )

        self.assertIsNone(message)

    def test_source_message_from_row_drops_marker_created_by_stripping(
        self,
    ) -> None:
        # Stripping an inner task-notification block must not let a
        # manufactured system-reminder block (or vice versa) slip past the
        # final harness_envelope_reason check.
        interleaved_contents = [
            (
                "<system-remin<task-notification>x</task-notification>der>"
                "hidden</system-reminder>"
            ),
            (
                "<task-notif<system-reminder>x</system-reminder>ication>"
                "hidden</task-notification>"
            ),
        ]

        for content in interleaved_contents:
            with self.subTest(content=content):
                message = source_message_from_claude_code_row(
                    {
                        "type": "user",
                        "sessionId": "session-1",
                        "uuid": "interleaved-markers",
                        "message": {"role": "user", "content": content},
                    }
                )
                self.assertIsNone(message)

    def test_source_message_from_row_drops_task_notification_tag_variants(
        self,
    ) -> None:
        # Attribute or whitespace variants of the tag must still fail closed.
        variant_contents = [
            '<task-notification id="t1">payload</task-notification >',
            "<task-notification >payload</task-notification>",
        ]

        for content in variant_contents:
            with self.subTest(content=content):
                message = source_message_from_claude_code_row(
                    {
                        "type": "user",
                        "sessionId": "session-1",
                        "uuid": "variant-notification",
                        "message": {"role": "user", "content": content},
                    }
                )
                self.assertIsNone(message)

    def test_source_message_from_row_drops_unpaired_task_notification_tag(
        self,
    ) -> None:
        unpaired_contents = [
            "<task-notification>\ndangling open tag, no close",
            "dangling close tag only\n</task-notification>",
            "remember cedar\n<task-notification>\nunterminated block",
        ]

        for content in unpaired_contents:
            with self.subTest(content=content):
                message = source_message_from_claude_code_row(
                    {
                        "type": "user",
                        "sessionId": "session-1",
                        "uuid": "unpaired-notification",
                        "message": {"role": "user", "content": content},
                    }
                )
                self.assertIsNone(message)

    def test_scan_drops_envelopes_and_strips_reminders(self) -> None:
        rows = [
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "slash-command",
                "message": {
                    "role": "user",
                    "content": "<command-name>/clear</command-name>",
                },
            },
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "mixed-reminder",
                "message": {
                    "role": "user",
                    "content": (
                        "remember cedar\n"
                        "<system-reminder>injected</system-reminder>"
                    ),
                },
            },
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "pure-notification",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>subagent report</task-notification>"
                    ),
                },
            },
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "mixed-notification",
                "message": {
                    "role": "user",
                    "content": (
                        "remember birch\n"
                        "<task-notification>report</task-notification>"
                    ),
                },
            },
            {
                "type": "user",
                "sessionId": "session-1",
                "uuid": "clean",
                "message": {"role": "user", "content": "clean maple"},
            },
        ]

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            scan = scan_claude_code_transcript(path)

        self.assertEqual(scan.ignored, 2)
        texts = [
            single_message_adapter.validate_json(message.message_json).parts[0].content
            for message in scan.messages
        ]
        self.assertEqual(texts, ["remember cedar", "remember birch", "clean maple"])

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
