from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.storage import init_db, save_messages
from vexic.tools import expand_history, search_long_term, search_memory


class ToolConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_long_term_without_embedder_returns_configuration_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            ctx = SimpleNamespace(
                deps=SimpleNamespace(
                    db_path=db_path,
                    session_id="default",
                    secrets={},
                    authority=None,
                    retrieved_facts_this_turn=[],
                ),
                usage=None,
            )

            result = await search_long_term(ctx, "compact reports")

        self.assertIn("pip install vexic[local-embed]", result)


class ToolAgentScopeTests(unittest.TestCase):
    def test_transcript_helpers_honor_dependency_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            message_ids = {
                agent_id: save_messages(
                    db_path,
                    [ModelRequest(parts=[UserPromptPart(content=content)])],
                    agent_id=agent_id,
                )[0]
                for agent_id, content in (
                    ("agent-a", "cedar agent a transcript"),
                    ("agent-b", "cedar agent b transcript"),
                    (None, "cedar shared transcript"),
                )
            }
            deps = SimpleNamespace(
                db_path=db_path,
                session_id="default",
                agent_id="agent-a",
                secrets={},
            )

            search_result = search_memory(deps, "cedar")
            history_result = expand_history(
                deps,
                min(message_ids.values()),
                max(message_ids.values()),
            )
            shared_deps = SimpleNamespace(
                db_path=db_path,
                session_id="default",
                secrets={},
            )
            shared_search_result = search_memory(shared_deps, "cedar")
            shared_history_result = expand_history(
                shared_deps,
                min(message_ids.values()),
                max(message_ids.values()),
            )

        self.assertIn("agent a transcript", search_result)
        self.assertNotIn("agent b transcript", search_result)
        self.assertNotIn("shared transcript", search_result)
        self.assertIn("agent a transcript", history_result)
        self.assertNotIn("agent b transcript", history_result)
        self.assertNotIn("shared transcript", history_result)
        self.assertIn("shared transcript", shared_search_result)
        self.assertNotIn("agent a transcript", shared_search_result)
        self.assertNotIn("agent b transcript", shared_search_result)
        self.assertIn("shared transcript", shared_history_result)
        self.assertNotIn("agent a transcript", shared_history_result)
        self.assertNotIn("agent b transcript", shared_history_result)


if __name__ == "__main__":
    unittest.main()
