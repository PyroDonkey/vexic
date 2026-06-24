from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_adapter() -> object:
    spec = importlib.util.spec_from_file_location(
        "openai_live_adapter_under_test",
        ROOT / "adapters" / "openai_live_adapter.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_adapter_rejects_blank_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENAI_API_KEY", "  \t")

    with pytest.raises(
        RuntimeError,
        match="OPENAI_API_KEY is required for the live retrieval adapter.",
    ):
        adapter.build_extraction_agent("retrieval-smoke")


@pytest.mark.parametrize("timeout", ["nan", "inf"])
def test_live_adapter_rejects_non_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS", timeout)
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)

    with pytest.raises(
        RuntimeError,
        match="VEXIC_LIVE_REQUEST_TIMEOUT_SECONDS must be a finite number greater than 0.",
    ):
        adapter.build_extraction_agent("retrieval-smoke")
