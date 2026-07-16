"""Summarize phase: compact Tier 1 transcript spans into `session_summaries`
rows (ADR: session summaries are rebuildable from Tier 1, never a source of
truth themselves).

Two passes run per compactable session, mirroring the Light phase's
conventions for usage accumulation, agent invocation, and fail-closed model
access (see `vexic.pipeline.run_light_phase`):

- Leaf pass: walk `find_session_compaction_span` until it yields no more
  spans, rendering each span's transcript and asking the summary agent for a
  plain-text summary (`result.output`), recorded as a `leaf` row.
- Condense pass: once the session's summary frontier gets too large (more
  than `CONDENSE_MAX_FRONTIER_LEAVES` entries, or more than a third of
  `TAU_SOFT` tokens), the oldest contiguous run of frontier summaries --
  the prefix whose message-id ranges are adjacent, ending at the first gap
  -- is condensed into a single `condensed` row that replaces it.

Per-session error isolation: a failure summarizing one session (including a
redaction violation) is swallowed and reported via a content-free print; the
phase moves on to the next session rather than aborting the whole run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from vexic.ports import AgentFactory, ContentCodec, missing_host_port
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import (
    SessionSummary,
    count_session_summaries_since,
    fetch_session_summary_frontier,
    find_session_compaction_span,
    init_db,
    list_compactable_session_ids,
    record_session_summary,
    render_compaction_source,
)
from vexic.text_utils import TAU_SOFT
from vexic.usage import UsageSummary, summarize_agent_usage

CONDENSE_MAX_FRONTIER_LEAVES = 8

__all__ = [
    "CONDENSE_MAX_FRONTIER_LEAVES",
    "SummarizePhaseOutcome",
    "run_summarize_phase",
]


@dataclass(frozen=True)
class SummarizePhaseOutcome:
    """Result of a summarize run: aggregate usage plus per-session error
    accounting, so a swallowed session failure never reads as a clean run."""

    usage: UsageSummary
    sessions_considered: int
    sessions_failed: int


def _forbidden_secret_values(
    secrets: Mapping[str, str] | None,
    extra_values: tuple[str, ...] = (),
) -> list[str]:
    values = [] if secrets is None else list(secrets.values())
    return [*values, *extra_values]


def _render_condense_source(summaries: list[SessionSummary]) -> str:
    return "\n---\n".join(summary.summary_text for summary in summaries)


def _oldest_contiguous_run(frontier: list[SessionSummary]) -> list[SessionSummary]:
    """Longest prefix of the frontier whose message-id ranges are adjacent.

    The frontier is ordered by ``first_message_id``; the run ends at the
    first summary that does not start exactly one past the previous
    summary's ``last_message_id``.
    """
    run = [frontier[0]]
    for summary in frontier[1:]:
        if summary.first_message_id != run[-1].last_message_id + 1:
            break
        run.append(summary)
    return run


def _budget_reached(
    db_path: str,
    *,
    agent_id: str | None,
    daily_span_budget: int | None,
    day_start: str,
) -> bool:
    if daily_span_budget is None:
        return False
    count = count_session_summaries_since(
        db_path,
        agent_id=agent_id,
        created_at_floor=day_start,
    )
    return count >= daily_span_budget


async def _run_leaf_pass(
    db_path: str,
    agent: object,
    *,
    session_id: str,
    agent_id: str | None,
    timezone_name: str,
    now_utc: datetime | None,
    forbidden: list[str],
    content_codec: ContentCodec | None,
    daily_span_budget: int | None,
    day_start: str,
    created_at: str,
) -> UsageSummary:
    usage = UsageSummary()
    while True:
        span = find_session_compaction_span(
            db_path,
            session_id=session_id,
            agent_id=agent_id,
            timezone_name=timezone_name,
            now_utc=now_utc,
            content_codec=content_codec,
        )
        if span is None:
            return usage

        if _budget_reached(
            db_path,
            agent_id=agent_id,
            daily_span_budget=daily_span_budget,
            day_start=day_start,
        ):
            print(
                "Summarize phase: daily span budget reached, "
                "stopping leaf pass for this session."
            )
            return usage

        first_message_id, last_message_id = span
        source = render_compaction_source(
            db_path,
            session_id=session_id,
            agent_id=agent_id,
            first_message_id=first_message_id,
            last_message_id=last_message_id,
            content_codec=content_codec,
        )
        assert_no_forbidden_secret_values(forbidden, source)
        result = await agent.run(source)
        span_usage = summarize_agent_usage(result)
        record_session_summary(
            db_path,
            session_id=session_id,
            agent_id=agent_id,
            kind="leaf",
            first_message_id=first_message_id,
            last_message_id=last_message_id,
            summary_text=result.output,
            usage=span_usage,
            forbidden_secret_values=forbidden,
            content_codec=content_codec,
            created_at=created_at,
        )
        usage = usage.plus(span_usage)


async def _run_condense_pass(
    db_path: str,
    agent: object,
    *,
    session_id: str,
    agent_id: str | None,
    forbidden: list[str],
    content_codec: ContentCodec | None,
    daily_span_budget: int | None,
    day_start: str,
    created_at: str,
) -> UsageSummary:
    frontier = fetch_session_summary_frontier(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
        content_codec=content_codec,
    )
    frontier_tokens = sum(summary.token_estimate for summary in frontier)
    if (
        len(frontier) <= CONDENSE_MAX_FRONTIER_LEAVES
        and frontier_tokens <= TAU_SOFT // 3
    ):
        return UsageSummary()

    if _budget_reached(
        db_path,
        agent_id=agent_id,
        daily_span_budget=daily_span_budget,
        day_start=day_start,
    ):
        print(
            "Summarize phase: daily span budget reached, "
            "skipping condense pass for this session."
        )
        return UsageSummary()

    # Condense only the oldest contiguous run of frontier summaries: walk
    # the frontier (ordered by message-id range) from the front and stop at
    # the first gap in message-id adjacency. A condensed row must never span
    # transcript messages that no summary in the run actually covers.
    run = _oldest_contiguous_run(frontier)
    condense_source = _render_condense_source(run)
    condense_prompt = f"Condense the following summaries:\n{condense_source}"
    assert_no_forbidden_secret_values(forbidden, condense_prompt)
    result = await agent.run(condense_prompt)
    condense_usage = summarize_agent_usage(result)
    record_session_summary(
        db_path,
        session_id=session_id,
        agent_id=agent_id,
        kind="condensed",
        first_message_id=run[0].first_message_id,
        last_message_id=run[-1].last_message_id,
        replaces_summary_ids=[summary.id for summary in run],
        summary_text=result.output,
        usage=condense_usage,
        forbidden_secret_values=forbidden,
        content_codec=content_codec,
        created_at=created_at,
    )
    return condense_usage


async def run_summarize_phase(
    db_path: str,
    model_group: str,
    *,
    agent_id: str | None = None,
    timezone_name: str = "UTC",
    now_utc: datetime | None = None,
    secrets: Mapping[str, str] | None = None,
    summary_agent_factory: AgentFactory | None = None,
    forbidden_secret_values: tuple[str, ...] = (),
    content_codec: ContentCodec | None = None,
    daily_span_budget: int | None = None,
) -> SummarizePhaseOutcome:
    if summary_agent_factory is None:
        raise missing_host_port(
            "Session summarization",
            hint="Provide build_summary_agent in the dream-phase adapter.",
        )

    forbidden = _forbidden_secret_values(secrets, forbidden_secret_values)
    init_db(db_path, content_codec=content_codec)
    agent = summary_agent_factory(model_group, secrets=secrets)

    # A single per-run clock reading: every summary write from this run
    # shares the same explicit `created_at`, and the budget window is this
    # same instant's UTC-day. Frozen/mocked via `now_utc` in tests.
    run_now_utc = now_utc if now_utc is not None else datetime.now(timezone.utc)
    created_at = run_now_utc.strftime("%Y-%m-%d %H:%M:%S")
    day_start = run_now_utc.strftime("%Y-%m-%d") + " 00:00:00"

    usage = UsageSummary()
    session_ids = list_compactable_session_ids(db_path, agent_id=agent_id)
    error_count = 0
    for session_id in session_ids:
        try:
            leaf_usage = await _run_leaf_pass(
                db_path,
                agent,
                session_id=session_id,
                agent_id=agent_id,
                timezone_name=timezone_name,
                now_utc=now_utc,
                forbidden=forbidden,
                content_codec=content_codec,
                daily_span_budget=daily_span_budget,
                day_start=day_start,
                created_at=created_at,
            )
            condense_usage = await _run_condense_pass(
                db_path,
                agent,
                session_id=session_id,
                agent_id=agent_id,
                forbidden=forbidden,
                content_codec=content_codec,
                daily_span_budget=daily_span_budget,
                day_start=day_start,
                created_at=created_at,
            )
            usage = usage.plus(leaf_usage).plus(condense_usage)
        except Exception as exc:
            # Per-session isolation: a failure summarizing one session (a
            # raising agent, or a redaction violation on its output) must not
            # prevent other sessions from being summarized this cycle.
            error_count += 1
            print(
                f"Summarize phase: session error -- {type(exc).__name__}. "
                "Continuing with next session."
            )

    print(
        f"Summarize phase: {len(session_ids)} sessions considered, "
        f"{error_count} failed."
    )
    return SummarizePhaseOutcome(
        usage=usage,
        sessions_considered=len(session_ids),
        sessions_failed=error_count,
    )
