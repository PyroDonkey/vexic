from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.pipeline import _main, run_light_phase
from vexic.ports import HostPortNotConfigured
from vexic.storage import init_db, save_messages


class PipelineEmbeddingPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_light_phase_requires_explicit_embedding_port_before_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            save_messages(
                db_path,
                [ModelRequest(parts=[UserPromptPart(content="I prefer compact reports.")])],
            )
            agent_factory_called = False

            def agent_factory(model_group: str, secrets: object = None) -> object:
                nonlocal agent_factory_called
                agent_factory_called = True
                return SimpleNamespace()

            with self.assertRaisesRegex(HostPortNotConfigured, "Embeddings"):
                await run_light_phase(
                    db_path,
                    "glm",
                    extraction_agent_factory=agent_factory,
                )

            self.assertFalse(agent_factory_called)


class PipelineCliTests(unittest.TestCase):
    def test_cli_without_embedding_adapter_exits_with_configuration_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            stderr = StringIO()
            argv = ["vexic.pipeline", "--db", db_path, "--model-group", "glm"]

            with (
                patch.object(sys, "argv", argv),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as caught,
            ):
                _main()

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Embeddings requires a host-supplied model port", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
