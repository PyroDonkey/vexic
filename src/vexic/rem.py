"""REM phase: cluster active Tier 2 candidates and write boost signals only."""

import asyncio
import traceback
from collections.abc import Mapping
from typing import Any

from vexic.ports import AgentFactory, missing_host_port
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import RemCandidate, commit_rem_cycle, load_rem_candidates
from vexic.text_utils import estimate_tokens
from vexic.timeutil import utc_now_iso
from vexic.usage import UsageSummary, summarize_agent_usage

REM_MAX_CANDIDATES_PER_BATCH = 50
REM_MAX_PROMPT_TOKENS_PER_BATCH = 12_000

REM_SYSTEM_PROMPT = """\
You cluster short-term memory candidates and assign reinforcement boosts.

Rules:
- Return only candidate_id values from the provided list.
- A boost is a number in [0, 1].
- Use higher boosts for candidates that appear mutually reinforcing, central to
  the user's ongoing work, or likely to deserve promotion soon.
- Use 0 for isolated, weak, or unimportant candidates.
- Do not rewrite facts, promote facts, retire facts, or invent new candidates.\
"""


def build_rem_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Any:
    raise missing_host_port("REM boost")


def _forbidden_secret_values(secrets: Mapping[str, str] | None) -> list[str]:
    if secrets is None:
        return []
    return list(secrets.values())


def _rem_prompt(candidate_lines: list[str]) -> str:
    return "Memory candidates:\n" + "\n".join(candidate_lines)


def _candidate_line(candidate: RemCandidate) -> str:
    return (
        f"candidate_id={candidate.candidate_id}; "
        f"category={candidate.category}; fact={candidate.fact_text}"
    )


def _estimated_rem_prompt_tokens(candidate_lines: list[str]) -> int:
    return estimate_tokens(f"{REM_SYSTEM_PROMPT}\n\n{_rem_prompt(candidate_lines)}")


def _candidate_batches(
    candidates: list[RemCandidate],
    *,
    max_candidates_per_batch: int,
    max_prompt_tokens_per_batch: int,
) -> list[list[RemCandidate]]:
    batches: list[list[RemCandidate]] = []
    current_batch: list[RemCandidate] = []
    current_lines: list[str] = []

    for candidate in candidates:
        line = _candidate_line(candidate)
        if _estimated_rem_prompt_tokens([line]) > max_prompt_tokens_per_batch:
            raise ValueError(
                "REM candidate "
                f"{candidate.candidate_id} exceeds max_prompt_tokens_per_batch."
            )

        next_lines = [*current_lines, line]
        next_batch_is_too_large = (
            len(current_batch) + 1 > max_candidates_per_batch
            or _estimated_rem_prompt_tokens(next_lines) > max_prompt_tokens_per_batch
        )
        if current_batch and next_batch_is_too_large:
            batches.append(current_batch)
            current_batch = [candidate]
            current_lines = [line]
        else:
            current_batch.append(candidate)
            current_lines = next_lines

    if current_batch:
        batches.append(current_batch)

    return batches


async def run_rem_phase(
    db_path: str,
    model_group: str,
    *,
    agent_id: str | None = None,
    secrets: Mapping[str, str] | None = None,
    max_candidates_per_batch: int = REM_MAX_CANDIDATES_PER_BATCH,
    max_prompt_tokens_per_batch: int = REM_MAX_PROMPT_TOKENS_PER_BATCH,
    rem_agent_factory: AgentFactory | None = None,
) -> None:
    started_at = utc_now_iso()
    forbidden = _forbidden_secret_values(secrets)
    agent_factory = rem_agent_factory or build_rem_agent
    try:
        if max_candidates_per_batch <= 0:
            raise ValueError("max_candidates_per_batch must be greater than 0.")
        if max_prompt_tokens_per_batch <= 0:
            raise ValueError("max_prompt_tokens_per_batch must be greater than 0.")

        candidates = load_rem_candidates(db_path, agent_id=agent_id)
        if not candidates:
            commit_rem_cycle(
                db_path,
                {},
                agent_id=agent_id,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status="ok",
                forbidden_secret_values=forbidden,
            )
            print("REM phase: no eligible candidates. No-op.")
            return

        batches = _candidate_batches(
            candidates,
            max_candidates_per_batch=max_candidates_per_batch,
            max_prompt_tokens_per_batch=max_prompt_tokens_per_batch,
        )
        batch_prompts = [
            _rem_prompt([_candidate_line(candidate) for candidate in batch])
            for batch in batches
        ]
        for prompt in batch_prompts:
            assert_no_forbidden_secret_values(forbidden, prompt)

        agent = agent_factory(model_group, secrets=secrets)
        usage = UsageSummary()
        boosts = {candidate.candidate_id: 0.0 for candidate in candidates}
        for batch, prompt in zip(batches, batch_prompts, strict=True):
            result = await agent.run(prompt)
            usage = usage.plus(summarize_agent_usage(result))
            batch_candidate_ids = {candidate.candidate_id for candidate in batch}
            for boost in result.output.boosts:
                if boost.candidate_id not in batch_candidate_ids:
                    raise ValueError(
                        f"REM returned boost for candidate {boost.candidate_id} "
                        "outside the current batch."
                    )
                boosts[boost.candidate_id] = boost.boost

        stats = commit_rem_cycle(
            db_path,
            boosts,
            agent_id=agent_id,
            started_at=started_at,
            finished_at=utc_now_iso(),
            status="ok",
            model_requests=usage.model_requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_micros=usage.estimated_cost_micros,
            forbidden_secret_values=forbidden,
        )
        print(f"REM phase: {stats.boosted} candidates boosted.")
    except Exception as exc:
        try:
            commit_rem_cycle(
                db_path,
                {},
                agent_id=agent_id,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status="error",
                error_detail=traceback.format_exc(),
                forbidden_secret_values=forbidden,
            )
        except Exception:
            pass
        print(
            f"REM phase: ERROR -- {exc}. "
            "Boosts unchanged; Deep phase also skipped this cycle."
        )
        raise


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the REM memory boost phase once.")
    parser.add_argument("--db", required=True, help="Path to a Vexic SQLite memory database.")
    parser.add_argument("--model-group", required=True, help="Host model group label.")
    parser.add_argument("--agent-id", help="Optional agent memory scope. Omit for shared scope.")
    args = parser.parse_args()

    asyncio.run(run_rem_phase(args.db, args.model_group, agent_id=args.agent_id))


if __name__ == "__main__":
    _main()
