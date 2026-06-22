import asyncio
from collections.abc import Mapping
import traceback
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from vexic.models import FactCandidate
from vexic.ports import AgentFactory, EmbedTexts, HostPortNotConfigured, missing_host_port
from vexic.storage import (
    backfill_missing_candidate_embeddings,
    commit_dream_cycle,
    get_watermark,
    init_db,
    load_candidates_missing_embeddings,
    load_messages_since,
)
from vexic.timeutil import utc_now_iso
from vexic.usage import summarize_agent_usage

LIGHT_PHASE_BATCH_SIZE = 50


def build_extraction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Any:
    raise missing_host_port("Light extraction")


def render_transcript(rows: list[tuple[int, ModelMessage]]) -> str:
    lines: list[str] = []
    for message_id, msg in rows:
        lines.extend(_render_message_lines(message_id, msg))
    return "\n".join(lines)


def rendered_message_ids(rows: list[tuple[int, ModelMessage]]) -> list[int]:
    return [
        message_id
        for message_id, msg in rows
        if _render_message_lines(message_id, msg)
    ]


def _render_message_lines(message_id: int, msg: ModelMessage) -> list[str]:
    lines: list[str] = []
    if isinstance(msg, ModelRequest):
        for part in msg.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                lines.append(f"[message_id={message_id}] User: {part.content}")
    elif isinstance(msg, ModelResponse):
        for part in msg.parts:
            if isinstance(part, TextPart):
                lines.append(f"[message_id={message_id}] Assistant: {part.content}")
    return lines


def validate_candidate_source_ids(
    candidates: list[FactCandidate],
    allowed_message_ids: list[int],
) -> None:
    allowed = set(allowed_message_ids)
    for candidate in candidates:
        candidate_ids = set(candidate.source_message_ids)
        if not candidate_ids:
            raise ValueError(
                f"Candidate source_message_ids must be non-empty: {candidate.fact_text!r}"
            )
        invalid_ids = sorted(candidate_ids - allowed)
        if invalid_ids:
            raise ValueError(
                "Candidate source_message_ids must refer to messages in the current window. "
                f"Invalid IDs for {candidate.fact_text!r}: {invalid_ids}"
            )
        candidate.source_message_ids = sorted(candidate_ids)


def _forbidden_secret_values(secrets: Mapping[str, str] | None) -> list[str]:
    if secrets is None:
        return []
    return list(secrets.values())


async def run_light_phase(
    db_path: str,
    model_group: str,
    batch_size: int = LIGHT_PHASE_BATCH_SIZE,
    agent_id: str | None = None,
    secrets: Mapping[str, str] | None = None,
    extraction_agent_factory: AgentFactory | None = None,
    embed: EmbedTexts | None = None,
) -> None:
    started_at = utc_now_iso()
    watermark = 0
    forbidden = _forbidden_secret_values(secrets)
    agent_factory = extraction_agent_factory or build_extraction_agent
    if embed is None:
        raise missing_host_port("Embeddings")
    embedder = embed

    try:
        init_db(db_path)
        watermark = get_watermark(db_path, agent_id=agent_id)

        rows = load_messages_since(
            db_path,
            watermark,
            limit=batch_size,
            agent_id=agent_id,
            exclude_session_prefixes=("onboarding:",),
        )
        if not rows:
            missing_embeddings = load_candidates_missing_embeddings(db_path)
            if missing_embeddings:
                backfill_embeddings = embedder([fact_text for _, fact_text in missing_embeddings])
                backfill_missing_candidate_embeddings(
                    db_path,
                    list(zip([candidate_id for candidate_id, _ in missing_embeddings], backfill_embeddings, strict=True)),
                    forbidden_secret_values=forbidden,
                )
            commit_dream_cycle(
                db_path,
                [],
                agent_id=agent_id,
                status="ok",
                started_at=started_at,
                finished_at=utc_now_iso(),
                messages_processed=0,
                last_processed_message_id=watermark,
                forbidden_secret_values=forbidden,
            )
            print("Light phase: no new messages. No-op.")
            return

        window_ids = [msg_id for msg_id, _ in rows]
        transcript = render_transcript(rows)
        evidence_ids = rendered_message_ids(rows)

        agent = agent_factory(model_group, secrets=secrets)
        result = await agent.run(transcript)
        usage = summarize_agent_usage(result)
        candidates = result.output
        validate_candidate_source_ids(candidates, evidence_ids)

        missing_embeddings = load_candidates_missing_embeddings(db_path)
        if missing_embeddings:
            backfill_embeddings = embedder([fact_text for _, fact_text in missing_embeddings])
            backfill_missing_candidate_embeddings(
                db_path,
                list(zip([candidate_id for candidate_id, _ in missing_embeddings], backfill_embeddings, strict=True)),
                forbidden_secret_values=forbidden,
            )

        candidate_embeddings = embedder([candidate.fact_text for candidate in candidates])
        commit_dream_cycle(
            db_path,
            candidates,
            agent_id=agent_id,
            status="ok",
            started_at=started_at,
            finished_at=utc_now_iso(),
            messages_processed=len(rows),
            last_processed_message_id=max(window_ids),
            candidate_embeddings=candidate_embeddings,
            model_requests=usage.model_requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_micros=usage.estimated_cost_micros,
            forbidden_secret_values=forbidden,
        )
        print(f"Light phase: {len(rows)} messages -> {len(candidates)} extracted candidates.")

    except Exception as exc:
        commit_dream_cycle(
            db_path,
            [],
            agent_id=agent_id,
            status="error",
            started_at=started_at,
            finished_at=utc_now_iso(),
            messages_processed=0,
            last_processed_message_id=watermark,
            error_detail=traceback.format_exc(),
            forbidden_secret_values=forbidden,
        )
        print(f"Light phase: ERROR -- {exc}. Watermark held; will retry.")
        raise


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Light memory extraction phase once.")
    parser.add_argument("--db", required=True, help="Path to a Vexic SQLite memory database.")
    parser.add_argument("--model-group", required=True, help="Host model group label.")
    parser.add_argument("--agent-id", help="Optional agent memory scope. Omit for shared scope.")
    args = parser.parse_args()

    try:
        asyncio.run(run_light_phase(args.db, args.model_group, agent_id=args.agent_id))
    except HostPortNotConfigured as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    _main()
