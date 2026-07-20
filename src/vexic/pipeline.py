"""Light-phase extraction pipeline.

Reads new transcript messages past the dream watermark, renders them for the
host-supplied extraction agent, validates the returned ``FactCandidate``
provenance, embeds candidate text, and commits the cycle atomically. The
extraction agent itself is a host port: callers must inject an
``extraction_agent_factory`` (see ``vexic.ports``).
"""

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from vexic.embeddings import embed_texts, ensure_local_embeddings_available
from vexic.error_reporting import format_error_detail, mark_dream_recorded
from vexic.models import FactCandidate
from vexic.ports import (
    AgentFactory,
    ContentCodec,
    EmbedTexts,
    HostPortNotConfigured,
    missing_host_port,
)
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import (
    backfill_missing_candidate_embeddings,
    commit_dream_cycle,
    get_watermark,
    init_db,
    load_candidates_missing_embeddings,
    load_messages_since,
)
from vexic.storage.errors import retry_once_if_retryable
from vexic.storage.schema import DreamStatus
from vexic.timeutil import utc_now_iso
from vexic.usage import UsageSummary, summarize_agent_usage

LIGHT_PHASE_BATCH_SIZE = 50
_LOCAL_EMBEDDER = embed_texts

_MARKER_RE = re.compile(r"\[message_id=\d+[^\]]*\]")
_YEAR_RE = re.compile(r"\b(1\d{3}|20\d{2})\b")
_ISO_FULL_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_ISO_YM_RE = re.compile(r"\b(\d{4})-(\d{2})\b(?!-)")
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
# Case-sensitive by design: month names in fact_text are virtually always
# capitalized as month usage. Dropping IGNORECASE stops modal lowercase words
# ("...they may 2024 relocate") from being read as a month-year date; a genuine
# lowercase month reference degrades safely to undated rather than backfilling
# a fabricated month (ADR 0038).
_MONTH_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?:(\d{1,2})(?:st|nd|rd|th)?,?\s+)?(\d{4})\b",
)


def build_extraction_agent(
    model_group: str,
    secrets: Mapping[str, str] | None = None,
) -> Any:
    """Host port stub: raises until an adapter supplies an extraction agent."""
    raise missing_host_port("Light extraction")


def _ensure_embedding_adapter(embedder: EmbedTexts) -> None:
    if embedder is _LOCAL_EMBEDDER:
        ensure_local_embeddings_available()


_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _observed_label(timestamp: str | None) -> str:
    """Transient prompt scaffolding only: never persisted into message text,
    FTS, or replay (Memory Invariant 2, ADR 0034/0038)."""
    if not timestamp:
        return ""
    try:
        observed = date.fromisoformat(timestamp[:10])
    except ValueError:
        return ""
    return f" observed={observed.isoformat()} {_WEEKDAY_ABBR[observed.weekday()]}"


def render_transcript(rows: list[tuple[int, str | None, ModelMessage]]) -> str:
    """Render user/assistant text parts as ``[message_id=N] Role: text`` lines,
    labeled with the message's observed date and weekday when a valid
    timestamp is available."""
    lines: list[str] = []
    for message_id, timestamp, msg in rows:
        lines.extend(_render_message_lines(message_id, timestamp, msg))
    return "\n".join(lines)


def rendered_message_ids(rows: list[tuple[int, str | None, ModelMessage]]) -> list[int]:
    """Ids of messages that produce at least one rendered transcript line."""
    return [
        message_id
        for message_id, timestamp, msg in rows
        if _render_message_lines(message_id, timestamp, msg)
    ]


def _render_message_lines(
    message_id: int, timestamp: str | None, msg: ModelMessage
) -> list[str]:
    marker = f"[message_id={message_id}{_observed_label(timestamp)}]"
    lines: list[str] = []
    if isinstance(msg, ModelRequest):
        for part in msg.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                lines.append(f"{marker} User: {part.content}")
    elif isinstance(msg, ModelResponse):
        for part in msg.parts:
            if isinstance(part, TextPart):
                lines.append(f"{marker} Assistant: {part.content}")
    return lines


def keep_candidates_with_valid_source_ids(
    candidates: list[FactCandidate],
    allowed_message_ids: list[int],
) -> tuple[list[FactCandidate], int]:
    """Keep only candidates whose source_message_ids sit inside the rendered
    window, returning them with the count of those dropped.

    Invariant 5 is enforced per candidate, not per batch. A miscited candidate
    still never reaches Tier 2 -- provenance stays airtight -- but one model
    slip no longer fails the whole Light run, which would halt the chain and
    starve REM, Deep, and Tier 3.
    """
    allowed = set(allowed_message_ids)
    kept: list[FactCandidate] = []
    dropped = 0
    for candidate in candidates:
        candidate_ids = set(candidate.source_message_ids)
        if not candidate_ids or candidate_ids - allowed:
            # Counted, never echoed: fact_text and the offending ids would
            # carry tenant content into shared logs and error diagnostics.
            dropped += 1
            continue
        candidate.source_message_ids = sorted(candidate_ids)
        kept.append(candidate)
    return kept, dropped


def _plausible_years(rows: list[tuple[int, str | None, ModelMessage]], transcript: str) -> set[int]:
    """Years grounded in this Light window: each message's observed year (and
    its neighbors), plus any 4-digit year literally present in the rendered
    transcript text."""
    years: set[int] = set()
    for _, timestamp, _ in rows:
        if timestamp:
            try:
                y = date.fromisoformat(timestamp[:10]).year
            except ValueError:
                continue
            years.update((y - 1, y, y + 1))
    # Strip [message_id=... observed=...] markers before scanning for years:
    # a 4-digit message_id or the observed= date is transient scaffolding, not
    # transcript content, and must not ground an occurred_at year.
    text = _MARKER_RE.sub(" ", transcript)
    years.update(int(m.group(0)) for m in _YEAR_RE.finditer(text))
    return years


def _single_intext_date(fact_text: str) -> str | None:
    """The one absolute date stated in fact_text, at stated precision, or
    None. A calendar-invalid match (e.g. "February 30, 2023") still counts as
    a match for the exactly-one-total rule, but is never itself returned --
    it disqualifies the copy rather than producing a fabricated date."""
    found: list[str | None] = []
    for m in _ISO_FULL_RE.finditer(fact_text):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            date(year, month, day)
        except ValueError:
            found.append(None)
        else:
            found.append(m.group(0))
    stripped = _ISO_FULL_RE.sub(" ", fact_text)
    for m in _ISO_YM_RE.finditer(stripped):
        year, month = int(m.group(1)), int(m.group(2))
        try:
            date(year, month, 1)
        except ValueError:
            found.append(None)
        else:
            found.append(m.group(0))
    for m in _MONTH_DATE_RE.finditer(fact_text):
        month = _MONTHS[m.group(1).lower()]
        year = int(m.group(3))
        if m.group(2):
            day = int(m.group(2))
            try:
                date(year, month, day)
            except ValueError:
                found.append(None)
            else:
                found.append(f"{year}-{month:02d}-{day:02d}")
        else:
            try:
                date(year, month, 1)
            except ValueError:
                found.append(None)
            else:
                found.append(f"{year}-{month:02d}")
    return found[0] if len(found) == 1 else None


def _strip_marker_echo(fact_text: str) -> str:
    """Remove any echoed ``[message_id=... observed=...]`` marker from
    fact_text and collapse the resulting whitespace.

    The render marker is transient prompt scaffolding (Memory Invariant 2); an
    extractor that copies it into fact_text would persist it into Tier 2 text
    and FTS, and its ``observed=`` date could be misread as an in-text event
    date. Stripped before the date-copy logic and before embedding/commit.
    """
    return re.sub(r"\s+", " ", _MARKER_RE.sub(" ", fact_text)).strip()


def apply_occurred_at_guards(
    candidates: list[FactCandidate],
    rows: list[tuple[int, str | None, ModelMessage]],
    transcript: str,
) -> list[FactCandidate]:
    """Deterministic occurred_at guards (ADR 0038).

    Year plausibility runs first and only against a model-supplied
    occurred_at: a year with no grounding in the window's observed dates or
    the transcript text is dropped to None rather than trusted, killing the
    class of fabricated far-future/far-past dates deterministically. The
    in-text copy-backfill then runs only for event candidates still lacking
    a date, and only copies when fact_text states exactly one absolute date.

    Year plausibility is then re-checked against whatever occurred_at is left
    standing, including a value the copy-backfill just supplied: fact_text is
    itself model output and can carry a fabricated year, so a backfilled date
    is never exempt from the same check a model-supplied date gets. Every
    non-null occurred_at leaving this function has a year in ``plausible``.

    Fabricated components degrade to undated (ADR 0037 Tier 2 sink) rather
    than dropping the candidate; in-text dates copy at stated precision only.
    """
    plausible = _plausible_years(rows, transcript)
    for candidate in candidates:
        # Strip echoed render markers first: they carry an observed= date that
        # _single_intext_date would otherwise misread as an event date, and
        # must not survive into stored fact_text (runs before embedding at the
        # run_light_phase call site).
        candidate.fact_text = _strip_marker_echo(candidate.fact_text)
        if candidate.occurred_at is not None:
            if int(candidate.occurred_at[:4]) not in plausible:
                candidate.occurred_at = None
        if candidate.occurred_at is None and candidate.category == "event":
            candidate.occurred_at = _single_intext_date(candidate.fact_text)
        if candidate.occurred_at is not None:
            if int(candidate.occurred_at[:4]) not in plausible:
                candidate.occurred_at = None
        # Ungrounded-precision cap: if fact_text states a single in-text date
        # that is a strict, shorter prefix of the (model-supplied) occurred_at,
        # truncate occurred_at to the in-text precision. Precision reduction
        # only -- never extension (ADR 0038 day-invention mitigation).
        if candidate.occurred_at is not None:
            intext = _single_intext_date(candidate.fact_text)
            if (
                intext is not None
                and candidate.occurred_at.startswith(intext)
                and len(candidate.occurred_at) > len(intext)
            ):
                candidate.occurred_at = intext
    return candidates


def _forbidden_secret_values(
    secrets: Mapping[str, str] | None,
    extra_values: tuple[str, ...] = (),
) -> list[str]:
    values = [] if secrets is None else list(secrets.values())
    return [*values, *extra_values]


@dataclass(frozen=True)
class LightPhaseOutcome:
    """Count-only accounting for one Light cycle (never candidate content)."""

    usage: UsageSummary
    candidates_kept: int = 0
    candidates_dropped: int = 0


async def run_light_phase(
    db_path: str,
    model_group: str,
    batch_size: int = LIGHT_PHASE_BATCH_SIZE,
    agent_id: str | None = None,
    secrets: Mapping[str, str] | None = None,
    extraction_agent_factory: AgentFactory | None = None,
    embed: EmbedTexts | None = None,
    forbidden_secret_values: tuple[str, ...] = (),
    content_codec: ContentCodec | None = None,
) -> LightPhaseOutcome:
    """Run one Light extraction cycle over messages past the watermark.

    Returns a count-only outcome with the usage summary; commits candidates
    and the new watermark atomically via ``commit_dream_cycle``.
    """
    started_at = utc_now_iso()
    watermark = 0
    dropped = 0
    forbidden = _forbidden_secret_values(secrets, forbidden_secret_values)
    agent_factory = extraction_agent_factory or build_extraction_agent
    embedder = embed or embed_texts

    try:
        init_db(db_path, content_codec=content_codec)
        watermark = get_watermark(db_path, agent_id=agent_id)

        rows = load_messages_since(
            db_path,
            watermark,
            limit=batch_size,
            agent_id=agent_id,
            exclude_session_prefixes=("onboarding:",),
            content_codec=content_codec,
        )
        if not rows:
            missing_embeddings = load_candidates_missing_embeddings(
                db_path,
                agent_id=agent_id,
            )
            if missing_embeddings:
                _ensure_embedding_adapter(embedder)
                assert_no_forbidden_secret_values(
                    forbidden,
                    *(fact_text for _, fact_text in missing_embeddings),
                )
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
                observed_watermark=watermark,
                forbidden_secret_values=forbidden,
            )
            print("Light phase: no new messages. No-op.")
            return LightPhaseOutcome(usage=UsageSummary())

        window_ids = [msg_id for msg_id, _, _ in rows]
        transcript = render_transcript(rows)
        evidence_ids = rendered_message_ids(rows)
        assert_no_forbidden_secret_values(forbidden, transcript)
        _ensure_embedding_adapter(embedder)

        agent = agent_factory(model_group, secrets=secrets)
        result = await agent.run(transcript)
        usage = summarize_agent_usage(result)
        candidates, dropped = keep_candidates_with_valid_source_ids(
            result.output, evidence_ids
        )
        apply_occurred_at_guards(candidates, rows, transcript)

        missing_embeddings = load_candidates_missing_embeddings(
            db_path,
            agent_id=agent_id,
        )
        if missing_embeddings:
            assert_no_forbidden_secret_values(
                forbidden,
                *(fact_text for _, fact_text in missing_embeddings),
            )
            backfill_embeddings = embedder([fact_text for _, fact_text in missing_embeddings])
            backfill_missing_candidate_embeddings(
                db_path,
                list(zip([candidate_id for candidate_id, _ in missing_embeddings], backfill_embeddings, strict=True)),
                forbidden_secret_values=forbidden,
            )

        assert_no_forbidden_secret_values(
            forbidden,
            *(candidate.fact_text for candidate in candidates),
        )
        candidate_embeddings = embedder([candidate.fact_text for candidate in candidates])
        # ADR 0031 amendment: the drop count is durable, and a run that
        # extracted candidates but kept none is 'partial', not silently 'ok'.
        status: DreamStatus = "partial" if dropped and not candidates else "ok"
        commit_dream_cycle(
            db_path,
            candidates,
            agent_id=agent_id,
            status=status,
            started_at=started_at,
            finished_at=utc_now_iso(),
            messages_processed=len(rows),
            last_processed_message_id=max(window_ids),
            candidate_embeddings=candidate_embeddings,
            observed_watermark=watermark,
            model_requests=usage.model_requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_micros=usage.estimated_cost_micros,
            candidates_dropped=dropped,
            forbidden_secret_values=forbidden,
        )
        dropped_note = (
            f" ({dropped} dropped: source_message_ids missing or outside the window)"
            if dropped
            else ""
        )
        print(
            f"Light phase: {len(rows)} messages -> "
            f"{len(candidates)} extracted candidates{dropped_note}."
        )
        return LightPhaseOutcome(
            usage=usage,
            candidates_kept=len(candidates),
            candidates_dropped=dropped,
        )

    except Exception as exc:
        # Best-effort audit, retried once on a retryable storage fault. A
        # failure writing the error row must not mask the original exception,
        # but the sweeper must learn whether it landed: advancing the 24h retry
        # clock over an unrecorded failure is what stalled Tier 3 in a live
        # dreaming incident.
        recorded = False
        try:
            retry_once_if_retryable(
                lambda: commit_dream_cycle(
                    db_path,
                    [],
                    agent_id=agent_id,
                    status="error",
                    started_at=started_at,
                    finished_at=utc_now_iso(),
                    messages_processed=0,
                    last_processed_message_id=watermark,
                    error_detail=format_error_detail(exc),
                    # A drop count established before the failure is still the
                    # ADR 0031 miscitation signal; do not zero it on error.
                    candidates_dropped=dropped,
                    forbidden_secret_values=forbidden,
                )
            )
            recorded = True
        except Exception:
            # Best-effort status write; the original error is re-raised below.
            recorded = False
        print(
            f"Light phase: ERROR -- {type(exc).__name__}. Watermark held; will retry."
        )
        raise mark_dream_recorded(exc, recorded)


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
