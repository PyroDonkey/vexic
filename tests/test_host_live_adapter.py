from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_adapter() -> object:
    spec = importlib.util.spec_from_file_location(
        "openrouter_live_adapter_under_test",
        ROOT / "adapters" / "openrouter_live_adapter.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_adapter_rejects_blank_openrouter_key(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "  \t")

    with pytest.raises(
        RuntimeError,
        match="OPENROUTER_API_KEY is required for the live retrieval adapter.",
    ):
        adapter.build_extraction_agent("retrieval-smoke")


@pytest.mark.parametrize("timeout", ["nan", "inf"])
def test_live_adapter_rejects_non_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS", timeout)
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)

    with pytest.raises(
        RuntimeError,
        match="VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS must be a finite number greater than 0.",
    ):
        adapter.build_extraction_agent("retrieval-smoke")


def test_live_adapter_rejects_openai_provider_model_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("VEXIC_LIVE_MODEL", "openai:gpt-4o-mini")

    with pytest.raises(
        RuntimeError,
        match="VEXIC_LIVE_MODEL must use an OpenRouter model id like",
    ):
        adapter.build_extraction_agent("retrieval-smoke")


def test_live_adapter_strips_model_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    captured: dict[str, str] = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("VEXIC_LIVE_MODEL", "  openai/gpt-4o-mini\n")

    class _ChatModel:
        def __init__(self, model_name: str, *, provider: object) -> None:
            captured["model_name"] = model_name

    class _Agent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(adapter, "Agent", _Agent)
    monkeypatch.setattr(adapter, "OpenAIChatModel", _ChatModel)

    adapter.build_extraction_agent("retrieval-smoke")

    assert captured["model_name"] == "openai/gpt-4o-mini"


def test_live_adapter_defaults_to_deepseek_v4_pro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    captured: dict[str, str] = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)

    class _ChatModel:
        def __init__(self, model_name: str, *, provider: object) -> None:
            captured["model_name"] = model_name

    class _Agent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(adapter, "Agent", _Agent)
    monkeypatch.setattr(adapter, "OpenAIChatModel", _ChatModel)

    adapter.build_extraction_agent("retrieval-smoke")

    assert captured["model_name"] == "deepseek/deepseek-v4-pro"


def test_live_adapter_embedding_can_run_inside_active_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    class _Result:
        embeddings = [[1.0] + [0.0] * 383]

    class _Embedder:
        def __init__(self, model: object) -> None:
            self.model = model

        def embed_documents_sync(self, texts: list[str], *, settings: object) -> _Result:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return _Result()
            raise RuntimeError("called inside active event loop")

    monkeypatch.setattr(adapter, "Embedder", _Embedder)

    async def run() -> list[list[float]]:
        return adapter.embed_texts(["hello"])

    assert asyncio.run(run()) == [[1.0] + [0.0] * 383]
