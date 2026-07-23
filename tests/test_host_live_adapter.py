from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Below this, a reasoning model can burn the whole budget thinking and emit
# nothing. Kept independent of the adapter's own constants so a bad edit there
# cannot move the goalposts.
REASONING_HEADROOM_FLOOR = 4096


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


def test_live_adapter_pins_no_data_collection_on_model_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Transcript text and stored fact text travel in these requests; routing
    # must be pinned to providers that do not retain or train on them rather
    # than inheriting whatever the OpenRouter account default allows.
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)

    for build in (adapter.build_extraction_agent, adapter.build_contradiction_agent):
        agent = build("retrieval-smoke")
        extra_body = agent.model_settings["extra_body"]
        assert extra_body["provider"]["data_collection"] == "deny"


def test_live_adapter_pins_no_data_collection_on_embedding_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_EMBEDDING_MODEL", raising=False)
    captured: dict[str, object] = {}

    class FakeEmbedder:
        def __init__(self, model: object) -> None:
            captured["model"] = model

        def embed_documents_sync(self, texts: list[str], *, settings: dict) -> object:
            captured["settings"] = settings

            class _Result:
                embeddings = [[0.0] * adapter.EMBEDDING_DIM for _ in texts]

            return _Result()

    monkeypatch.setattr(adapter, "Embedder", FakeEmbedder)

    adapter.embed_texts(["hello"])

    settings = captured["settings"]
    assert settings["extra_body"]["provider"]["data_collection"] == "deny"
    assert settings["dimensions"] == adapter.EMBEDDING_DIM


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


def test_live_adapter_exposes_all_four_symbols() -> None:
    adapter = _load_adapter()
    for name in (
        "embed_texts",
        "build_extraction_agent",
        "build_contradiction_agent",
        "build_summary_agent",
    ):
        assert callable(getattr(adapter, name, None)), name


def test_live_adapter_build_summary_agent_returns_agent_like_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    agent = adapter.build_summary_agent("summarize")

    assert agent.model_settings["extra_body"]["provider"]["data_collection"] == "deny"


def test_live_adapter_build_summary_agent_respects_env_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    captured: dict[str, str] = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("VEXIC_SUMMARY_MODEL", "  anthropic/claude-haiku-4.5\n")

    class _ChatModel:
        def __init__(self, model_name: str, *, provider: object) -> None:
            captured["model_name"] = model_name

    class _Agent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(adapter, "Agent", _Agent)
    monkeypatch.setattr(adapter, "OpenAIChatModel", _ChatModel)

    adapter.build_summary_agent("summarize")

    assert captured["model_name"] == "anthropic/claude-haiku-4.5"


def test_live_adapter_build_summary_agent_defaults_to_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    captured: dict[str, str] = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_SUMMARY_MODEL", raising=False)

    class _ChatModel:
        def __init__(self, model_name: str, *, provider: object) -> None:
            captured["model_name"] = model_name

    class _Agent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(adapter, "Agent", _Agent)
    monkeypatch.setattr(adapter, "OpenAIChatModel", _ChatModel)

    adapter.build_summary_agent("summarize")

    assert captured["model_name"] == "deepseek/deepseek-v4-pro"


def test_live_adapter_build_summary_agent_rejects_passed_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    with pytest.raises(
        RuntimeError,
        match="openrouter_live_adapter.py reads provider secrets from environment variables only.",
    ):
        adapter.build_summary_agent("summarize", secrets={"OPENROUTER_API_KEY": "x"})


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


def test_live_adapter_extraction_request_carries_raised_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wire-level proof for the dream-phase output cap: the extraction agent's
    # outgoing request must carry max_tokens, and the cap must leave room for
    # a reasoning model to think before emitting the structured candidate
    # list (512 starved deepseek/deepseek-v4-pro into finish_reason=length).
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)

    captured: dict[str, object] = {}

    class _RequestSeen(Exception):
        pass

    def record_request(messages: object, info: AgentInfo) -> object:
        captured["model_settings"] = info.model_settings
        raise _RequestSeen

    agent = adapter.build_extraction_agent("retrieval-smoke")
    with pytest.raises(_RequestSeen):
        agent.run_sync(
            "[message_id=1] user: hello", model=FunctionModel(record_request)
        )

    settings = captured["model_settings"]
    assert settings is not None, "no model settings reached the request"
    assert settings["max_tokens"] == 8192


def test_live_adapter_per_agent_output_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each dream agent gets an output budget that leaves a reasoning model room
    # to think before it emits: extraction a structured candidate list, summary
    # prose, contradiction a single boolean judgment. The judgment is small but
    # the reasoning ahead of it is not, so it needs extraction-sized headroom.
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_SUMMARY_MODEL", raising=False)

    assert (
        adapter.build_extraction_agent("retrieval-smoke").model_settings["max_tokens"]
        == 8192
    )
    assert (
        adapter.build_summary_agent("summarize").model_settings["max_tokens"] == 8192
    )
    assert (
        adapter.build_contradiction_agent("retrieval-smoke").model_settings[
            "max_tokens"
        ]
        == 8192
    )


def test_live_adapter_embedder_rejects_a_short_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The adapter validated embedding *dimensions* but never *count*. A provider
    # that returns fewer embeddings than inputs then flowed into
    # `zip(..., strict=True)` in the Light path and surfaced as a bare
    # ValueError -- indistinguishable from the libSQL storage faults that make
    # ValueError so ambiguous here. Fail at the boundary, naming the model.
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_EMBEDDING_MODEL", raising=False)

    class _ShortEmbedder:
        def __init__(self, model: object) -> None:
            pass

        def embed_documents_sync(self, texts: list[str], *, settings: dict) -> object:
            class _Result:
                # Two inputs, one embedding back.
                embeddings = [[0.0] * adapter.EMBEDDING_DIM]

            return _Result()

    monkeypatch.setattr(adapter, "Embedder", _ShortEmbedder)

    with pytest.raises(RuntimeError, match="one embedding per input"):
        adapter.embed_texts(["first text", "second text"])


def test_live_adapter_no_agent_caps_below_reasoning_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rule, not the numbers: the default model reasons before it emits, so
    # any cap sized to an agent's visible output starves it into
    # finish_reason=length. 512 killed extraction, then the same 512 killed
    # Deep's one-boolean contradiction judgment. A new agent that sizes its cap
    # by output length should fail here rather than in the hosted nightly dream.
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("VEXIC_LIVE_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.delenv("VEXIC_LIVE_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_SUMMARY_MODEL", raising=False)

    builders = (
        adapter.build_extraction_agent,
        adapter.build_contradiction_agent,
        adapter.build_summary_agent,
    )
    for build in builders:
        settings = build("retrieval-smoke").model_settings
        assert settings["max_tokens"] >= REASONING_HEADROOM_FLOOR, build.__name__

    # The recall judge is uncapped on purpose: a long structured verdict reason
    # must never truncate into a judge error.
    judge = adapter.build_longmemeval_recall_judge_agent("retrieval-smoke")
    assert "max_tokens" not in judge.model_settings


def test_live_adapter_max_output_tokens_env_overrides_all_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setenv("VEXIC_LIVE_MAX_OUTPUT_TOKENS", "1234")
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_RETRIEVAL_SMOKE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_SUMMARY_MODEL", raising=False)

    for agent in (
        adapter.build_extraction_agent("retrieval-smoke"),
        adapter.build_summary_agent("summarize"),
        adapter.build_contradiction_agent("retrieval-smoke"),
    ):
        assert agent.model_settings["max_tokens"] == 1234


def test_live_adapter_builds_longmemeval_recall_judge_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vexic.longmemeval import (
        LONGMEMEVAL_RECALL_JUDGE_PROMPT,
        LongMemEvalRecallJudgeVerdict,
    )

    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.setenv("VEXIC_LIVE_CLAUDE_MODEL", "anthropic/claude-sonnet-5")

    # Capture Agent construction kwargs instead of reaching into pydantic_ai
    # internals, so the assertions survive upstream attribute renames.
    captured: dict[str, object] = {}

    class _RecordingAgent:
        def __init__(self, model: object, **kwargs: object) -> None:
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr(adapter, "Agent", _RecordingAgent)

    adapter.build_longmemeval_recall_judge_agent("claude")

    assert captured["output_type"] is LongMemEvalRecallJudgeVerdict
    assert captured["instructions"] == LONGMEMEVAL_RECALL_JUDGE_PROMPT
    settings = captured["model_settings"]
    assert settings["temperature"] == 0
    assert "max_tokens" not in settings


def test_live_adapter_judge_agent_requires_explicit_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("VEXIC_LIVE_MODEL", raising=False)
    monkeypatch.delenv("VEXIC_LIVE_CLAUDE_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="VEXIC_LIVE_CLAUDE_MODEL"):
        adapter.build_longmemeval_recall_judge_agent("claude")


def test_live_adapter_judge_agent_rejects_passed_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_adapter()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    with pytest.raises(RuntimeError, match="reads provider secrets from environment variables only"):
        adapter.build_longmemeval_recall_judge_agent(
            "claude", secrets={"OPENROUTER_API_KEY": "x"}
        )


def _extraction_instructions_normalized() -> str:
    """Extraction instructions, lowercased with whitespace collapsed.

    The prompt is hard-wrapped prose, so a rule can straddle a line break.
    Asserting on the collapsed form lets these tests pin the *rule* rather
    than the current line wrapping (ADR 0039, option A).
    """
    import re

    adapter = _load_adapter()
    return re.sub(r"\s+", " ", adapter.EXTRACTION_INSTRUCTIONS.lower()).strip()


def test_extraction_instructions_route_named_entities_into_subject() -> None:
    # ADR 0039: the extraction prompt gave no guidance on the subject field, so
    # the model emitted "User" for nearly every fact and real entities never
    # got their own bucket. The prompt must name the subject field and tell the
    # model to use the specific named entity a fact is about.
    instructions = _extraction_instructions_normalized()

    assert "subject" in instructions
    assert "named entity" in instructions


def test_extraction_instructions_reserve_user_subject_for_user_scoped_facts() -> None:
    # The other half of ADR 0039 option A: "User" must stop being the default
    # bucket. The prompt has to say the subject "User" is reserved for facts
    # genuinely about the user themselves.
    instructions = _extraction_instructions_normalized()

    assert "reserve" in instructions
    assert '"user"' in instructions
    assert "about the user themselves" in instructions


def test_extraction_instructions_keep_fact_text_self_contained_under_subject_guidance() -> None:
    # Failure mode the subject guidance could introduce: the model treats
    # subject as where the entity lives and drops it from fact_text, which is
    # the field retrieval and scoring actually read. Subject is a key, not a
    # substitute for the statement.
    instructions = _extraction_instructions_normalized()

    assert "fact_text" in instructions
    assert "self-contained" in instructions
