from __future__ import annotations

import argparse
import asyncio
import inspect
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import ModuleType
from typing import Any, Iterable, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import UsageLimits

from vexic.contract import (
    AppendTranscriptRequest,
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    RedactionContext,
    SearchLongTermRequest,
    TrustBoundary,
)
from vexic.deep import run_deep_phase
from vexic.pipeline import run_light_phase
from vexic.rem import run_rem_phase
from vexic.service import LocalMemoryService
from vexic.storage import single_message_adapter
from vexic.usage import summarize_agent_usage


class BaselineConfigError(ValueError):
    pass


class FixtureTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = "user"
    content: str

    @field_validator("content")
    @classmethod
    def _content_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


TranscriptTurn = str | FixtureTurn


class FixtureRow(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    row_id: str = Field(alias="id")
    transcript: list[TranscriptTurn] = Field(min_length=1)
    question: str
    expected_fact: str

    @field_validator("row_id", "question", "expected_fact")
    @classmethod
    def _text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


FixtureRow.model_rebuild()


class ProviderBudget:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.used = 0

    def remaining(self) -> int:
        return self.max_calls - self.used

    def ensure_available(self, kind: str) -> None:
        if self.remaining() <= 0:
            raise RuntimeError(f"Provider call cap exceeded before {kind}.")

    def spend(self, kind: str, count: int = 1) -> None:
        self.used += max(count, 1)
        if self.used > self.max_calls:
            raise RuntimeError(
                f"Provider call cap exceeded after {kind}: "
                f"{self.used}/{self.max_calls}."
            )


class CountingAgent:
    def __init__(self, agent: Any, budget: ProviderBudget) -> None:
        self.agent = agent
        self.budget = budget

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        self.budget.ensure_available("agent.run")
        request_limit = self.budget.remaining()
        if _accepts_kwarg(self.agent.run, "usage_limits"):
            kwargs.setdefault(
                "usage_limits",
                UsageLimits(request_limit=request_limit),
            )
        self.budget.spend("agent.run attempt")
        result = self.agent.run(*args, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
        additional_requests = max(summarize_agent_usage(result).model_requests, 1) - 1
        if additional_requests:
            self.budget.spend("agent.run", additional_requests)
        return result


def _accepts_kwarg(func: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the opt-in live Vexic retrieval baseline smoke."
    )
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--fixture")
    parser.add_argument("--adapter")
    parser.add_argument("--provider")
    parser.add_argument("--model-group")
    parser.add_argument("--output-dir")
    parser.add_argument("--max-rows", type=int, default=1)
    parser.add_argument("--max-messages-per-row", type=int, default=4)
    parser.add_argument("--max-transcript-chars", type=int, default=4000)
    parser.add_argument("--max-provider-calls", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--top-n", type=int, default=1)
    parser.add_argument("--neighbor-k", type=int, default=1)
    parser.add_argument("--limit", type=int, default=5)
    return parser


def _require(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise BaselineConfigError(f"{name} is required with --allow-live.")
    return value


def _turn_text(turn: object) -> str:
    if isinstance(turn, str):
        return turn
    if isinstance(turn, FixtureTurn):
        return turn.content
    if isinstance(turn, dict) and isinstance(turn.get("content"), str):
        return str(turn["content"])
    raise BaselineConfigError("transcript entries must be strings or role/content objects.")


def _message_from_turn(turn: object) -> ModelRequest | ModelResponse:
    if isinstance(turn, str):
        return ModelRequest(parts=[UserPromptPart(content=turn)])
    if isinstance(turn, FixtureTurn):
        if turn.role == "assistant":
            return ModelResponse(parts=[TextPart(content=turn.content)])
        return ModelRequest(parts=[UserPromptPart(content=turn.content)])
    if not isinstance(turn, dict):
        raise BaselineConfigError("transcript entries must be strings or role/content objects.")
    role = str(turn.get("role", "user")).lower()
    content = _turn_text(turn)
    if role == "assistant":
        return ModelResponse(parts=[TextPart(content=content)])
    if role == "user":
        return ModelRequest(parts=[UserPromptPart(content=content)])
    raise BaselineConfigError("transcript role must be user or assistant.")


def _load_fixture(path: Path, *, max_rows: int | None = None) -> list[FixtureRow]:
    if not path.exists():
        raise BaselineConfigError(f"fixture not found: {path}")
    rows: list[FixtureRow] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(FixtureRow.model_validate_json(line))
            except ValidationError as exc:
                first = exc.errors()[0]
                location = ".".join(str(part) for part in first["loc"])
                detail = f"{location}: {first['msg']}" if location else first["msg"]
                raise BaselineConfigError(
                    f"fixture line {line_number} is invalid: {detail}."
                ) from exc
            if max_rows is not None and len(rows) > max_rows:
                raise BaselineConfigError(
                    f"fixture has more than --max-rows {max_rows}."
                )
    if not rows:
        raise BaselineConfigError("fixture must contain at least one row.")
    return rows


def _validate_numeric_caps(args: argparse.Namespace) -> None:
    for name in (
        "max_rows",
        "max_messages_per_row",
        "max_transcript_chars",
        "max_provider_calls",
        "timeout_seconds",
        "top_n",
        "neighbor_k",
        "limit",
    ):
        if getattr(args, name) <= 0:
            raise BaselineConfigError(f"--{name.replace('_', '-')} must be greater than 0.")


def _validate_caps(args: argparse.Namespace, rows: list[FixtureRow]) -> None:
    _validate_numeric_caps(args)
    if len(rows) > args.max_rows:
        raise BaselineConfigError(
            f"fixture has {len(rows)} rows, above --max-rows {args.max_rows}."
        )
    for row in rows:
        if len(row.transcript) > args.max_messages_per_row:
            raise BaselineConfigError(
                f"row {row.row_id} exceeds --max-messages-per-row {args.max_messages_per_row}."
            )
        text_chars = sum(len(_turn_text(turn)) for turn in row.transcript)
        if text_chars > args.max_transcript_chars:
            raise BaselineConfigError(
                f"row {row.row_id} exceeds --max-transcript-chars {args.max_transcript_chars}."
            )
    estimated = estimate_provider_calls(len(rows), args.top_n, args.neighbor_k)
    if estimated > args.max_provider_calls:
        raise BaselineConfigError(
            f"estimated provider calls {estimated} exceed --max-provider-calls "
            f"{args.max_provider_calls}."
        )


def estimate_provider_calls(row_count: int, top_n: int, neighbor_k: int) -> int:
    deep_pair_calls = top_n * (top_n - 1) // 2
    calls_per_row = 1 + 1 + (top_n * neighbor_k) + deep_pair_calls + 3
    return row_count * calls_per_row


def _load_adapter(path: Path) -> ModuleType:
    if not path.exists():
        raise BaselineConfigError(f"adapter not found: {path}")
    module_name = f"vexic_live_adapter_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise BaselineConfigError(f"could not load adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for name in (
        "build_extraction_agent",
        "build_rem_agent",
        "build_contradiction_agent",
        "embed_texts",
    ):
        if not callable(getattr(module, name, None)):
            raise BaselineConfigError(f"adapter must define callable {name}.")
    return module


def _validate_adapter_provider(provider: str, adapter: ModuleType) -> None:
    adapter_provider = getattr(adapter, "PROVIDER", None)
    if adapter_provider is None:
        return
    if not isinstance(adapter_provider, str) or not adapter_provider.strip():
        raise BaselineConfigError("adapter PROVIDER must be a non-empty string.")
    adapter_provider = adapter_provider.strip()
    provider = provider.strip()
    if adapter_provider.lower() != provider.lower():
        raise BaselineConfigError(
            f"adapter provider {adapter_provider} does not match --provider {provider}."
        )


def _wrap_factory(factory: Any, budget: ProviderBudget) -> Any:
    def counted_factory(model_group: str, secrets: dict[str, str] | None = None) -> CountingAgent:
        return CountingAgent(factory(model_group, secrets=secrets), budget)

    return counted_factory


def _wrap_embed(embed_texts: Any, budget: ProviderBudget) -> Any:
    def counted_embed(texts: list[str]) -> list[list[float]]:
        if not texts:
            return embed_texts(texts)
        budget.ensure_available("embed_texts")
        budget.spend("embed_texts")
        return embed_texts(texts)

    return counted_embed


def _scope(tenant_id: str) -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant_id,
        session_id="baseline",
        principal=Principal(
            principal_id="live-retrieval-baseline",
            principal_type=PrincipalType.OPERATOR,
        ),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
    )


def _diagnostics(
    db_path: Path,
    *,
    facts: Iterable[str] = (),
    candidate_notes: Iterable[str] = (),
) -> dict[str, object]:
    counts = {
        "tier1_found": 0,
        "tier2_extracted": 0,
        "tier3_promoted": 0,
        "tier3_retrieved": bool(list(facts)),
        "candidate_fallback_used": bool(list(candidate_notes)),
    }
    if not db_path.exists():
        return counts
    with sqlite3.connect(db_path) as conn:
        counts["tier1_found"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        counts["tier2_extracted"] = conn.execute(
            "SELECT COUNT(*) FROM memory_candidates"
        ).fetchone()[0]
        counts["tier3_promoted"] = conn.execute(
            "SELECT COUNT(*) FROM long_term_memory WHERE retired = 0"
        ).fetchone()[0]
    return counts


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def _contains_expected(values: Iterable[str] | None, expected_fact: str | None) -> bool:
    if not expected_fact:
        return True
    expected = _normalized(expected_fact)
    return any(expected in _normalized(value) for value in values or ())


def classify_failure(
    *,
    provider_error: bool = False,
    tier2_count: int,
    tier3_count: int,
    facts: Iterable[str] | None = None,
    candidate_notes: Iterable[str] | None = None,
    expected_fact: str | None = None,
    synthesis_failed: bool = False,
) -> str | None:
    if provider_error:
        return "provider_runtime_failure"
    if tier2_count <= 0:
        return "extraction_miss"
    if tier3_count > 0 and _contains_expected(facts, expected_fact):
        return "judge_synthesis_issue" if synthesis_failed else None
    if candidate_notes:
        return "candidate_fallback"
    if tier3_count <= 0:
        return "promotion_miss"
    if not _contains_expected(facts, expected_fact):
        return "retrieval_miss"
    if synthesis_failed:
        return "judge_synthesis_issue"
    return None


async def _append_transcript(
    service: LocalMemoryService,
    scope: MemoryScope,
    row: FixtureRow,
) -> None:
    messages = [
        single_message_adapter.dump_json(_message_from_turn(turn)).decode()
        for turn in row.transcript
    ]
    await service.append_transcript(
        AppendTranscriptRequest(
            scope=scope,
            redaction=RedactionContext(forbidden_values=()),
            messages_json=messages,
        )
    )


async def _run_row_inner(
    *,
    db_path: Path,
    row: FixtureRow,
    args: argparse.Namespace,
    adapter: ModuleType,
    budget: ProviderBudget,
) -> dict[str, object]:
    tenant_id = f"live-baseline-{uuid.uuid4().hex}"
    scope = _scope(tenant_id)
    embed = _wrap_embed(adapter.embed_texts, budget)
    service = LocalMemoryService(db_path=str(db_path), tenant_id=tenant_id, embed=embed)
    service.init_schema()
    await _append_transcript(service, scope, row)
    await run_light_phase(
        str(db_path),
        args.model_group,
        batch_size=len(row.transcript),
        extraction_agent_factory=_wrap_factory(adapter.build_extraction_agent, budget),
        embed=embed,
    )
    await run_rem_phase(
        str(db_path),
        args.model_group,
        rem_agent_factory=_wrap_factory(adapter.build_rem_agent, budget),
    )
    await run_deep_phase(
        str(db_path),
        args.model_group,
        top_n=args.top_n,
        neighbor_k=args.neighbor_k,
        contradiction_agent_factory=_wrap_factory(
            adapter.build_contradiction_agent,
            budget,
        ),
    )
    result = await service.search_long_term(
        SearchLongTermRequest(scope=scope, query=row.question, limit=args.limit)
    )
    facts = [fact.fact_text for fact in result.facts]
    notes = [note.fact_text for note in result.candidate_notes]
    diagnostics = _diagnostics(db_path, facts=facts, candidate_notes=notes)
    failure_type = classify_failure(
        tier2_count=int(diagnostics["tier2_extracted"]),
        tier3_count=int(diagnostics["tier3_promoted"]),
        facts=facts,
        candidate_notes=notes,
        expected_fact=row.expected_fact,
    )
    return {
        "id": row.row_id,
        "failure_type": failure_type,
        "diagnostics": diagnostics,
        "facts": [fact.model_dump(mode="json") for fact in result.facts],
        "candidate_notes": [note.model_dump(mode="json") for note in result.candidate_notes],
    }


async def _run_row(
    row: FixtureRow,
    args: argparse.Namespace,
    adapter: ModuleType,
    budget: ProviderBudget,
) -> dict[str, object]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="vexic-live-baseline-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        db_path = Path(temp_dir) / "memory.db"
        try:
            metric = await asyncio.wait_for(
                _run_row_inner(
                    db_path=db_path,
                    row=row,
                    args=args,
                    adapter=adapter,
                    budget=budget,
                ),
                timeout=args.timeout_seconds,
            )
        except Exception as exc:
            diagnostics = _diagnostics(db_path)
            metric = {
                "id": row.row_id,
                "failure_type": "provider_runtime_failure",
                "diagnostics": diagnostics,
                "facts": [],
                "candidate_notes": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        metric["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return metric


async def _run_live(
    rows: list[FixtureRow],
    args: argparse.Namespace,
    adapter: ModuleType,
) -> dict[str, object]:
    budget = ProviderBudget(args.max_provider_calls)
    started = time.monotonic()
    metrics = [await _run_row(row, args, adapter, budget) for row in rows]
    return {
        "provider": args.provider,
        "model_group": args.model_group,
        "caps": {
            "max_rows": args.max_rows,
            "max_messages_per_row": args.max_messages_per_row,
            "max_transcript_chars": args.max_transcript_chars,
            "max_provider_calls": args.max_provider_calls,
            "timeout_seconds": args.timeout_seconds,
            "top_n": args.top_n,
            "neighbor_k": args.neighbor_k,
        },
        "provider_calls_used": budget.used,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rows": metrics,
    }


def _write_artifacts(output_dir: Path, retrieval: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "retrieval_metrics.json").write_text(
        json.dumps(retrieval, indent=2, sort_keys=True) + "\n"
    )
    (output_dir / "answer_synthesis_metrics.json").write_text(
        json.dumps(
            {
                "status": "not_run",
                "reason": "retrieval-only live smoke; answer synthesis is not enabled",
                "failure_taxonomy": ["judge_synthesis_issue"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if not args.allow_live:
        print("Live retrieval baseline skipped; pass --allow-live to run provider calls.")
        return 0

    try:
        fixture = Path(_require(args.fixture, "--fixture"))
        adapter_path = Path(_require(args.adapter, "--adapter"))
        provider = _require(args.provider, "--provider")
        _require(args.model_group, "--model-group")
        output_dir = Path(_require(args.output_dir, "--output-dir"))
        _validate_numeric_caps(args)
        rows = _load_fixture(fixture, max_rows=args.max_rows)
        _validate_caps(args, rows)
        adapter = _load_adapter(adapter_path)
        _validate_adapter_provider(provider, adapter)
        retrieval = asyncio.run(_run_live(rows, args, adapter))
        _write_artifacts(output_dir, retrieval)
    except BaselineConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"live retrieval baseline failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 0 if all(row["failure_type"] is None for row in retrieval["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
