"""Oracle-evidence experiment: size retrieval set-completeness vs answer-time
derivation over existing LongMemEval run DBs (COA follow-on to the miss-class
analysis).

Read-only over run artifacts. The ONLY live provider calls are recall-judge
invocations; every other step (fused[:k] reconstruction, constituent capture,
pre-fusion-pool ceiling, headroom membership sets) is deterministic and runs
under --bind-only with no provider access.

For each hand-curated class-3 miss the harness builds several evidence sets and
scores each with the existing LongMemEval recall judge:

  * oracle    -- the hand-selected constituent facts (combined ceiling: complete
                 evidence + judge derivation).
  * baseline  -- reconstructed fused[:5], the fused top-5 the run actually
                 returned (re-judged, informational; N is defined by the run's
                 RECORDED verdict, never this re-judge).
  * sweep k   -- reconstructed fused[:k] for k in {8, 10, 15}, plus the
                 deterministic constituent-capture fraction at each k.

Pass = judge verdict == "supported" only, matching longmemeval.py's
`judged_recall_pass`; a "partial" verdict is a miss and is reported separately.

Gated behind --allow-live and a provider-call budget cap, mirroring
src/vexic/live_retrieval_baseline.py and scripts/ablate_extraction_prompts.py.
The harness lives under scripts/ (outside the vexic package boundary) and imports
only public vexic.* plus the read-only analysis helpers.

The oracle fixture is a hand-curated, run-local evidence artifact: it references
long_term_memory rowids inside frozen .eval-runs/** DBs (git-ignored, not
reproducible in CI), so it is attached to the PR/issue rather than committed. The
deterministic path exercised by tests builds its own synthetic run DB.

Example -- deterministic capture/ceiling table, no provider calls, no
--allow-live needed:

    uv run python scripts/oracle_evidence_experiment.py --bind-only \\
        --oracle-fixture .eval-runs/oracle/curated.json

Live judged run (spends provider budget):

    OPENROUTER_API_KEY=... uv run python scripts/oracle_evidence_experiment.py \\
        --allow-live --adapter adapters/openrouter_live_adapter.py \\
        --oracle-fixture .eval-runs/oracle/curated.json \\
        --out .eval-runs/oracle --max-provider-calls 250 --repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra_path in (str(_REPO_ROOT / "src"), str(_REPO_ROOT)):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

from contextlib import closing  # noqa: E402

from vexic.longmemeval_analysis import (  # noqa: E402
    _answer_retrieval_arrays,
    _open_readonly,
    _question_path_component,
)
from vexic.contract import MemoryCategory  # noqa: E402
from vexic.longmemeval import (  # noqa: E402
    LongMemEvalRecallJudgeInput,
    LongMemEvalRecallJudgeVerdict,
)
from vexic.subagents.retrieval import (  # noqa: E402
    RETURN_K,
    reciprocal_rank_fusion,
)

# An async judge: question + gold answer + retrieved fact texts -> verdict.
# The live wiring wraps score_longmemeval_recall; tests inject a stub.
JudgeFn = Callable[
    [LongMemEvalRecallJudgeInput], Awaitable[LongMemEvalRecallJudgeVerdict]
]

# Return-k values the sweep judges. RETURN_K (=5) is the run's actual fused
# top-k = the baseline; the wider values probe how much of the curated oracle
# set widening retrieval surfaces. All stay within the persisted RETRIEVE_K
# (=20) per-retriever pool, so fused[:k] reconstructs offline with no re-embed.
# dict.fromkeys dedups if RETURN_K is ever set to one of the wider values, so a
# k is never judged twice (wasting repeats on an identical slice).
SWEEP_K_VALUES = tuple(dict.fromkeys((RETURN_K, 8, 10, 15)))


class OracleFixtureError(ValueError):
    """A malformed oracle fixture, or a curated fact that no longer resolves to
    its recorded text (rowid drift)."""


class OracleEntry(BaseModel):
    """One hand-curated class-3 miss: which facts, in which frozen run DB, make
    up the oracle-complete evidence set for this question."""

    model_config = {"extra": "forbid"}

    question_id: str
    run_dir: str
    question: str
    # The LongMemEval question_type, threaded into the recall-judge input so a
    # single-session-preference oracle case is judged under the same
    # rubric-aware render as the main eval path (not the literal render).
    # Optional for back-compat: fixtures authored before rubric judging omit it
    # and judge with question_type None (the pre-rubric behavior).
    question_type: str | None = None
    gold_answer: Any
    constituent_fact_ids: list[int] = Field(min_length=1)
    expected_fact_texts: list[str] = Field(min_length=1)
    # False when the curated Tier-3 set does NOT fully cover the gold answer --
    # a needed constituent was never promoted (undated Tier-2 candidate, or
    # transcript/tabular-only). Such a miss is bounded by extraction/promotion,
    # not retrieval or derivation, so headroom attributes it separately.
    oracle_complete: bool = True
    note: str = ""

    def model_post_init(self, _context: Any) -> None:
        if len(self.constituent_fact_ids) != len(self.expected_fact_texts):
            raise ValueError(
                "constituent_fact_ids and expected_fact_texts must be the same "
                f"length (question {self.question_id!r}: "
                f"{len(self.constituent_fact_ids)} vs "
                f"{len(self.expected_fact_texts)})"
            )
        if len(set(self.constituent_fact_ids)) != len(self.constituent_fact_ids):
            # A duplicated id double-counts in constituent_capture and feeds the
            # judge the same fact twice, silently distorting the oracle set.
            raise ValueError(
                f"question {self.question_id!r}: duplicate constituent_fact_ids "
                f"{self.constituent_fact_ids}"
            )


def load_oracle_fixture(path: Path) -> list[OracleEntry]:
    """Parse the hand-authored oracle fixture into typed entries.

    Fails loudly (OracleFixtureError) on any shape problem: the fixture is
    hand-curated evidence and a silent skip would understate the miss set.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise OracleFixtureError("oracle fixture must be a JSON list of entries")
    entries: list[OracleEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise OracleFixtureError(f"entry {index} is not an object")
        try:
            entry = OracleEntry.model_validate(item)
        except ValidationError as exc:
            raise OracleFixtureError(f"entry {index} is invalid: {exc}") from exc
        if entry.question_id in seen:
            raise OracleFixtureError(
                f"duplicate question_id in fixture: {entry.question_id!r}"
            )
        seen.add(entry.question_id)
        entries.append(entry)
    return entries


def _memory_db_path(entry: OracleEntry) -> Path:
    """The per-question memory.db inside the entry's pinned run dir. Pinning the
    run_dir per entry keys each question to one authoritative run (resolving
    retry dedup) and keeps rowids stable, since these frozen .eval-runs DBs are
    never re-run."""
    return (
        Path(entry.run_dir)
        / _question_path_component(entry.question_id)
        / "memory.db"
    )


def resolve_constituents(entry: OracleEntry) -> list[str]:
    """Fetch the live fact_text for each curated constituent id, read-only.

    Drift guard (fails loud, OracleFixtureError): a curated id missing from the
    DB, OR a live text that no longer matches (normalized) its recorded
    expected_fact_text. The text check is what catches a rowid *reassigned* to a
    different fact by a re-run -- recording the expected text is not enough; it
    must be validated.
    """
    db_path = _memory_db_path(entry)
    if not db_path.exists():
        raise OracleFixtureError(
            f"memory.db not found for {entry.question_id!r}: {db_path}"
        )
    texts: list[str] = []
    with closing(_open_readonly(db_path)) as conn:
        for fact_id, expected in zip(
            entry.constituent_fact_ids, entry.expected_fact_texts, strict=True
        ):
            row = conn.execute(
                "SELECT fact_text FROM long_term_memory WHERE id = ? AND retired = 0",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise OracleFixtureError(
                    f"{entry.question_id!r}: curated fact id {fact_id} is not a "
                    "live fact in the run DB (deleted or retired)"
                )
            live = row[0]
            # Exact equality, not normalized: a rowid reassigned to a fact that
            # differs only in case or whitespace would slip a normalized guard.
            # The fixture copies fact_text verbatim from this same DB, so exact
            # match is the correct, strictly-safer check (normalize would only
            # widen the accept set). A mismatch fails loud; fix the fixture.
            if live != expected:
                raise OracleFixtureError(
                    f"{entry.question_id!r}: fact id {fact_id} drifted -- "
                    f"expected {expected!r}, DB now holds {live!r}"
                )
            texts.append(live)
    return texts


def _read_answer_arrays(
    entry: OracleEntry,
) -> tuple[list[int], list[int], list[int]]:
    """The persisted per-retriever pools (keyword, vector) and the stored fused
    top-k from the run's answer retrieval event."""
    db_path = _memory_db_path(entry)
    if not db_path.exists():
        raise OracleFixtureError(
            f"memory.db not found for {entry.question_id!r}: {db_path}"
        )
    with closing(_open_readonly(db_path)) as conn:
        return _answer_retrieval_arrays(conn, entry.question_id)


def load_retrieval_arrays(entry: OracleEntry) -> tuple[list[int], list[int]]:
    """The persisted per-retriever pools (keyword, vector) from the run's answer
    retrieval event. Each is the full RETRIEVE_K-wide ranking, so fused[:k] is
    reconstructable offline for any k <= pool size."""
    keyword_ids, vector_ids, _fused_stored = _read_answer_arrays(entry)
    return keyword_ids, vector_ids


def preflight(entries: list[OracleEntry]) -> None:
    """Validate every entry deterministically BEFORE any judge call, so a
    fixture defect on a late entry fails loud at exit 2 without burning provider
    budget or emitting a partial run.

    Per entry: the drift guard (resolve_constituents), the answer retrieval
    event exists with a non-empty pool when constituents are present, and the
    offline fused reconstruction reproduces exactly what the run stored -- the
    experiment's central faithfulness premise. Any mismatch means RRF wiring,
    tie-break, or the pool JSON drifted from the frozen run, so the reconstructed
    baseline/sweep would look authoritative while differing from what production
    returned.
    """
    for entry in entries:
        resolve_constituents(entry)
        keyword_ids, vector_ids, fused_stored = _read_answer_arrays(entry)
        if entry.constituent_fact_ids and not (keyword_ids or vector_ids):
            raise OracleFixtureError(
                f"{entry.question_id!r}: no answer retrieval event / empty "
                "keyword+vector pools, so fused[:k] cannot be reconstructed -- "
                "judging empty sets would fabricate a false 'derivation needed' "
                "result. Check run_dir/question_id (and candidate-fallback rows)."
            )
        recon = reconstruct_fused(keyword_ids, vector_ids, len(fused_stored))
        if recon != fused_stored:
            raise OracleFixtureError(
                f"{entry.question_id!r}: reconstructed fused {recon} != stored "
                f"fused {fused_stored}; RRF wiring drifted from the frozen run."
            )


def reconstruct_fused(
    keyword_ids: list[int], vector_ids: list[int], k: int
) -> list[int]:
    """Reciprocal-rank-fuse the two persisted pools and take the top k -- the
    exact fused[:k] production would return at return_k=k (ADR 0037 event
    reordering only permutes within this set, so membership is faithful)."""
    return reciprocal_rank_fusion([list(keyword_ids), list(vector_ids)])[:k]


def constituent_capture(
    constituent_ids: list[int], fused_k_ids: list[int]
) -> dict[str, Any]:
    """How much of the curated oracle set a fused[:k] slice surfaces. Measured
    over the curated constituents because class-3 aggregation misses have empty
    classifier gold_fact_ids."""
    fused_set = set(fused_k_ids)
    captured = sum(1 for fact_id in constituent_ids if fact_id in fused_set)
    total = len(constituent_ids)
    return {
        "captured": captured,
        "total": total,
        "fraction": captured / total if total else None,
        "retrieved_count": len(fused_k_ids),
    }


def pool_ceiling(
    constituent_ids: list[int], keyword_ids: list[int], vector_ids: list[int]
) -> list[int]:
    """Curated constituents outside the pre-fusion pool union (keyword U vector):
    facts beyond the RETRIEVE_K pool that no return_k widening can ever surface.
    Computed over curated constituents -- the classifier's outside_retrieve_k is
    defined over the empty gold_fact_ids and cannot be reused here."""
    pool = set(keyword_ids) | set(vector_ids)
    return [fact_id for fact_id in constituent_ids if fact_id not in pool]


@dataclass(frozen=True)
class _FactRow:
    """Just the columns the event-time reorder needs, duck-typed to match the
    LongTermFact attributes _with_events_sorted reads."""

    fact_id: int
    fact_text: str
    category: str
    occurred_at: str | None
    mentioned_at: str | None
    created_at: str


def _event_sorted(facts: list[_FactRow]) -> list[_FactRow]:
    """Reorder event-category facts by event time, newest first, leaving
    non-event facts in their relevance slots.

    Copied from vexic.subagents.retrieval._with_events_sorted (ADR 0037);
    scripts stay standalone rather than import a private helper. The sort key
    is occurred_at, falling back to mentioned_at then created_at, truncated to
    day grain; Python's stable sort keeps equal-key events in RRF order.
    """
    result = list(facts)
    positions = [
        index
        for index, fact in enumerate(result)
        if fact.category == MemoryCategory.EVENT.value
    ]
    if len(positions) < 2:
        return result
    ordered = sorted(
        (result[index] for index in positions),
        key=lambda fact: (
            (fact.occurred_at or "").strip()
            or (fact.mentioned_at or "").strip()
            or fact.created_at
        )[:10],
        reverse=True,
    )
    for position, fact in zip(positions, ordered):
        result[position] = fact
    return result


def _fetch_fact_rows(entry: OracleEntry, fact_ids: list[int]) -> list[_FactRow]:
    """Read the columns _event_sorted needs for the given ids, preserving the
    input (fused-rank) order. No retired filter: production fetch_long_term_facts
    selects purely by id (storage/longterm.py), so a fused id pointing at a
    retired fact is still presented -- matching that keeps the judged evidence
    set faithful to what production would return. Ids with no row are dropped,
    never fabricated.

    Optional sort columns are probed rather than assumed: frozen run DBs are
    pinned at whatever schema they were created with, and mentioned_at predates
    only the ADR 0037 migration, so an older DB lacks it. A missing column reads
    as None, which _event_sorted already treats as "fall through to the next
    rung" -- exactly how production behaved before the column existed."""
    if not fact_ids:
        return []
    db_path = _memory_db_path(entry)
    with closing(_open_readonly(db_path)) as conn:
        present = {
            row[1]
            for row in conn.execute("PRAGMA table_info(long_term_memory)")
        }
        optional = [c for c in ("occurred_at", "mentioned_at") if c in present]
        columns = ["id", "fact_text", "category", *optional, "created_at"]
        select = f"SELECT {', '.join(columns)} FROM long_term_memory WHERE id = ?"
        by_id: dict[int, _FactRow] = {}
        for fact_id in fact_ids:
            row = conn.execute(select, (fact_id,)).fetchone()
            if row is not None:
                values = dict(zip(columns, row))
                by_id[fact_id] = _FactRow(
                    fact_id=values["id"],
                    fact_text=values["fact_text"],
                    category=values["category"],
                    occurred_at=values.get("occurred_at"),
                    mentioned_at=values.get("mentioned_at"),
                    created_at=values["created_at"],
                )
    return [by_id[fact_id] for fact_id in fact_ids if fact_id in by_id]


def condition_fact_texts(entry: OracleEntry, fact_ids: list[int]) -> list[str]:
    """The fact texts for a condition's id set, in production presentation order
    (fused rank with the ADR 0037 event-time reorder applied)."""
    return [row.fact_text for row in _event_sorted(_fetch_fact_rows(entry, fact_ids))]


class ProviderBudgetExhausted(RuntimeError):
    pass


class ProviderBudget:
    # Copied from scripts/ablate_extraction_prompts.py; scripts stay standalone.
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.used = 0

    def remaining(self) -> int:
        return self.max_calls - self.used

    def take(self) -> None:
        if self.remaining() <= 0:
            raise ProviderBudgetExhausted(
                f"provider call cap exceeded: {self.used}/{self.max_calls}"
            )
        self.used += 1


def pass_fraction(verdicts: list[str]) -> float | None:
    """Fraction of verdicts that are "supported". Matches longmemeval.py's
    judged_recall_pass -- "partial" is a miss, never a pass."""
    if not verdicts:
        return None
    return sum(1 for v in verdicts if v == "supported") / len(verdicts)


def partial_fraction(verdicts: list[str]) -> float | None:
    if not verdicts:
        return None
    return sum(1 for v in verdicts if v == "partial") / len(verdicts)


async def _judge_texts(
    judge_fn: JudgeFn,
    *,
    question: str,
    gold_answer: Any,
    fact_texts: list[str],
    repeats: int,
    budget: ProviderBudget,
    question_type: str | None = None,
) -> dict[str, Any]:
    """Judge one evidence set `repeats` times and aggregate. A single-shot LLM
    verdict is noisy even at temperature 0, so headroom is read off the pass
    fraction, not one verdict.

    A transient judge failure (rate limit, unparseable structured output) on one
    repeat must not abort the whole budgeted run and discard every completed
    question: the error is recorded and the loop continues, mirroring the
    per-call resilience in scripts/ablate_extraction_prompts.py. Pass/partial
    fractions are computed over the graded (non-error) repeats -- all-error means
    no signal (None), like production's judge_error -> judged_recall_pass None.
    The budget call is still counted; the provider may have charged for it.
    """
    judge_input = LongMemEvalRecallJudgeInput(
        question=question,
        gold_answer=gold_answer,
        retrieved_fact_texts=tuple(fact_texts),
        question_type=question_type,
    )
    verdicts: list[str] = []
    errors = 0
    for _ in range(repeats):
        budget.take()
        try:
            verdict = await judge_fn(judge_input)
            verdicts.append(verdict.verdict)
        except ProviderBudgetExhausted:
            raise
        except Exception:  # noqa: BLE001 - live judge boundary
            errors += 1
            verdicts.append("error")
    graded = [v for v in verdicts if v != "error"]
    return {
        "verdicts": verdicts,
        "pass_fraction": pass_fraction(graded),
        "partial_fraction": partial_fraction(graded),
        "n": len(verdicts),
        "errors": errors,
        "retrieved_count": len(fact_texts),
    }


async def run_question(
    entry: OracleEntry,
    judge_fn: JudgeFn,
    *,
    repeats: int,
    budget: ProviderBudget,
) -> dict[str, Any]:
    """Score every condition for one curated miss: oracle (combined ceiling),
    baseline fused[:5], and the fused[:k] sweep, plus the deterministic
    constituent-capture and pre-fusion-pool ceiling."""
    resolve_constituents(entry)  # drift guard: fail loud before spending budget
    keyword_ids, vector_ids = load_retrieval_arrays(entry)

    oracle_texts = condition_fact_texts(entry, entry.constituent_fact_ids)
    oracle = await _judge_texts(
        judge_fn,
        question=entry.question,
        gold_answer=entry.gold_answer,
        fact_texts=oracle_texts,
        repeats=repeats,
        budget=budget,
        question_type=entry.question_type,
    )

    sweep: dict[str, Any] = {}
    capture: dict[str, Any] = {}
    for k in SWEEP_K_VALUES:
        fused_k = reconstruct_fused(keyword_ids, vector_ids, k)
        capture[str(k)] = constituent_capture(entry.constituent_fact_ids, fused_k)
        fused_texts = condition_fact_texts(entry, fused_k)
        sweep[str(k)] = await _judge_texts(
            judge_fn,
            question=entry.question,
            gold_answer=entry.gold_answer,
            fact_texts=fused_texts,
            repeats=repeats,
            budget=budget,
            question_type=entry.question_type,
        )

    return {
        "question_id": entry.question_id,
        "run_dir": entry.run_dir,
        "note": entry.note,
        "oracle_complete": entry.oracle_complete,
        "oracle": oracle,
        "baseline": sweep[str(RETURN_K)],
        "sweep": sweep,
        "capture": capture,
        "pool_ceiling": pool_ceiling(
            entry.constituent_fact_ids, keyword_ids, vector_ids
        ),
    }


def _sweep_pass_fractions(result: dict[str, Any]) -> list[tuple[int, float | None]]:
    return [
        (int(k), cond.get("pass_fraction"))
        for k, cond in sorted(result["sweep"].items(), key=lambda kv: int(kv[0]))
    ]


def build_headroom(
    results: list[dict[str, Any]], *, threshold: float = 0.5
) -> dict[str, Any]:
    """Decompose the recall headroom over N (every curated recorded miss) as
    membership sets, never as a subtractive residual -- under judge noise a
    "combined minus set-completeness" number can go negative, so the raw sets
    and their overlaps are reported instead.

    A condition "passes" when its pass fraction over repeats is >= threshold.

      * set_completeness_reachable -- some sweep k GREATER THAN RETURN_K passes:
        actually widening retrieval (past the run's top-5) surfaces enough for
        the current judge to flip it, with no explicit derivation step. The
        baseline k=RETURN_K re-judge is deliberately excluded here -- a k=5 pass
        on a recorded miss is judge noise on the same evidence, not widening.
      * baseline_rejudge_pass -- the k=RETURN_K re-judge passes despite the run
        recording a miss: single-shot judge non-determinism, reported so it is
        not mistaken for a headroom gain.
      * combined_ceiling -- the oracle condition passes (complete evidence +
        judge derivation): the upper bound.
      * derivation_needed -- combined_ceiling minus set_completeness_reachable,
        reported as a set difference: oracle passes but no widened k does, so an
        answer-time fold over the assembled set is required.
      * nonmonotonic_regressions -- a larger k drops below threshold after a
        smaller k passed (a distractor flipped the verdict); flagged, never
        silently absorbed into the sets above.
    """

    def passes(fraction: float | None) -> bool:
        return fraction is not None and fraction >= threshold

    set_completeness: list[str] = []
    baseline_rejudge: list[str] = []
    combined: list[str] = []
    regressions: list[str] = []
    ceiling_bound: list[str] = []
    upstream_gap: list[str] = []
    no_oracle_signal: list[str] = []

    for result in results:
        qid = result["question_id"]
        if result["oracle"].get("pass_fraction") is None:
            # Every oracle repeat errored: no graded signal. Excluded from the
            # combined ceiling AND the derivation ceiling so a provider outage is
            # never reported as "complete facts still failed".
            no_oracle_signal.append(qid)
        if not result.get("oracle_complete", True):
            # Constituents missing from Tier-3 entirely: bounded by
            # extraction/promotion, not retrieval or derivation.
            upstream_gap.append(qid)
        sweep = _sweep_pass_fractions(result)
        # Widening = strictly past the run's returned top-k (RETURN_K); a pass at
        # k=RETURN_K is the baseline re-judge, tracked separately.
        if any(passes(frac) for k, frac in sweep if k > RETURN_K):
            set_completeness.append(qid)
        if any(passes(frac) for k, frac in sweep if k == RETURN_K):
            baseline_rejudge.append(qid)
        if passes(result["oracle"].get("pass_fraction")):
            combined.append(qid)
        if result.get("pool_ceiling"):
            ceiling_bound.append(qid)
        # Non-monotonic: a pass at some k followed by a miss at a strictly larger
        # k. sweep is sorted ascending by k above.
        seen_pass = False
        for _k, frac in sweep:
            if passes(frac):
                seen_pass = True
            elif seen_pass:
                regressions.append(qid)
                break

    reachable_set = set(set_completeness)
    n = len(results)

    def rate(subset: list[str]) -> float | None:
        return len(subset) / n if n else None

    # Split "oracle passes but no widened k does" by whether retrieval could even
    # surface the full set in the tested k range: if the widest k captures every
    # constituent yet the judge still misses, an answer-time fold over the
    # assembled set is the gap (derivation); if the widest k never captures the
    # set, the fact ranks beyond tested k -- a retrieval-depth limit, not
    # derivation. Uses the deterministic capture, not another judge call.
    widest_k = str(max(SWEEP_K_VALUES))
    by_id = {r["question_id"]: r for r in results}

    def full_capture(result: dict[str, Any]) -> bool:
        return result.get("capture", {}).get(widest_k, {}).get("fraction") == 1.0

    def has_graded_widened_sweep(result: dict[str, Any]) -> bool:
        # At least one widened (k>RETURN_K) condition produced a real verdict.
        return any(
            frac is not None
            for k, frac in _sweep_pass_fractions(result)
            if k > RETURN_K
        )

    combined_set = set(combined)
    # A question the oracle passed but no widened k graded (every widened repeat
    # errored) has no evidence either way: assigning derivation vs retrieval-depth
    # from capture alone would turn unavailable judge data into a substantive
    # result, so it is held out in no_widened_signal instead.
    unreached = [qid for qid in combined if qid not in reachable_set]
    no_widened_signal = [
        qid for qid in unreached if not has_graded_widened_sweep(by_id[qid])
    ]
    graded_unreached = [
        qid for qid in unreached if has_graded_widened_sweep(by_id[qid])
    ]
    derivation_needed = [
        qid for qid in graded_unreached if full_capture(by_id[qid])
    ]
    retrieval_depth_limited = [
        qid for qid in graded_unreached if not full_capture(by_id[qid])
    ]

    # Complete evidence, a graded oracle signal, but neither widening nor the
    # judge-with-oracle passes: a genuine answer-time derivation ceiling (distinct
    # from upstream gaps and from questions with no graded oracle verdict).
    reached = reachable_set | combined_set
    no_signal_set = set(no_oracle_signal)
    derivation_ceiling = [
        r["question_id"]
        for r in results
        if r.get("oracle_complete", True)
        and r["question_id"] not in reached
        and r["question_id"] not in no_signal_set
    ]

    return {
        "n": n,
        "threshold": threshold,
        "set_completeness_reachable": set_completeness,
        "baseline_rejudge_pass": baseline_rejudge,
        "combined_ceiling": combined,
        "derivation_needed": derivation_needed,
        "retrieval_depth_limited": retrieval_depth_limited,
        "no_widened_signal": no_widened_signal,
        "derivation_ceiling_complete_evidence": derivation_ceiling,
        "upstream_extraction_gap": upstream_gap,
        "no_oracle_signal": no_oracle_signal,
        "nonmonotonic_regressions": regressions,
        "retrieve_k_ceiling_bound": ceiling_bound,
        "rates": {
            "set_completeness_reachable": rate(set_completeness),
            "combined_ceiling": rate(combined),
            "derivation_needed": rate(derivation_needed),
            "retrieval_depth_limited": rate(retrieval_depth_limited),
            "upstream_extraction_gap": rate(upstream_gap),
        },
    }


def recorded_verdict(entry: OracleEntry) -> str | None:
    """The judge verdict this question's run RECORDED in diagnostics.jsonl, or
    None when that outcome is unknown. N is defined by this recorded value, never
    a fresh re-judge.

    Returns None -- "unknown", not a miss -- unless the row completed a clean
    judged run: a row whose status is not "ok", that carries a judge_error, or
    whose judge_verdict is absent did not produce a trustworthy verdict, so a
    stale judge_verdict on a failed run must not be read as the recorded outcome
    (else a failed run could masquerade as a curation-error pass and drop a valid
    miss from the denominator).
    """
    diagnostics_path = Path(entry.run_dir) / "diagnostics.jsonl"
    if not diagnostics_path.exists():
        return None
    for line in diagnostics_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("question_id") == entry.question_id:
            if row.get("status") not in (None, "ok") or row.get("judge_error"):
                return None
            return row.get("judge_verdict")
    return None


async def run_experiment(
    entries: list[OracleEntry],
    judge_fn: JudgeFn,
    *,
    repeats: int,
    budget: ProviderBudget,
) -> dict[str, Any]:
    """Score every curated miss and assemble the metrics document.

    Pre-flights all entries first (no budget spent), so a fixture defect fails
    loud before any provider call. Only a question the run RECORDED as a miss
    (verdict "partial" or "not_supported") is scored and counted in the headroom
    denominator N. A recorded "supported" is a curation error (curation_warnings)
    and a None -- diagnostics missing/failed/absent -- is an unknown outcome
    (unknown_recorded); neither is a confirmed miss, so both are excluded from N
    rather than silently scored as one. Budget is shared across questions;
    exhaustion stops the run and is recorded, never raised past the last
    completed question.
    """
    preflight(entries)
    recorded = {entry.question_id: recorded_verdict(entry) for entry in entries}
    curation_warnings = [
        qid for qid, verdict in recorded.items() if verdict == "supported"
    ]
    unknown_recorded = [
        qid for qid, verdict in recorded.items() if verdict is None
    ]
    miss_verdicts = {"partial", "not_supported"}
    scored = [
        e for e in entries if recorded.get(e.question_id) in miss_verdicts
    ]

    results: list[dict[str, Any]] = []
    budget_exhausted = False
    for entry in scored:
        try:
            result = await run_question(
                entry, judge_fn, repeats=repeats, budget=budget
            )
        except ProviderBudgetExhausted:
            budget_exhausted = True
            break
        result["recorded_verdict"] = recorded.get(entry.question_id)
        results.append(result)
    return {
        "caps": {"repeats": repeats, "max_provider_calls": budget.max_calls},
        "provider_calls_used": budget.used,
        "budget_exhausted": budget_exhausted,
        "sweep_k_values": list(SWEEP_K_VALUES),
        "curation_warnings": curation_warnings,
        "unknown_recorded": unknown_recorded,
        "results": results,
        "headroom": build_headroom(results),
    }


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f}"


def render_table(results: list[dict[str, Any]]) -> str:
    """Per-question markdown table: oracle vs baseline(k=5) vs best-k judged pass
    fraction, plus constituent capture at the widest k. Satisfies the
    acceptance criterion's per-question comparison table."""
    lines = [
        "| question | recorded | oracle | baseline k5 | best-k | best-k@ | capture@15 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    widest = str(max(SWEEP_K_VALUES))
    for result in results:
        sweep = result["sweep"]
        # Only graded (non-None) conditions are eligible for best-k: an all-error
        # sweep condition (pass_fraction None) must not masquerade as a real 0.00.
        best_k, best_frac = None, None
        for k, cond in sweep.items():
            frac = cond.get("pass_fraction")
            if frac is None:
                continue
            if best_frac is None or frac > best_frac:
                best_frac, best_k = frac, k
        lines.append(
            "| {qid} | {rec} | {oracle} | {base} | {best} | {bk} | {cap} |".format(
                qid=result["question_id"],
                rec=result.get("recorded_verdict") or "--",
                oracle=_fmt(result["oracle"].get("pass_fraction")),
                base=_fmt(result["baseline"].get("pass_fraction")),
                best=_fmt(best_frac),
                bk=best_k or "--",
                cap=_fmt(result["capture"].get(widest, {}).get("fraction")),
            )
        )
    return "\n".join(lines)


def _write_artifacts(out_dir: Path, doc: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "oracle_evidence_metrics.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n"
    )
    (out_dir / "oracle_evidence_table.md").write_text(
        render_table(doc["results"]) + "\n"
    )


def _bind_only_report(entries: list[OracleEntry]) -> str:
    """Deterministic capture/ceiling table, no provider calls: proves the
    fixture resolves and shows how much of each oracle set the fused[:k] sweep
    surfaces."""
    lines = ["| question | constituents | " + " | ".join(f"cap@{k}" for k in SWEEP_K_VALUES) + " | ceiling |"]
    lines.append("| --- | --- | " + " | ".join("---" for _ in SWEEP_K_VALUES) + " | --- |")
    for entry in entries:
        resolve_constituents(entry)  # drift guard
        keyword_ids, vector_ids = load_retrieval_arrays(entry)
        caps = []
        for k in SWEEP_K_VALUES:
            fused_k = reconstruct_fused(keyword_ids, vector_ids, k)
            caps.append(_fmt(constituent_capture(entry.constituent_fact_ids, fused_k)["fraction"]))
        ceiling = pool_ceiling(entry.constituent_fact_ids, keyword_ids, vector_ids)
        lines.append(
            f"| {entry.question_id} | {len(entry.constituent_fact_ids)} | "
            + " | ".join(caps)
            + f" | {len(ceiling)} |"
        )
    return "\n".join(lines)


def _load_judge_agent_factory(adapter_path: str) -> Callable[..., Any]:
    """Load build_longmemeval_recall_judge_agent from the adapter module path
    (same contract as vexic.longmemeval's judged-recall adapter)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_oracle_judge_adapter", adapter_path)
    if spec is None or spec.loader is None:
        raise OracleFixtureError(f"cannot load adapter: {adapter_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, "build_longmemeval_recall_judge_agent", None)
    if factory is None:
        raise OracleFixtureError(
            f"adapter {adapter_path} has no build_longmemeval_recall_judge_agent"
        )
    return factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the opt-in oracle-evidence experiment."
    )
    parser.add_argument("--oracle-fixture")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--bind-only", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-provider-calls", type=int, default=250)
    parser.add_argument("--out")
    parser.add_argument(
        "--adapter", default="adapters/openrouter_live_adapter.py"
    )
    parser.add_argument("--judge-model-group", default="claude")
    return parser


def _require(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise OracleFixtureError(f"{name} is required with --allow-live.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.bind_only:
        try:
            fixture = _require(args.oracle_fixture, "--oracle-fixture")
            entries = load_oracle_fixture(Path(fixture))
            preflight(entries)  # drift guard + fused cross-check, no provider
            print(_bind_only_report(entries))
        except OracleFixtureError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if not args.allow_live:
        print(
            "Oracle-evidence experiment skipped; pass --allow-live to run "
            "provider calls (or --bind-only for the deterministic table)."
        )
        return 0

    try:
        if args.repeats <= 0 or args.max_provider_calls <= 0:
            raise OracleFixtureError(
                "--repeats and --max-provider-calls must be greater than 0."
            )
        fixture = _require(args.oracle_fixture, "--oracle-fixture")
        out_dir = Path(_require(args.out, "--out"))
        entries = load_oracle_fixture(Path(fixture))
        factory = _load_judge_agent_factory(args.adapter)
        agent = factory(args.judge_model_group)

        async def judge_fn(
            judge_input: LongMemEvalRecallJudgeInput,
        ) -> LongMemEvalRecallJudgeVerdict:
            from vexic.longmemeval import score_longmemeval_recall

            return await score_longmemeval_recall(
                judge_input,
                judge_model_group=args.judge_model_group,
                agent=agent,
            )

        budget = ProviderBudget(args.max_provider_calls)
        doc = _run_experiment_sync(entries, judge_fn, args.repeats, budget)
        _write_artifacts(out_dir, doc)
        print(render_table(doc["results"]))
    except OracleFixtureError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - live provider boundary
        print(
            f"Oracle-evidence experiment failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


def _run_experiment_sync(
    entries: list[OracleEntry],
    judge_fn: JudgeFn,
    repeats: int,
    budget: ProviderBudget,
) -> dict[str, Any]:
    return asyncio.run(
        run_experiment(entries, judge_fn, repeats=repeats, budget=budget)
    )


if __name__ == "__main__":
    raise SystemExit(main())
