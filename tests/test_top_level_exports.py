"""Top-level ``vexic`` import surface (COA-284).

The package re-exports the light contract symbols eagerly and
``LocalMemoryService`` lazily (PEP 562), so ``import vexic`` must never drag
in ``vexic.service`` and its storage/pipeline dependencies.
"""

import subprocess
import sys
import unittest


class TopLevelExportTests(unittest.TestCase):
    def test_contract_symbols_and_service_importable(self) -> None:
        from vexic import (  # noqa: F401
            AppendTranscriptRequest,
            LocalMemoryService,
            MemoryCapability,
            MemoryScope,
            MemoryService,
            RedactionContext,
            SearchLongTermRequest,
            SearchTranscriptRequest,
        )
        from vexic.service import LocalMemoryService as direct

        self.assertIs(LocalMemoryService, direct)

    def test_dir_includes_lazy_export(self) -> None:
        import vexic

        self.assertIn("LocalMemoryService", dir(vexic))
        self.assertIn("MemoryScope", dir(vexic))

    def test_unknown_attribute_raises(self) -> None:
        import vexic

        with self.assertRaises(AttributeError):
            vexic.does_not_exist

    def test_import_vexic_does_not_load_service_module(self) -> None:
        # Fresh interpreter: this process may already have vexic.service loaded.
        code = (
            "import sys, vexic\n"
            "assert 'vexic.service' not in sys.modules, 'vexic.service loaded eagerly'\n"
            "assert 'pydantic_ai' not in sys.modules, 'pydantic_ai loaded eagerly'\n"
            "from vexic import LocalMemoryService\n"
            "assert 'vexic.service' in sys.modules\n"
        )
        subprocess.run([sys.executable, "-c", code], check=True)


if __name__ == "__main__":
    unittest.main()
