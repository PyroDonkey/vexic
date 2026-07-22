"""Offline Deep-backlog replay over frozen LongMemEval run DBs.

Read-only and provider-free: every question this harness answers is
deterministic, reconstructed from the persisted ``dream_runs``,
``long_term_memory`` and ``memory_dedup_events`` tables rather than by re-running
any model. It exists to measure whether a backlog of promotion-eligible Tier-2
candidates actually drains across successive Deep cycles, or whether a
low-ranked candidate is permanently outcompeted.

``dream_runs`` carries no phase column, so this module reconstructs the
Light / REM / Deep timeline from each row's counter signature, and reconstructs
which candidate was promoted in which Deep cycle by joining
``long_term_memory.promoted_from_candidate_id`` and the fact ``created_at`` into
the Deep runs' time intervals.

LIMITATIONS:

  * Phase reconstruction reads counter signatures, not a stored phase. All-zero
    Deep/REM rows are resolved by their position inside a Light-to-fixpoint ->
    REM -> Deep cycle, and flagged ``phase_inferred``. A genuinely ambiguous
    trailing REM-without-Deep row is only distinguishable when it carries a
    boost signature.
  * Attribution spans each Deep cycle's start to the next cycle's start
    (half-open, the last cycle unbounded above) because ``commit_deep_cycle``
    persists promoted facts after the run's ``finished_at`` is captured. Cycle
    starts are floored to the second since fact ``created_at`` is
    second-truncated while run bounds carry microseconds.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any, Iterator, Mapping, Sequence, Union

from pydantic import BaseModel, Field, ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra_path in (str(_REPO_ROOT / "src"), str(_REPO_ROOT)):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

from vexic.deep import DEFAULT_TOP_N, compute_score  # noqa: E402
from vexic.longmemeval import (  # noqa: E402
    _DiagnosticCandidate,
    _deep_eligible,
    _embedded_candidate_ids,
    _load_diagnostic_candidates,
    _question_path_component,
    _rank_diagnostic_candidates,
    _timestamp_datetime,
)
from vexic.models import canonical_partial_date  # noqa: E402
from vexic.storage import init_db  # noqa: E402

ConnOrPath = Union[sqlite3.Connection, str, Path]

# Fixed scoring anchor for an empty candidate pool. Wall-clock now would make an
# empty pool emit a different clock on every rerun; the ranking is empty in that
# case, so this anchor only stabilizes the surfaced field (sibling harness).
_EMPTY_POOL_SCORING_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)


@contextmanager
def _connection(conn_or_path: ConnOrPath) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection, opening the copy only when given a path."""
    if isinstance(conn_or_path, sqlite3.Connection):
        yield conn_or_path
        return
    conn = sqlite3.connect(f"file:{Path(conn_or_path)}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class DeepCycle:
    """One reconstructed Deep phase, in chronological order."""

    run_id: int
    started_at: str
    finished_at: str | None
    promotions: int
    retirements: int
    phase_inferred: bool


@dataclass(frozen=True)
class LightRun:
    """One Light run that advanced the transcript watermark."""

    run_id: int
    watermark: int
    finished_at: str | None


@dataclass(frozen=True)
class DreamCycleTimeline:
    """The Deep cycles and watermark-advancing Light runs of one run DB."""

    deep_cycles: list[DeepCycle]
    light_runs: list[LightRun]


@dataclass(frozen=True)
class _DreamRow:
    run_id: int
    started_at: str
    finished_at: str | None
    watermark: int
    inserted: int
    merged: int
    review: int
    boosted: int
    promotions: int
    retirements: int

    @property
    def phase(self) -> str:
        """Signature classification (F4), most specific signal first."""
        if self.promotions > 0 or self.retirements > 0:
            return "deep"
        if self.boosted > 0:
            return "rem"
        if self.watermark > 0 or self.inserted > 0 or self.merged > 0 or self.review > 0:
            return "light"
        return "ambiguous"


def _load_dream_rows(conn: sqlite3.Connection) -> list[_DreamRow]:
    """Read the non-error, single-scope (agent_id IS NULL) rows in run order."""
    rows = conn.execute(
        """
        SELECT id, started_at, finished_at, last_processed_message_id,
               candidates_inserted, candidates_merged, candidates_review,
               candidates_boosted, promotions, retirements
        FROM dream_runs
        WHERE status != 'error' AND agent_id IS NULL
        ORDER BY id
        """
    ).fetchall()
    return [
        _DreamRow(
            run_id=int(row[0]),
            started_at=str(row[1]),
            finished_at=None if row[2] is None else str(row[2]),
            watermark=int(row[3]),
            inserted=int(row[4]),
            merged=int(row[5]),
            review=int(row[6]),
            boosted=int(row[7]),
            promotions=int(row[8]),
            retirements=int(row[9]),
        )
        for row in rows
    ]


def _split_batches(rows: list[_DreamRow]) -> list[list[_DreamRow]]:
    """Group rows into cycles: a clearly-Light row after tail rows opens one.

    Orchestration is Light-to-fixpoint -> REM -> Deep, so a clearly-Light row
    only ever starts a cycle. Once a batch has collected any non-Light (REM /
    Deep / ambiguous) tail row, the next clearly-Light row begins a new batch.
    """
    batches: list[list[_DreamRow]] = []
    current: list[_DreamRow] = []
    has_tail = False
    for row in rows:
        if row.phase == "light" and has_tail:
            batches.append(current)
            current = []
            has_tail = False
        current.append(row)
        if row.phase != "light":
            has_tail = True
    if current:
        batches.append(current)
    return batches


def _pair_tail(tail: list[_DreamRow]) -> list[DeepCycle]:
    """Resolve a batch tail into Deep cycles, pairing (REM, Deep) from the right.

    Grammar per cycle is ``L* R D``. Reading the non-Light tail right to left,
    each (earlier, later) pair is one cycle's (REM, Deep). A lone leftover at
    the far left is a superseded no-op Light and produces no cycle; a lone
    trailing REM (boost signature, no Deep) likewise produces no cycle.
    """
    if not tail:
        return []
    # A trailing REM-without-Deep row: it carries a boost signature and no Deep
    # ever followed, so it must not be read as a Deep.
    if tail[-1].phase == "rem":
        deep_tail = tail[:-1]
    else:
        deep_tail = tail
    cycles: list[DeepCycle] = []
    index = len(deep_tail)
    while index >= 1:
        deep = deep_tail[index - 1]
        earlier = deep_tail[index - 2] if index >= 2 else None
        # The earlier row is only this Deep's REM slot when it is genuinely a REM
        # (boost signature) or an all-zero ambiguous row. Two consecutive
        # clearly-Deep rows must not collapse: a clearly-Deep earlier row keeps
        # its own cycle, so it is not consumed here.
        if earlier is not None and earlier.phase in ("rem", "ambiguous"):
            inferred = deep.phase == "ambiguous" or earlier.phase == "ambiguous"
            cycles.append(
                DeepCycle(
                    run_id=deep.run_id,
                    started_at=deep.started_at,
                    finished_at=deep.finished_at,
                    promotions=deep.promotions,
                    retirements=deep.retirements,
                    phase_inferred=inferred,
                )
            )
            index -= 2
            continue
        # No REM partner (the earlier row is clearly Deep, or there is none). A
        # clearly-Deep row still counts as its own cycle; a lone ambiguous row is
        # a superseded no-op Light and produces nothing.
        if deep.phase == "deep":
            cycles.append(
                DeepCycle(
                    run_id=deep.run_id,
                    started_at=deep.started_at,
                    finished_at=deep.finished_at,
                    promotions=deep.promotions,
                    retirements=deep.retirements,
                    phase_inferred=False,
                )
            )
        index -= 1
    cycles.reverse()
    return cycles


def _light_runs(rows: list[_DreamRow]) -> list[LightRun]:
    """Clearly-Light runs, keeping only strictly-increasing watermarks."""
    runs: list[LightRun] = []
    high_water = 0
    for row in rows:
        if row.phase != "light":
            continue
        if row.watermark <= high_water:
            continue
        high_water = row.watermark
        runs.append(
            LightRun(run_id=row.run_id, watermark=row.watermark, finished_at=row.finished_at)
        )
    return runs


def _dream_cycle_timeline(conn_or_path: ConnOrPath) -> DreamCycleTimeline:
    """Reconstruct the ordered Deep cycles and Light watermark runs (F4)."""
    with _connection(conn_or_path) as conn:
        rows = _load_dream_rows(conn)
    deep_cycles: list[DeepCycle] = []
    for batch in _split_batches(rows):
        prefix_end = 0
        while prefix_end < len(batch) and batch[prefix_end].phase == "light":
            prefix_end += 1
        deep_cycles.extend(_pair_tail(batch[prefix_end:]))
    return DreamCycleTimeline(deep_cycles=deep_cycles, light_runs=_light_runs(rows))


@dataclass(frozen=True)
class PromotionAttribution:
    """Which Deep cycle each promoted candidate landed in (F3)."""

    by_candidate: dict[int, int]
    per_cycle_counts: list[int]
    unattributed: list[int]
    attribution_consistent: bool


def _floor_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _promotion_events(
    conn_or_path: ConnOrPath, deep_cycles: list[DeepCycle]
) -> PromotionAttribution:
    """Attribute each promoted fact to the Deep cycle that created it (F3).

    ``commit_deep_cycle`` persists a promoted fact *after* the run's
    ``finished_at`` is captured, so a delayed write can land past
    ``ceil(finished_at)`` and would spuriously unattribute against the run
    interval. Deep promotion is the only ``long_term_memory`` writer, so
    anything between cycle k's start and cycle k+1's start belongs to k: cycle k
    owns ``[floor(started_at_k), floor(started_at_{k+1}))`` for every cycle but
    the last, and the last cycle owns ``[floor(started_at), unbounded)``. A fact
    created before the first cycle's start stays unattributed (it signals
    corruption). ``started_at`` is floored to the second because fact
    ``created_at`` is second-truncated while run bounds carry microseconds.
    """
    starts = [_floor_second(_timestamp_datetime(cycle.started_at)) for cycle in deep_cycles]
    # Half-open intervals [start_k, start_{k+1}); the last cycle is unbounded
    # above (None end) so a delayed commit past its finished_at still lands in it.
    intervals: list[tuple[datetime, datetime | None]] = [
        (starts[index], starts[index + 1] if index + 1 < len(starts) else None)
        for index in range(len(starts))
    ]

    with _connection(conn_or_path) as conn:
        rows = conn.execute(
            """
            SELECT id, promoted_from_candidate_id, created_at
            FROM long_term_memory
            ORDER BY id
            """
        ).fetchall()

    by_candidate: dict[int, int] = {}
    per_cycle_counts = [0] * len(deep_cycles)
    unattributed: list[int] = []
    for _fact_id, candidate_id, created_at in rows:
        created = _timestamp_datetime(str(created_at))
        assigned: int | None = None
        for index, (start, end) in enumerate(intervals):
            if created < start:
                continue
            if end is None or created < end:
                assigned = index
                break
        if assigned is None:
            unattributed.append(int(candidate_id))
            continue
        by_candidate[int(candidate_id)] = assigned
        per_cycle_counts[assigned] += 1

    consistent = not unattributed and all(
        per_cycle_counts[index] == cycle.promotions
        for index, cycle in enumerate(deep_cycles)
    )
    return PromotionAttribution(
        by_candidate=by_candidate,
        per_cycle_counts=per_cycle_counts,
        unattributed=unattributed,
        attribution_consistent=consistent,
    )


@dataclass(frozen=True)
class _ReplayCandidate:
    """One Tier-2 candidate row, with the final-state fields the pool needs.

    ``created_at`` is parsed loud through ``_timestamp_datetime`` so an
    unparseable stamp raises rather than silently defaulting to ``now()``; the
    remaining flags are the post-run snapshot used to approximate the eligible
    pool at each historical cycle (F8's final-state approximation).
    """

    candidate_id: int
    importance: int
    category: str
    occurred_at: str | None
    mentioned_at: str | None
    rem_boost: float
    created_at: datetime
    source_message_ids: list[int]
    retired: bool
    stale: bool
    needs_review: bool
    promoted: bool
    promoted_fact_id: int | None
    has_embedding: bool


@dataclass(frozen=True)
class CandidateState:
    """Exact per-cycle state of one candidate at a Deep cycle's ``started_at``.

    Reconstructed from ``memory_dedup_events`` (F5): ``hit_count`` and
    ``last_seen_at`` accumulate every merge that had landed by ``T``, and
    ``mentioned_at`` is the per-cycle healed earliest-source date.
    """

    candidate_id: int
    hit_count: int
    last_seen_at: datetime
    mentioned_at: str | None


def _load_replay_candidates(conn: sqlite3.Connection) -> list[_ReplayCandidate]:
    """Load every candidate row (all flags carried, filtered later per F8).

    ``created_at`` parses loud: an unparseable stamp raises ``ValueError``
    rather than going non-deterministic through a ``now()`` fallback. A frozen
    pre-heal DB captured before the ADR 0037 migration may lack the
    ``mentioned_at`` column entirely; the schema is probed so the as-run read
    treats stored ``mentioned_at`` as ``None`` for every row instead of raising.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
    mentioned_at_select = "mentioned_at" if "mentioned_at" in columns else "NULL"
    rows = conn.execute(
        f"""
        SELECT id, importance, category, occurred_at, {mentioned_at_select}, rem_boost,
               created_at, source_message_ids, retired, stale, needs_review,
               promoted, promoted_fact_id
        FROM memory_candidates
        ORDER BY id ASC
        """
    ).fetchall()
    embedded = _embedded_candidate_ids(conn)
    candidates: list[_ReplayCandidate] = []
    for row in rows:
        try:
            source_ids = [int(value) for value in json.loads(row[7])]
        except (TypeError, ValueError):
            source_ids = []
        candidates.append(
            _ReplayCandidate(
                candidate_id=int(row[0]),
                importance=int(row[1]),
                category=str(row[2]),
                occurred_at=None if row[3] is None else str(row[3]),
                mentioned_at=None if row[4] is None else str(row[4]),
                rem_boost=float(row[5]),
                created_at=_timestamp_datetime(str(row[6])),
                source_message_ids=source_ids,
                retired=bool(row[8]),
                stale=bool(row[9]),
                needs_review=bool(row[10]),
                promoted=bool(row[11]),
                promoted_fact_id=None if row[12] is None else int(row[12]),
                has_embedding=int(row[0]) in embedded,
            )
        )
    return candidates


def _load_merge_events(conn: sqlite3.Connection) -> dict[int, list[datetime]]:
    """Merge-decision event times per target candidate, ascending (F5).

    Only ``decision='merge'`` rows reinforce hit_count; each ``created_at``
    parses loud so an unparseable event stamp raises rather than defaulting.
    """
    rows = conn.execute(
        """
        SELECT candidate_id, created_at
        FROM memory_dedup_events
        WHERE decision = 'merge'
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    events: dict[int, list[datetime]] = {}
    for candidate_id, created_at in rows:
        events.setdefault(int(candidate_id), []).append(
            _timestamp_datetime(str(created_at))
        )
    for times in events.values():
        times.sort()
    return events


def _load_message_times(conn: sqlite3.Connection) -> dict[int, str]:
    """Raw ``messages`` timestamp strings keyed by message id.

    Kept as raw strings: the per-cycle ``mentioned_at`` derivation fail-softs on
    an individual unparseable message stamp exactly like the production
    ``_earliest_date_from_timestamps`` backfill, rather than aborting.
    """
    rows = conn.execute("SELECT id, timestamp FROM messages").fetchall()
    return {int(row[0]): row[1] for row in rows if row[1] is not None}


def _derive_mentioned_at(
    source_message_ids: Sequence[int],
    message_times: Mapping[int, str],
    *,
    watermark: int | None = None,
) -> str | None:
    """Earliest cited-message calendar date, date-only ISO (ADR 0037 semantics).

    Derived provenance, never model output: a pure function of
    ``source_message_ids`` over ``messages``. Parser parity with production's
    ``_earliest_date_from_timestamps`` (storage/schema.py) is exact -- only
    ``datetime.fromisoformat`` is accepted (never the permissive LongMemEval
    ``%Y/%m/%d %H:%M`` fallback ``_timestamp_datetime`` allows), a naive stamp is
    read as UTC while an aware stamp is converted to UTC, and each stamp
    fail-softs on ``(ValueError, OverflowError)`` so a single unparseable message
    is skipped rather than aborting. An empty or fully-unresolvable citation set
    yields ``None``.

    ``watermark`` is the anachronism gate: a later merge can union in an
    earlier-dated source message, which would make an early cycle look healed
    before that source was ever cited. When a watermark is supplied, only source
    message ids at or below it (the transcript Light had processed by the cycle)
    are considered; ``None`` disables the gate so every cited message counts.
    """
    earliest: datetime | None = None
    for message_id in source_message_ids:
        if watermark is not None and int(message_id) > watermark:
            continue
        raw = message_times.get(int(message_id))
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            continue
        if earliest is None or parsed < earliest:
            earliest = parsed
    return None if earliest is None else earliest.date().isoformat()


def _candidate_state_at(
    candidate: _ReplayCandidate,
    started_at: datetime,
    *,
    cycle_index: int,
    merge_events: Sequence[datetime],
    message_times: Mapping[int, str],
    promoted_at_cycle: int | None,
    watermark: int | None = None,
) -> CandidateState | None:
    """Exact state of ``candidate`` at Deep cycle ``cycle_index`` (F5).

    Returns ``None`` when the candidate is not in that cycle's pool: it was
    created after ``started_at``, or it was already promoted in an earlier
    cycle (``promoted_at_cycle < cycle_index``). Otherwise ``hit_count`` is
    ``1 + merges landed by T`` and ``last_seen_at`` is the latest such merge
    (else the candidate's own ``created_at``). ``watermark`` bounds the
    per-cycle ``mentioned_at`` derivation to the transcript Light had already
    processed, so a later-merged earlier source cannot heal an early cycle
    anachronistically.
    """
    if candidate.created_at > started_at:
        return None
    if promoted_at_cycle is not None and promoted_at_cycle < cycle_index:
        return None
    landed = [event for event in merge_events if event <= started_at]
    hit_count = 1 + len(landed)
    last_seen_at = max(landed) if landed else candidate.created_at
    return CandidateState(
        candidate_id=candidate.candidate_id,
        hit_count=hit_count,
        last_seen_at=last_seen_at,
        mentioned_at=_derive_mentioned_at(
            candidate.source_message_ids, message_times, watermark=watermark
        ),
    )


def _is_undated_event(category: str, occurred_at: str | None, mentioned_at: str | None) -> bool:
    """Production's undated-``event`` skip (deep.select_promotions / _deep_eligible).

    A ``category='event'`` candidate with neither a canonical ``occurred_at``
    nor any ``mentioned_at`` is refused promotion. The occurred_at gate runs the
    same ``canonical_partial_date`` the promotion path uses, so a nonblank but
    invalid value is treated as undated.
    """
    return (
        category == "event"
        and canonical_partial_date(occurred_at) is None
        and not (mentioned_at or "").strip()
    )


@dataclass(frozen=True)
class TrackedRank:
    """One tracked gap candidate's rank + eligibility at one Deep cycle (flag B).

    Both modes are scored with the per-cycle ``hit_count``/``max_hit_count`` and
    the final ``rem_boost``. ``as_run_rank``/``healed_rank`` are 1-based positions
    inside their mode's pool, ``None`` when the candidate is outside that pool
    (created later, already promoted, or filtered out — undated as-run).
    """

    candidate_id: int
    as_run_rank: int | None
    as_run_eligible: bool
    healed_rank: int | None
    healed_eligible: bool


@dataclass(frozen=True)
class CycleReplay:
    """Reconstructed pool + top-N prediction for one historical Deep cycle (F8).

    ``as_run`` mirrors the pool production actually scored (undated events
    excluded via the candidate's stored ``mentioned_at``); ``healed`` substitutes
    the per-cycle derived ``mentioned_at`` so undated events that ADR 0037 would
    heal join the pool. Both pools score with per-cycle ``hit_count`` and a
    per-cycle ``max_hit_count`` (F6b) at ``now = started_at`` (F7). Predictions
    are bracketed by ``rem_boost`` (final vs 0, F6) since history is unrecoverable.
    ``tracked_ranks`` carries the per-cycle rank trajectory of the gap-fixture
    candidates in both modes (flag B).
    """

    run_id: int
    started_at: str
    phase_inferred: bool
    promotions_recorded: int
    as_run_pool_size: int
    healed_pool_size: int
    newly_eligible_inflow: int
    backlog_after: int
    saturated: bool
    predicted_as_run_rem_final: list[int]
    predicted_as_run_rem_zero: list[int]
    prediction_overlap_rem_final: float | None
    prediction_overlap_rem_zero: float | None
    tracked_ranks: dict[int, TrackedRank]


def _assert_single_scope(conn: sqlite3.Connection) -> None:
    """Fail loud on any agent-scoped row (production Deep filters by agent_id).

    ``_load_replay_candidates``/``_deep_eligible`` do not filter ``agent_id``, so
    a multi-scope DB would silently blend pools. The frozen eval DBs are
    single-scope (all ``agent_id IS NULL``); anything else is unsupported.
    """
    for table in ("memory_candidates", "dream_runs"):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE agent_id IS NOT NULL LIMIT 1"
        ).fetchone()
        if row is not None:
            raise ValueError(
                f"{table} carries agent-scoped rows; replay requires a "
                "single-scope (agent_id IS NULL) DB."
            )


def _rank_ids(
    pool_ids: Sequence[int],
    states: Mapping[int, tuple[_ReplayCandidate, CandidateState]],
    started: datetime,
    *,
    rem_zero: bool,
) -> dict[int, int]:
    """Rank one mode's pool at ``started``, id-ASC stable tie-break (F6b/F7).

    ``pool_ids`` arrives id-ASC and Python's sort is stable, so equal scores keep
    id order — exactly ``select_promotions``. The ``max_hit_count`` normalizer is
    the busiest member of *this* pool this cycle, never the final max (F6b).
    """
    max_hit_count = max((states[cid][1].hit_count for cid in pool_ids), default=0)
    scored = [
        (
            compute_score(
                importance=states[cid][0].importance,
                hit_count=states[cid][1].hit_count,
                days_since_last_seen=(started - states[cid][1].last_seen_at).total_seconds()
                / 86400,
                max_hit_count=max_hit_count,
                rem_boost=0.0 if rem_zero else states[cid][0].rem_boost,
            ),
            cid,
        )
        for cid in pool_ids
    ]
    ordered = sorted(scored, key=lambda pair: pair[0], reverse=True)
    return {cid: rank for rank, (_, cid) in enumerate(ordered, start=1)}


def _top_from_ranks(ranks: Mapping[int, int], top_n: int) -> list[int]:
    """Top-N candidate ids by rank, ties already broken id-ASC by ``_rank_ids``."""
    return [
        cid
        for cid, rank in sorted(ranks.items(), key=lambda pair: pair[1])
        if rank <= top_n
    ]


def _watermark_at(light_runs: Sequence[LightRun], started_at: datetime) -> int | None:
    """Highest Light watermark reached at or before a Deep cycle's ``started_at``.

    The anachronism gate (F4b): only source messages Light had processed before
    the cycle should count toward a per-cycle ``mentioned_at``. Light watermarks
    are strictly increasing, so the max among runs whose ``finished_at`` precedes
    the cycle is the transcript boundary. Returns ``None`` when no Light run
    finished by then (a synthetic Deep-only DB), which disables the gate so a
    zero-merge reconstruction is byte-identical to the pre-gate behavior.
    """
    reached: int | None = None
    for run in light_runs:
        if run.finished_at is None:
            continue
        if _timestamp_datetime(run.finished_at) <= started_at:
            reached = run.watermark if reached is None else max(reached, run.watermark)
    return reached


def _replay_cycles(
    conn_or_path: ConnOrPath,
    *,
    top_n: int = DEFAULT_TOP_N,
    tracked_ids: Sequence[int] = (),
    timeline: DreamCycleTimeline | None = None,
    attribution: PromotionAttribution | None = None,
) -> list[CycleReplay]:
    """Reconstruct each Deep cycle's pool and top-N prediction, both modes (F8).

    For every Deep cycle in chronological order, the pool is the candidates that
    existed at ``started_at`` and were not promoted in an earlier cycle, passing
    the final-state flags (``retired=0``/``stale=0``/``needs_review=0`` and an
    embedding row — a final-state approximation of the pool each cycle scored).
    Scoring uses per-cycle ``hit_count`` and per-cycle ``max_hit_count`` at
    ``now = started_at``; predictions are bracketed by ``rem_boost``.

    A caller that already reconstructed the timeline and attribution (the
    ``replay_question`` orchestration does) passes them in so the joins run once
    rather than being recomputed here; both default to being derived internally.
    """
    if timeline is None:
        timeline = _dream_cycle_timeline(conn_or_path)
    if attribution is None:
        attribution = _promotion_events(conn_or_path, timeline.deep_cycles)
    with _connection(conn_or_path) as conn:
        _assert_single_scope(conn)
        candidates = _load_replay_candidates(conn)
        merge_events = _load_merge_events(conn)
        message_times = _load_message_times(conn)

    replays: list[CycleReplay] = []
    previous_healed_ids: set[int] = set()
    for cycle_index, cycle in enumerate(timeline.deep_cycles):
        started = _timestamp_datetime(cycle.started_at)
        watermark = _watermark_at(timeline.light_runs, started)
        actual_ids = {
            candidate_id
            for candidate_id, index in attribution.by_candidate.items()
            if index == cycle_index
        }

        # Per-cycle state for every candidate still in this cycle's pool.
        states: dict[int, tuple[_ReplayCandidate, CandidateState]] = {}
        for candidate in candidates:
            if candidate.retired or candidate.stale or candidate.needs_review:
                continue
            if not candidate.has_embedding:
                continue
            state = _candidate_state_at(
                candidate,
                started,
                cycle_index=cycle_index,
                merge_events=merge_events.get(candidate.candidate_id, []),
                message_times=message_times,
                promoted_at_cycle=attribution.by_candidate.get(candidate.candidate_id),
                watermark=watermark,
            )
            if state is None:
                continue
            states[candidate.candidate_id] = (candidate, state)

        as_run_ids = [
            cid
            for cid, (candidate, _state) in states.items()
            if not _is_undated_event(
                candidate.category, candidate.occurred_at, candidate.mentioned_at
            )
        ]
        healed_ids = [
            cid
            for cid, (candidate, state) in states.items()
            if not _is_undated_event(
                candidate.category, candidate.occurred_at, state.mentioned_at
            )
        ]

        # Rank both pools; F6b keeps the hit_count normalizer per-pool per-cycle.
        as_run_ranks_final = _rank_ids(as_run_ids, states, started, rem_zero=False)
        as_run_ranks_zero = _rank_ids(as_run_ids, states, started, rem_zero=True)
        healed_ranks_final = _rank_ids(healed_ids, states, started, rem_zero=False)

        predicted_final = _top_from_ranks(as_run_ranks_final, top_n)
        predicted_zero = _top_from_ranks(as_run_ranks_zero, top_n)

        tracked_ranks = {
            tid: TrackedRank(
                candidate_id=tid,
                as_run_rank=as_run_ranks_final.get(tid),
                as_run_eligible=tid in as_run_ranks_final,
                healed_rank=healed_ranks_final.get(tid),
                healed_eligible=tid in healed_ranks_final,
            )
            for tid in sorted(tracked_ids)
        }

        healed_set = set(healed_ids)
        inflow = len(healed_set - previous_healed_ids)
        previous_healed_ids = healed_set

        replays.append(
            CycleReplay(
                run_id=cycle.run_id,
                started_at=cycle.started_at,
                phase_inferred=cycle.phase_inferred,
                promotions_recorded=cycle.promotions,
                as_run_pool_size=len(as_run_ids),
                healed_pool_size=len(healed_ids),
                newly_eligible_inflow=inflow,
                backlog_after=len(healed_ids) - cycle.promotions,
                saturated=cycle.promotions == top_n,
                predicted_as_run_rem_final=predicted_final,
                predicted_as_run_rem_zero=predicted_zero,
                prediction_overlap_rem_final=_overlap(predicted_final, actual_ids),
                prediction_overlap_rem_zero=_overlap(predicted_zero, actual_ids),
                tracked_ranks=tracked_ranks,
            )
        )
    return replays


def _overlap(predicted: list[int], actual: set[int]) -> float | None:
    """``|predicted ∩ actual| / |actual|``; ``None`` when no actual promotions."""
    if not actual:
        return None
    return len(set(predicted) & actual) / len(actual)


def _scoring_time(candidates: Sequence[_DiagnosticCandidate]) -> datetime:
    """Deterministic forward-sim initial clock: the pool's newest ``last_seen_at``.

    Matches the sibling harness (F7): the frozen runs never persisted a scoring
    clock, so a fixed in-pool anchor keeps the forward drain reproducible. An
    empty pool falls back to a fixed epoch for the same reason wall-clock now
    would drift across reruns.
    """
    if not candidates:
        return _EMPTY_POOL_SCORING_TIME
    return max(candidate.last_seen_at for candidate in candidates)


@dataclass(frozen=True)
class ForwardSimulation:
    """Quiescent forward drain of the healed final backlog (F9).

    From the healed final eligible pool, each round ranks the survivors, removes
    the top-N, and advances the clock by ``gap_days`` — no new mentions ever
    arrive, so the pool only shrinks. ``never_eligible`` are candidates the
    ``_deep_eligible`` gate rejects outright (no embedding, or an event still
    undated after the heal); ``unassigned`` are eligible candidates the round cap
    cut off before they drained.
    """

    eligible_pool_size: int
    rounds: int
    promotes_at_round: dict[int, int]
    never_eligible: list[int]
    unassigned: list[int]


def _forward_simulate(
    candidates: Sequence[_DiagnosticCandidate],
    *,
    top_n: int,
    max_rounds: int,
    gap_days: float,
) -> ForwardSimulation:
    """Drain the healed final backlog under quiescence (no new inflow, F9).

    ``candidates`` is the whole healed final pool (``_load_diagnostic_candidates``
    on the init_db'd copy); ``_deep_eligible`` selects the drainable pool and the
    rest are recorded ``never_eligible``. Each round scores the survivors at
    ``_scoring_time(pool) + (round - 1) * gap_days`` and promotes the top-N,
    id-ASC tie-broken exactly like ``select_promotions``, until the pool empties
    or ``max_rounds`` is reached.
    """
    eligible = _deep_eligible(candidates)
    eligible_ids = {candidate.candidate_id for candidate in eligible}
    never_eligible = sorted(
        candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_id not in eligible_ids
    )

    base = _scoring_time(eligible)
    remaining = list(eligible)
    promotes_at_round: dict[int, int] = {}
    round_number = 0
    while remaining and round_number < max_rounds:
        round_number += 1
        scoring_time = base + timedelta(days=(round_number - 1) * gap_days)
        ranks = _rank_diagnostic_candidates(remaining, scoring_time=scoring_time)
        promoted_now = {cid for cid, rank in ranks.items() if rank <= top_n}
        for cid in promoted_now:
            promotes_at_round[cid] = round_number
        remaining = [c for c in remaining if c.candidate_id not in promoted_now]

    return ForwardSimulation(
        eligible_pool_size=len(eligible),
        rounds=round_number,
        promotes_at_round=promotes_at_round,
        never_eligible=never_eligible,
        unassigned=sorted(c.candidate_id for c in remaining),
    )


# --------------------------------------------------------------------------- #
# Gap fixture, question orchestration, verdicts, CLI and artifacts (task 6).
# --------------------------------------------------------------------------- #

DEFAULT_MAX_FORWARD_ROUNDS = 50
# Disposable copy-first workspace prefix. Tracker-id free by design.
_WORKSPACE_PREFIX = "deep-backlog-replay-"
_METRICS_NAME = "deep_backlog_replay_metrics.json"
_TABLE_NAME = "deep_backlog_replay_table.md"


class GapFixtureError(ValueError):
    """A malformed gap fixture, or a gap candidate that no longer resolves."""


class Gap(BaseModel):
    """One missing constituent behind a class-3 miss (sibling fixture shape)."""

    model_config = {"extra": "forbid"}

    gap_id: str
    kind: str
    description: str = ""
    frozen_candidate_id: int | None = None
    frozen_fact_id: int | None = None
    match_tokens: list[str] = Field(default_factory=list)


class QuestionGaps(BaseModel):
    model_config = {"extra": "forbid"}

    question_id: str
    run_dir: str
    bucket: str
    question: str = ""
    gold_answer: Any = None
    gaps: list[Gap] = Field(default_factory=list)


def load_gap_fixture(path: Path) -> list[QuestionGaps]:
    """Parse and validate a gap fixture, or raise ``GapFixtureError``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GapFixtureError(f"could not read gap fixture {path}: {exc}") from exc
    questions = raw.get("questions") if isinstance(raw, dict) else raw
    if not isinstance(questions, list):
        raise GapFixtureError("gap fixture must be a list, or an object with 'questions'.")
    try:
        entries = [QuestionGaps.model_validate(item) for item in questions]
    except ValidationError as exc:
        raise GapFixtureError(f"invalid gap fixture: {exc}") from exc
    seen: set[str] = set()
    for entry in entries:
        if entry.question_id in seen:
            raise GapFixtureError(f"duplicate question_id in fixture: {entry.question_id}")
        seen.add(entry.question_id)
        gap_ids = [gap.gap_id for gap in entry.gaps]
        if len(gap_ids) != len(set(gap_ids)):
            raise GapFixtureError(f"duplicate gap_id under question {entry.question_id}")
    return entries


def question_db_path(entry: QuestionGaps) -> Path:
    """Resolve the frozen per-question memory.db, or fail loud."""
    db_path = Path(entry.run_dir) / _question_path_component(entry.question_id) / "memory.db"
    if not db_path.exists():
        raise GapFixtureError(f"question {entry.question_id}: no run DB at {db_path}")
    return db_path


def _copy_question_db(db_path: Path, destination: Path) -> Path:
    """Copy the frozen DB (plus any WAL sidecars) so mutation never touches it."""
    destination.mkdir(parents=True, exist_ok=True)
    copy_path = destination / db_path.name
    for suffix in ("", "-wal", "-shm"):
        source = db_path.with_name(db_path.name + suffix)
        if source.exists():
            destination_file = copy_path.with_name(copy_path.name + suffix)
            shutil.copy2(source, destination_file)
            # copy2 preserves the source mode; a read-only frozen artifact (0444)
            # yields a read-only copy that init_db cannot heal. Make it writable.
            destination_file.chmod(destination_file.stat().st_mode | stat.S_IWUSR)
    return copy_path


def _merge_event_count(conn: sqlite3.Connection) -> int:
    """Count reinforcing merge decisions (F5 real-data assertion)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM memory_dedup_events WHERE decision = 'merge'"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _hit_count_reconciles(conn: sqlite3.Connection) -> bool:
    """Every candidate's stored ``hit_count`` equals ``1 + its merge events``.

    Reconstruction-exactness check: a candidate is inserted at
    ``hit_count=1`` and each ``decision='merge'`` dedup event targeting it
    reinforces by one, so the stored count must equal ``1 + merge events`` for
    *every* candidate. A mismatch means the persisted state and the dedup-event
    log disagree, so per-cycle ``hit_count`` reconstruction cannot be trusted.
    """
    merge_counts: dict[int, int] = {}
    for (candidate_id,) in conn.execute(
        "SELECT candidate_id FROM memory_dedup_events WHERE decision = 'merge'"
    ):
        merge_counts[int(candidate_id)] = merge_counts.get(int(candidate_id), 0) + 1
    for candidate_id, hit_count in conn.execute(
        "SELECT id, hit_count FROM memory_candidates"
    ):
        if int(hit_count) != 1 + merge_counts.get(int(candidate_id), 0):
            return False
    return True


def _as_run_backlog(candidates: Sequence[_ReplayCandidate]) -> int:
    """Final as-run eligible-but-unpromoted count (stored ``mentioned_at``).

    Mirrors ``_deep_eligible`` on the un-healed copy: promoted candidates and
    unembedded rows are out, and an ``event`` still undated under its *stored*
    ``mentioned_at`` is excluded exactly as production Deep excluded it.
    """
    count = 0
    for candidate in candidates:
        if candidate.retired or candidate.stale or candidate.needs_review:
            continue
        if candidate.promoted or candidate.promoted_fact_id is not None:
            continue
        if not candidate.has_embedding:
            continue
        if _is_undated_event(
            candidate.category, candidate.occurred_at, candidate.mentioned_at
        ):
            continue
        count += 1
    return count


def _median_inter_cycle_gap_days(started_ats: Sequence[str]) -> float:
    """Median days between consecutive Deep cycles; 1.0 fallback under 2 cycles."""
    if len(started_ats) < 2:
        return 1.0
    times = [_timestamp_datetime(value) for value in started_ats]
    gaps = [
        (times[index] - times[index - 1]).total_seconds() / 86400
        for index in range(1, len(times))
    ]
    return float(median(gaps))


def _drain_verdict(
    *,
    attribution_consistent: bool,
    deep_cycles: int,
    final_backlog_healed: int,
    structural_starvation: bool,
) -> str:
    """Classify the backlog outcome from the measured series, not the forward sim.

    Gating first: when attribution is inconsistent the per-cycle reconstruction
    is untrustworthy, so no authoritative backlog classification is emitted --
    the verdict is ``unreliable-attribution``. Otherwise, per F9 the forward
    drain is near-tautological alone, so "transient" is never inferred from it: a
    run that actually cleared its healed backlog is ``drained-during-run``; a run
    whose per-cycle inflow structurally outpaces the cap (mean inflow >= top_n)
    is ``structural-starvation-during-ingestion``; any remaining backlog with
    sub-cap inflow is ``backlog-at-run-end-transient``.
    """
    if not attribution_consistent:
        return "unreliable-attribution"
    if deep_cycles == 0:
        return "no-deep-cycles"
    if final_backlog_healed <= 0:
        return "drained-during-run"
    if structural_starvation:
        return "structural-starvation-during-ingestion"
    return "backlog-at-run-end-transient"


def _tracked_candidate_verdict(
    *,
    attribution_consistent: bool,
    promoted_at_cycle: int | None,
    promoted_flag: bool,
    forward_promotes_at_round: int | None,
    undrained_at_round_cap: bool,
) -> str:
    """Classify one tracked gap candidate (measurement, not forward sim alone).

    Precedence: ``unreliable-attribution`` > ``promoted-historically`` >
    ``promoted-unattributed`` > ``promotes-under-quiescence`` >
    ``undrained-at-round-cap`` > ``never-eligible``. Gating first -- an
    inconsistent attribution reconstruction cannot support an authoritative
    per-candidate verdict. ``promoted-unattributed`` catches a candidate whose
    final promoted flag (or ``promoted_fact_id``) is set but which attribution
    never placed in a Deep cycle, so it is not silently demoted to
    ``never-eligible``. ``undrained-at-round-cap`` distinguishes an eligible
    candidate the forward round cap cut off (present in
    ``ForwardSimulation.unassigned``) from one the ``_deep_eligible`` gate
    rejects outright: the former would still drain given more rounds.
    """
    if not attribution_consistent:
        return "unreliable-attribution"
    if promoted_at_cycle is not None:
        return "promoted-historically"
    if promoted_flag:
        return "promoted-unattributed"
    if forward_promotes_at_round is not None:
        return "promotes-under-quiescence"
    if undrained_at_round_cap:
        return "undrained-at-round-cap"
    return "never-eligible"


def _cycle_to_dict(cycle: CycleReplay) -> dict[str, Any]:
    """Serialize one per-cycle replay record (deterministic key order)."""
    return {
        "run_id": cycle.run_id,
        "started_at": cycle.started_at,
        "phase_inferred": cycle.phase_inferred,
        "promotions_recorded": cycle.promotions_recorded,
        "as_run_pool_size": cycle.as_run_pool_size,
        "healed_pool_size": cycle.healed_pool_size,
        "newly_eligible_inflow": cycle.newly_eligible_inflow,
        "backlog_after": cycle.backlog_after,
        "saturated": cycle.saturated,
        "predicted_as_run_rem_final": cycle.predicted_as_run_rem_final,
        "predicted_as_run_rem_zero": cycle.predicted_as_run_rem_zero,
        "prediction_overlap_rem_final": cycle.prediction_overlap_rem_final,
        "prediction_overlap_rem_zero": cycle.prediction_overlap_rem_zero,
    }


def _tracked_candidate_result(
    candidate_id: int,
    *,
    cycles: list[CycleReplay],
    promoted_at_cycle: int | None,
    promoted_flag: bool,
    forward: ForwardSimulation,
    top_n: int,
    attribution_consistent: bool,
) -> dict[str, Any]:
    """Per-tracked-candidate record: trajectory both modes + verdict."""
    trajectory = []
    starved = False
    for cycle in cycles:
        tracked = cycle.tracked_ranks.get(candidate_id)
        if tracked is None:
            continue
        trajectory.append(
            {
                "run_id": cycle.run_id,
                "started_at": cycle.started_at,
                "as_run_rank": tracked.as_run_rank,
                "as_run_eligible": tracked.as_run_eligible,
                "healed_rank": tracked.healed_rank,
                "healed_eligible": tracked.healed_eligible,
                "saturated": cycle.saturated,
            }
        )
        if (
            tracked.healed_eligible
            and tracked.healed_rank is not None
            and tracked.healed_rank > top_n
            and cycle.saturated
        ):
            starved = True
    forward_round = forward.promotes_at_round.get(candidate_id)
    undrained_at_round_cap = candidate_id in forward.unassigned
    return {
        "candidate_id": candidate_id,
        "promoted_at_cycle": promoted_at_cycle,
        "forward_promotes_at_round": forward_round,
        "starved_while_ingesting": starved,
        "verdict": _tracked_candidate_verdict(
            attribution_consistent=attribution_consistent,
            promoted_at_cycle=promoted_at_cycle,
            promoted_flag=promoted_flag,
            forward_promotes_at_round=forward_round,
            undrained_at_round_cap=undrained_at_round_cap,
        ),
        "rank_trajectory": trajectory,
    }


def replay_question(
    entry: QuestionGaps,
    *,
    workspace: Path,
    top_n: int = DEFAULT_TOP_N,
    max_forward_rounds: int = DEFAULT_MAX_FORWARD_ROUNDS,
    forward_cycle_gap_days: float | None = None,
) -> dict[str, Any]:
    """Replay one question: as-run reconstruction + healed forward drain.

    Copy-first (both legs): the as-run reconstruction reads the raw copy exactly
    as production saw it (stored ``mentioned_at``), while the forward drain runs
    on a second copy healed by ``init_db`` (ADR 0037) so the healed backlog is
    the counterfactual pool. The frozen artifact is never opened.
    """
    db_path = question_db_path(entry)
    component = _question_path_component(entry.question_id)
    tracked_ids = [
        gap.frozen_candidate_id
        for gap in entry.gaps
        if gap.frozen_candidate_id is not None
    ]

    # As-run leg: raw copy, reconstruct per-cycle pools + tracked trajectories.
    # The timeline and attribution joins are reconstructed once here and threaded
    # into _replay_cycles so it does not recompute them internally.
    as_run_copy = _copy_question_db(db_path, workspace / component / "as-run")
    timeline = _dream_cycle_timeline(as_run_copy)
    attribution = _promotion_events(as_run_copy, timeline.deep_cycles)
    cycles = _replay_cycles(
        as_run_copy,
        top_n=top_n,
        tracked_ids=tracked_ids,
        timeline=timeline,
        attribution=attribution,
    )
    with _connection(as_run_copy) as conn:
        as_run_candidates = _load_replay_candidates(conn)
        merge_event_count = _merge_event_count(conn)
        hit_count_reconciles = _hit_count_reconciles(conn)
    final_backlog_as_run = _as_run_backlog(as_run_candidates)
    candidate_ids = {candidate.candidate_id for candidate in as_run_candidates}
    # Final promoted flag per candidate: the promoted boolean or a non-null
    # promoted_fact_id. Threaded to the tracked verdict so a candidate promoted
    # per its flags but unplaced by attribution is not read as never-eligible.
    promoted_flags = {
        candidate.candidate_id: (
            candidate.promoted or candidate.promoted_fact_id is not None
        )
        for candidate in as_run_candidates
    }

    # A gap fixture that names a candidate absent from this DB is stale; fail
    # loud (sibling harness behavior) rather than silently tracking nothing.
    for gap in entry.gaps:
        if gap.frozen_candidate_id is None:
            continue
        if gap.frozen_candidate_id not in candidate_ids:
            raise GapFixtureError(
                f"question {entry.question_id}: gap {gap.gap_id} names candidate "
                f"{gap.frozen_candidate_id}, which is not in {db_path}"
            )

    # Healed leg: second copy, init_db backfill, quiescent forward drain.
    healed_copy = _copy_question_db(db_path, workspace / component / "healed")
    init_db(str(healed_copy))
    healed_candidates = _load_diagnostic_candidates(healed_copy)
    gap_days = (
        forward_cycle_gap_days
        if forward_cycle_gap_days is not None
        else _median_inter_cycle_gap_days([cycle.started_at for cycle in cycles])
    )
    forward = _forward_simulate(
        healed_candidates,
        top_n=top_n,
        max_rounds=max_forward_rounds,
        gap_days=gap_days,
    )
    final_backlog_healed = forward.eligible_pool_size

    deep_cycles = len(cycles)
    saturated_cycles = sum(1 for cycle in cycles if cycle.saturated)
    # Sustained refill rate: the first Deep cycle's inflow is the initial pool
    # load, not refill *during* ingestion, so it is excluded. Structural
    # starvation (F9) is about refill outpacing drain across the run, which only
    # a second-cycle-onward inflow at/above the cap can demonstrate; a lone
    # saturated cycle that leaves a backlog is a run that simply ended early.
    refill_cycles = cycles[1:]
    mean_inflow = (
        sum(cycle.newly_eligible_inflow for cycle in refill_cycles) / len(refill_cycles)
        if refill_cycles
        else 0.0
    )
    structural_starvation = bool(refill_cycles) and mean_inflow >= top_n
    forward_rounds_to_drain = None if forward.unassigned else forward.rounds

    summary = {
        "merge_event_count": merge_event_count,
        "deep_cycles": deep_cycles,
        "saturated_cycles": saturated_cycles,
        "mean_eligible_inflow_per_cycle": mean_inflow,
        "deep_top_n": top_n,
        "structural_starvation_condition": structural_starvation,
        "final_backlog_as_run": final_backlog_as_run,
        "final_backlog_healed": final_backlog_healed,
        "forward_cycle_gap_days": gap_days,
        "forward_rounds_to_drain": forward_rounds_to_drain,
        "attribution_consistent": attribution.attribution_consistent,
        # Reconstruction-exactness. When merges exist, final-state
        # importance/occurred_at/source_message_ids are approximations, so
        # state_reconstruction_exact tells readers the per-cycle pool is bounded,
        # not exact; it is true only for a zero-merge DB whose hit_count log
        # reconciles.
        "hit_count_reconciles": hit_count_reconciles,
        "state_reconstruction_exact": merge_event_count == 0 and hit_count_reconciles,
        "drain_verdict": _drain_verdict(
            attribution_consistent=attribution.attribution_consistent,
            deep_cycles=deep_cycles,
            final_backlog_healed=final_backlog_healed,
            structural_starvation=structural_starvation,
        ),
    }

    tracked_candidates = [
        _tracked_candidate_result(
            candidate_id,
            cycles=cycles,
            promoted_at_cycle=attribution.by_candidate.get(candidate_id),
            promoted_flag=bool(promoted_flags.get(candidate_id, False)),
            forward=forward,
            top_n=top_n,
            attribution_consistent=attribution.attribution_consistent,
        )
        for candidate_id in tracked_ids
    ]

    return {
        "question_id": entry.question_id,
        "bucket": entry.bucket,
        "run_dir": entry.run_dir,
        "summary": summary,
        "cycles": [_cycle_to_dict(cycle) for cycle in cycles],
        "tracked_candidates": tracked_candidates,
    }


def _fmt(value: object) -> str:
    """Compact markdown cell, rounding floats to keep the table stable."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render_table(results: list[dict[str, Any]]) -> str:
    """Render one summary row per question plus one row per tracked candidate."""
    lines = [
        "# Deep backlog replay",
        "",
        "## Per-question backlog drain",
        "",
        "| question | bucket | deep cycles | saturated | mean inflow "
        "| backlog (healed) | forward rounds | verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        summary = result["summary"]
        lines.append(
            f"| {result['question_id']} | {result['bucket']} "
            f"| {_fmt(summary['deep_cycles'])} | {_fmt(summary['saturated_cycles'])} "
            f"| {_fmt(summary['mean_eligible_inflow_per_cycle'])} "
            f"| {_fmt(summary['final_backlog_healed'])} "
            f"| {_fmt(summary['forward_rounds_to_drain'])} "
            f"| {_fmt(summary['drain_verdict'])} |"
        )
    lines += [
        "",
        "## Per-tracked-candidate outcome",
        "",
        "| question | candidate | promoted at cycle | forward round "
        "| starved | verdict |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    any_tracked = False
    for result in results:
        for tracked in result["tracked_candidates"]:
            any_tracked = True
            lines.append(
                f"| {result['question_id']} | {_fmt(tracked['candidate_id'])} "
                f"| {_fmt(tracked['promoted_at_cycle'])} "
                f"| {_fmt(tracked['forward_promotes_at_round'])} "
                f"| {_fmt(tracked['starved_while_ingesting'])} "
                f"| {_fmt(tracked['verdict'])} |"
            )
    if not any_tracked:
        lines.append("| (none) | - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-question drain verdicts and tracked-candidate verdicts."""
    verdicts = [result["summary"]["drain_verdict"] for result in results]
    tracked = [
        candidate
        for result in results
        for candidate in result["tracked_candidates"]
    ]
    return {
        "questions": len(results),
        "drained_during_run": verdicts.count("drained-during-run"),
        "backlog_at_run_end_transient": verdicts.count("backlog-at-run-end-transient"),
        "structural_starvation_during_ingestion": verdicts.count(
            "structural-starvation-during-ingestion"
        ),
        "no_deep_cycles": verdicts.count("no-deep-cycles"),
        # Attribution gating can appear at both granularities: this key
        # counts the questions whose drain verdict was gated. Every tracked
        # candidate under a gated question is gated too, visible per-candidate.
        "unreliable_attribution": verdicts.count("unreliable-attribution"),
        "tracked_candidates": len(tracked),
        "promoted_historically": sum(
            1 for candidate in tracked if candidate["verdict"] == "promoted-historically"
        ),
        "promoted_unattributed": sum(
            1 for candidate in tracked if candidate["verdict"] == "promoted-unattributed"
        ),
        "promotes_under_quiescence": sum(
            1 for candidate in tracked if candidate["verdict"] == "promotes-under-quiescence"
        ),
        "undrained_at_round_cap": sum(
            1 for candidate in tracked if candidate["verdict"] == "undrained-at-round-cap"
        ),
        "never_eligible": sum(
            1 for candidate in tracked if candidate["verdict"] == "never-eligible"
        ),
    }


def _write_artifacts(out_dir: Path, doc: dict[str, Any], table: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / _METRICS_NAME).write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / _TABLE_NAME).write_text(table, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay Deep-cycle promotion over frozen LongMemEval run DBs and "
            "measure whether a healed candidate backlog drains or starves."
        )
    )
    parser.add_argument("--gaps", required=True, type=Path, help="Gap fixture JSON.")
    parser.add_argument(
        "--out", type=Path, default=None, help="Directory for the JSON/markdown artifacts."
    )
    parser.add_argument("--deep-top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument(
        "--question-id",
        action="append",
        default=[],
        help="Restrict the run to these question ids. Repeatable.",
    )
    parser.add_argument(
        "--max-forward-rounds", type=int, default=DEFAULT_MAX_FORWARD_ROUNDS
    )
    parser.add_argument(
        "--forward-cycle-gap-days",
        type=float,
        default=None,
        help="Days between quiescent forward rounds (default: median historical "
        "inter-Deep-cycle gap, or 1.0 with fewer than two Deep cycles).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.deep_top_n <= 0:
        print(
            f"--deep-top-n must be positive, got {args.deep_top_n}",
            file=sys.stderr,
        )
        return 2
    try:
        entries = load_gap_fixture(args.gaps)
    except GapFixtureError as exc:
        print(f"gap fixture error: {exc}", file=sys.stderr)
        return 2
    if args.question_id:
        wanted = set(args.question_id)
        entries = [entry for entry in entries if entry.question_id in wanted]
        missing = sorted(wanted - {entry.question_id for entry in entries})
        if missing:
            print(f"question ids not in fixture: {', '.join(missing)}", file=sys.stderr)
            return 2

    results: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix=_WORKSPACE_PREFIX) as tmp:
        workspace = Path(tmp)
        try:
            for entry in entries:
                results.append(
                    replay_question(
                        entry,
                        workspace=workspace,
                        top_n=args.deep_top_n,
                        max_forward_rounds=args.max_forward_rounds,
                        forward_cycle_gap_days=args.forward_cycle_gap_days,
                    )
                )
        except GapFixtureError as exc:
            print(f"gap fixture error: {exc}", file=sys.stderr)
            return 2

    table = render_table(results)
    doc = {
        "deep_top_n": args.deep_top_n,
        "max_forward_rounds": args.max_forward_rounds,
        "summary": summarize(results),
        "questions": results,
    }
    if args.out is not None:
        _write_artifacts(args.out, doc, table)
    print(table)
    print(json.dumps(doc["summary"], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
