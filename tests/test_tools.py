from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vexic.storage import init_db
from vexic.tools import search_long_term


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

        self.assertIn("Embeddings requires a host-supplied model port", result)


if __name__ == "__main__":
    unittest.main()
