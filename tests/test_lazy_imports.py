import os
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from math import sqrt
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import vexic.embeddings as embeddings
from vexic.ports import HostPortNotConfigured


ROOT = Path(__file__).resolve().parents[1]


class VexicLazyImportTests(unittest.TestCase):
    def test_importing_vexic_does_not_load_sentence_transformers(self) -> None:
        script = (
            "import importlib, sys; "
            "importlib.import_module('vexic'); "
            "raise SystemExit(1 if 'sentence_transformers' in sys.modules else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def tearDown(self) -> None:
        embeddings._EMBEDDING_MODEL = None
        sys.modules.pop("fastembed", None)

    def test_embed_texts_empty_input_does_not_import_fastembed(self) -> None:
        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "fastembed":
                raise AssertionError("fastembed should not load for empty input")
            return original_import(name, *args, **kwargs)

        original_import = __import__
        with patch("builtins.__import__", side_effect=blocked_import):
            self.assertEqual(embeddings.embed_texts([]), [])

    def test_embed_texts_without_local_extra_names_install_command(self) -> None:
        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "fastembed":
                raise ModuleNotFoundError("No module named 'fastembed'")
            return original_import(name, *args, **kwargs)

        original_import = __import__
        with (
            patch("builtins.__import__", side_effect=blocked_import),
            self.assertRaisesRegex(
                HostPortNotConfigured,
                r"requires a host-supplied model port.*pip install vexic\[local-embed\]",
            ),
        ):
            embeddings.embed_texts(["compact reports"])

    def test_embed_texts_uses_fastembed_lazily_and_normalizes(self) -> None:
        fake = ModuleType("fastembed")
        model_names: list[str] = []

        class TextEmbedding:
            def __init__(self, model_name: str) -> None:
                model_names.append(model_name)

            def embed(self, texts: list[str]) -> object:
                for text in texts:
                    if text == "first":
                        yield [3.0, 4.0] + [0.0] * (embeddings.EMBEDDING_DIM - 2)
                    else:
                        yield [0.0, 0.0, 5.0] + [0.0] * (embeddings.EMBEDDING_DIM - 3)

        fake.TextEmbedding = TextEmbedding
        sys.modules["fastembed"] = fake

        vectors = embeddings.embed_texts(["first", "second"])
        cached_vectors = embeddings.embed_texts(["first"])

        self.assertEqual(model_names, [embeddings.EMBEDDING_MODEL_NAME])
        self.assertEqual([len(vector) for vector in vectors], [embeddings.EMBEDDING_DIM] * 2)
        self.assertAlmostEqual(vectors[0][0], 0.6)
        self.assertAlmostEqual(vectors[0][1], 0.8)
        self.assertAlmostEqual(vectors[1][2], 1.0)
        self.assertTrue(
            all(
                abs(sqrt(sum(value * value for value in vector)) - 1.0) < 1e-12
                for vector in [*vectors, *cached_vectors]
            )
        )

    def test_embed_texts_initializes_fastembed_once_under_concurrency(self) -> None:
        fake = ModuleType("fastembed")
        model_names: list[str] = []
        start = threading.Event()

        class TextEmbedding:
            def __init__(self, model_name: str) -> None:
                model_names.append(model_name)
                time.sleep(0.05)

            def embed(self, texts: list[str]) -> object:
                vector = [1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 1)
                return [vector for _ in texts]

        fake.TextEmbedding = TextEmbedding
        sys.modules["fastembed"] = fake

        def embed_once() -> list[list[float]]:
            start.wait(timeout=1)
            return embeddings.embed_texts(["first"])

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(embed_once) for _ in range(8)]
            start.set()
            vectors = [future.result(timeout=2) for future in futures]

        self.assertEqual(model_names, [embeddings.EMBEDDING_MODEL_NAME])
        self.assertEqual(
            vectors,
            [[[1.0] + [0.0] * (embeddings.EMBEDDING_DIM - 1)] for _ in range(8)],
        )


if __name__ == "__main__":
    unittest.main()
