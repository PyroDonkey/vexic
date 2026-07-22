"""Offline promotion-eligibility simulation for the class-3 miss gaps.

Read-only over frozen LongMemEval run artifacts, with **no provider calls at
all**: the whole question this harness answers is deterministic.

The class-3 miss analysis dissected 12 misses over run DBs captured before the
``mentioned_at`` migration (ADR 0037), so the decision that undated ``event``
candidates promote on derived ``mentioned_at`` provenance is invisible in that
result. Those DBs still carry the full transcript, so the healing is
reproducible offline: ``_backfill_mentioned_at``
(``src/vexic/storage/schema.py``) derives ``mentioned_at`` from
``messages.timestamp`` via ``source_message_ids`` at init time.

For each gap candidate named in the gap fixture this harness reports:

  * whether ``mentioned_at`` heals, and to which date,
  * Deep promotion eligibility before (pre-column state) vs after the heal,
  * the candidate's rank inside the eligible pool, and whether that rank lands
    within ``--deep-top-n``.

Eligibility and ranking are not re-derived here: the harness reuses
``_load_diagnostic_candidates``, ``_deep_eligible`` and
``_rank_diagnostic_candidates`` from ``vexic.longmemeval``, which
mirror ``load_promotion_candidates`` plus the ``select_promotions``
undated-event skip.

LIMITATIONS -- this bounds the undated-event bucket, it does not confirm it:

  * It is a post-run snapshot of the FINAL candidate pool, not a replay of the
    per-cycle Deep pool. Eligible-and-ranked is not the same as promoted.
  * It says nothing about later Light extraction-prompt changes: the
    frozen candidate texts are what the old prompt produced.
  * Deep's contradiction check is model-backed and is not simulated.

The frozen run artifacts are never opened: each question DB is copied to a
temporary directory first, and the harness reads and heals only the copy, so
no read-only handle can leave a WAL sidecar next to the source.

Example:

    uv run python scripts/simulate_mentioned_at_promotion.py \\
        --gaps .eval-runs/class3-gaps/gaps.json --out .eval-runs/class3-gaps
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, Field, ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra_path in (str(_REPO_ROOT / "src"), str(_REPO_ROOT)):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

from vexic.longmemeval import (  # noqa: E402
    _DiagnosticCandidate,
    _deep_eligible,
    _load_diagnostic_candidates,
    _rank_diagnostic_candidates,
)
from vexic.longmemeval_analysis import _question_path_component  # noqa: E402
from vexic.storage import init_db  # noqa: E402

DEFAULT_DEEP_TOP_N = 15

# Prefix for the disposable heal-the-copy workspace. Tracker-id free by design.
_WORKSPACE_PREFIX = "class3-sim-"


class GapFixtureError(ValueError):
    """A malformed gap fixture, or a gap candidate that no longer resolves."""


class Gap(BaseModel):
    """One missing constituent behind a class-3 miss."""

    model_config = {"extra": "forbid"}

    gap_id: str
    kind: str
    description: str = ""
    # Only tier2-* gaps name a candidate; transcript-only and tabular gaps have
    # nothing in Tier 2 to simulate, and tier3-undated gaps name a fact.
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
    run_dir = Path(entry.run_dir)
    db_path = run_dir / _question_path_component(entry.question_id) / "memory.db"
    if not db_path.exists():
        raise GapFixtureError(
            f"question {entry.question_id}: no run DB at {db_path}"
        )
    return db_path


def _copy_question_db(db_path: Path, destination: Path) -> Path:
    """Copy the frozen DB (plus any WAL sidecars) so init heals only the copy."""
    destination.mkdir(parents=True, exist_ok=True)
    copy_path = destination / db_path.name
    for suffix in ("", "-wal", "-shm"):
        source = db_path.with_name(db_path.name + suffix)
        if source.exists():
            shutil.copy2(source, copy_path.with_name(copy_path.name + suffix))
    return copy_path


def frozen_candidate_rows(db_path: Path) -> dict[int, dict[str, Any]]:
    """Read the pre-heal candidate state with plain read-only sqlite3.

    Deliberately not ``storage.connect``: this reads the artifact as it was
    captured, before any schema ensure adds or backfills ``mentioned_at``, and
    it must not touch the vec0 virtual tables.
    """
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
        has_mentioned_at = "mentioned_at" in columns
        mentioned_at_select = "mentioned_at" if has_mentioned_at else "NULL"
        rows = conn.execute(
            f"""
            SELECT id, category, occurred_at, {mentioned_at_select},
                   promoted, retired, stale, needs_review
            FROM memory_candidates
            """
        ).fetchall()
    return {
        int(row[0]): {
            "category": str(row[1]),
            "occurred_at": None if row[2] is None else str(row[2]),
            "mentioned_at": None if row[3] is None else str(row[3]),
            "promoted": bool(row[4]),
            "retired": bool(row[5]),
            "stale": bool(row[6]),
            "needs_review": bool(row[7]),
            "pre_mentioned_at_column": not has_mentioned_at,
        }
        for row in rows
    }


def _with_mentioned_at(
    candidate: _DiagnosticCandidate, value: str | None
) -> _DiagnosticCandidate:
    """The same candidate with its ``mentioned_at`` replaced by ``value``."""
    return _DiagnosticCandidate(
        candidate_id=candidate.candidate_id,
        fact_text=candidate.fact_text,
        importance=candidate.importance,
        hit_count=candidate.hit_count,
        last_seen_at=candidate.last_seen_at,
        rem_boost=candidate.rem_boost,
        promoted=candidate.promoted,
        promoted_fact_id=candidate.promoted_fact_id,
        category=candidate.category,
        occurred_at=candidate.occurred_at,
        mentioned_at=value,
        has_embedding=candidate.has_embedding,
    )


def _before_pool(
    healed: list[_DiagnosticCandidate],
    frozen_rows: dict[int, dict[str, Any]],
) -> list[_DiagnosticCandidate]:
    """The candidate pool as it stood in the frozen artifact.

    Each healed candidate's ``mentioned_at`` is rolled back to the value the
    frozen DB carried (None only when the column was absent or NULL), so the
    before-heal eligibility is judged against the real pre-heal state rather
    than a blanket blank that would spuriously flip already-dated candidates.
    """
    return [
        _with_mentioned_at(
            candidate,
            (frozen_rows.get(candidate.candidate_id) or {}).get("mentioned_at"),
        )
        for candidate in healed
    ]


def _simulate_fact_date(db_path: Path, fact_id: int) -> dict[str, Any]:
    """Report whether a Tier-3 fact carries a date after the init-time heal.

    ``_backfill_mentioned_at`` runs for ``long_term_memory`` as well, so an
    undated fact that blocked a temporal derivation may now be dated.
    """
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        row = conn.execute(
            "SELECT occurred_at, mentioned_at FROM long_term_memory WHERE id = ?",
            (fact_id,),
        ).fetchone()
    if row is None:
        raise GapFixtureError(f"fact {fact_id} is not in {db_path}")
    occurred_at = None if row[0] is None else str(row[0])
    mentioned_at = None if row[1] is None else str(row[1])
    dated = bool((occurred_at or "").strip() or (mentioned_at or "").strip())
    return {
        "simulated": True,
        "frozen_fact_id": fact_id,
        "occurred_at": occurred_at,
        "healed_mentioned_at": mentioned_at,
        "mentioned_at_healed": mentioned_at is not None,
        "verdict": "fact-now-dated" if dated else "fact-still-undated",
    }


def _scoring_time(candidates: list[_DiagnosticCandidate]) -> datetime:
    """Deterministic scoring clock: the pool's newest ``last_seen_at``.

    The frozen runs did not persist ``candidate_scoring_time``, and wall-clock
    now would make the recency term drift with the calendar. Ranking is
    relative, so a fixed in-pool anchor keeps the table reproducible.
    """
    if not candidates:
        return datetime.now(timezone.utc)
    return max(candidate.last_seen_at for candidate in candidates)


def simulate_question(
    entry: QuestionGaps,
    *,
    workspace: Path,
    deep_top_n: int = DEFAULT_DEEP_TOP_N,
) -> dict[str, Any]:
    db_path = question_db_path(entry)
    # Copy first, then read and heal only the copy: opening the source at all --
    # even read-only -- can create a -shm/-wal sidecar next to a WAL-mode
    # artifact, so the source is never opened.
    copy_path = _copy_question_db(db_path, workspace / entry.question_id)
    frozen_rows = frozen_candidate_rows(copy_path)
    # init_db runs the schema ensure, which adds mentioned_at and backfills it
    # from the transcript (ADR 0037). This is the whole simulation.
    init_db(str(copy_path))

    healed = _load_diagnostic_candidates(copy_path)
    scoring_time = _scoring_time(healed)
    healed_by_id = {candidate.candidate_id: candidate for candidate in healed}
    after_ranks = _rank_diagnostic_candidates(
        _deep_eligible(healed), scoring_time=scoring_time
    )
    before_pool = _deep_eligible(_before_pool(healed, frozen_rows))
    before_ranks = _rank_diagnostic_candidates(before_pool, scoring_time=scoring_time)

    gap_results: list[dict[str, Any]] = []
    for gap in entry.gaps:
        result: dict[str, Any] = {
            "gap_id": gap.gap_id,
            "kind": gap.kind,
            "frozen_candidate_id": gap.frozen_candidate_id,
            "simulated": gap.frozen_candidate_id is not None,
        }
        if gap.frozen_candidate_id is None:
            if gap.frozen_fact_id is not None:
                # A Tier-3 fact that carried no date. long_term_memory heals on
                # the same init pass, so report whether the fact is dated now.
                result.update(_simulate_fact_date(copy_path, gap.frozen_fact_id))
                gap_results.append(result)
                continue
            result["verdict"] = "not-simulatable"
            result["note"] = (
                "no Tier-2 candidate to heal; bounded by extraction, not promotion"
            )
            gap_results.append(result)
            continue
        candidate_id = gap.frozen_candidate_id
        frozen = frozen_rows.get(candidate_id)
        candidate = healed_by_id.get(candidate_id)
        if frozen is None or candidate is None:
            raise GapFixtureError(
                f"question {entry.question_id}: gap {gap.gap_id} names candidate "
                f"{candidate_id}, which is not in {db_path}"
            )
        after_rank = after_ranks.get(candidate_id)
        result.update(
            {
                "category": candidate.category,
                "occurred_at": candidate.occurred_at,
                "frozen_mentioned_at": frozen["mentioned_at"],
                "healed_mentioned_at": candidate.mentioned_at,
                "mentioned_at_healed": (
                    frozen["mentioned_at"] is None and candidate.mentioned_at is not None
                ),
                "eligible_before": before_ranks.get(candidate_id) is not None,
                "eligible_after": after_rank is not None,
                "rank_after": after_rank,
                "eligible_pool_size": len(after_ranks),
                "within_deep_top_n": after_rank is not None and after_rank <= deep_top_n,
                # A pool no larger than the top-n slice ranks every survivor
                # trivially, so a flip inside it is not evidence about ranking.
                "top_n_covers_pool": len(after_ranks) <= deep_top_n,
            }
        )
        if result["eligible_after"] and not result["eligible_before"]:
            if not result["within_deep_top_n"]:
                result["verdict"] = "flips-eligible-outside-top-n"
            elif result["top_n_covers_pool"]:
                result["verdict"] = "flips-eligible-degenerate-pool"
            else:
                result["verdict"] = "flips-eligible-and-ranked"
        elif result["eligible_after"]:
            result["verdict"] = "already-eligible"
        else:
            result["verdict"] = "still-ineligible"
        gap_results.append(result)

    return {
        "question_id": entry.question_id,
        "bucket": entry.bucket,
        "run_dir": entry.run_dir,
        "pre_mentioned_at_column": any(
            row["pre_mentioned_at_column"] for row in frozen_rows.values()
        ),
        "candidate_count": len(frozen_rows),
        "eligible_before": len(before_ranks),
        "eligible_after": len(after_ranks),
        "scoring_time": scoring_time.isoformat(),
        "gaps": gap_results,
    }


def render_table(results: list[dict[str, Any]], *, deep_top_n: int) -> str:
    lines = [
        f"# Promotion-eligibility simulation (deep_top_n={deep_top_n})",
        "",
        "| question | bucket | gap | candidate | mentioned_at | eligible before -> after | rank | verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        if not result["gaps"]:
            lines.append(
                f"| {result['question_id']} | {result['bucket']} | (none) | - | - | - | - | oracle-complete |"
            )
            continue
        for gap in result["gaps"]:
            if not gap["simulated"]:
                lines.append(
                    f"| {result['question_id']} | {result['bucket']} | {gap['gap_id']} | - | - | - | - | {gap['verdict']} |"
                )
                continue
            rank = gap.get("rank_after")
            rank_text = (
                "-" if rank is None else f"{rank}/{gap['eligible_pool_size']}"
            )
            subject = (
                f"cand {gap['frozen_candidate_id']}"
                if gap["frozen_candidate_id"] is not None
                else f"fact {gap['frozen_fact_id']}"
            )
            eligibility = (
                f"{gap['eligible_before']} -> {gap['eligible_after']}"
                if "eligible_after" in gap
                else "n/a"
            )
            lines.append(
                f"| {result['question_id']} | {result['bucket']} | {gap['gap_id']} "
                f"| {subject} | {gap['healed_mentioned_at'] or '-'} "
                f"| {eligibility} | {rank_text} | {gap['verdict']} |"
            )
    return "\n".join(lines) + "\n"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    gaps = [gap for result in results for gap in result["gaps"]]
    simulated = [gap for gap in gaps if gap["simulated"]]
    return {
        "questions": len(results),
        "gaps_total": len(gaps),
        "gaps_simulatable": len(simulated),
        "gaps_not_simulatable": len(gaps) - len(simulated),
        "flips_eligible_and_ranked": sum(
            1 for gap in simulated if gap["verdict"] == "flips-eligible-and-ranked"
        ),
        "flips_eligible_degenerate_pool": sum(
            1 for gap in simulated if gap["verdict"] == "flips-eligible-degenerate-pool"
        ),
        "flips_eligible_outside_top_n": sum(
            1 for gap in simulated if gap["verdict"] == "flips-eligible-outside-top-n"
        ),
        "already_eligible": sum(
            1 for gap in simulated if gap["verdict"] == "already-eligible"
        ),
        "still_ineligible": sum(
            1 for gap in simulated if gap["verdict"] == "still-ineligible"
        ),
        "fact_now_dated": sum(
            1 for gap in simulated if gap["verdict"] == "fact-now-dated"
        ),
        "fact_still_undated": sum(
            1 for gap in simulated if gap["verdict"] == "fact-still-undated"
        ),
    }


def _write_artifacts(out_dir: Path, doc: dict[str, Any], table: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "promotion_simulation_metrics.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "promotion_simulation_table.md").write_text(table, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Simulate ADR 0037 mentioned_at healing over frozen LongMemEval run "
            "DBs and report Deep promotion eligibility for the class-3 miss gaps."
        )
    )
    parser.add_argument("--gaps", required=True, type=Path, help="Gap fixture JSON.")
    parser.add_argument(
        "--out", type=Path, default=None, help="Directory for the JSON/markdown artifacts."
    )
    parser.add_argument("--deep-top-n", type=int, default=DEFAULT_DEEP_TOP_N)
    parser.add_argument(
        "--question-id",
        action="append",
        default=[],
        help="Restrict the run to these question ids. Repeatable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
                    simulate_question(
                        entry, workspace=workspace, deep_top_n=args.deep_top_n
                    )
                )
        except GapFixtureError as exc:
            print(f"gap fixture error: {exc}", file=sys.stderr)
            return 2

    table = render_table(results, deep_top_n=args.deep_top_n)
    doc = {
        "deep_top_n": args.deep_top_n,
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
