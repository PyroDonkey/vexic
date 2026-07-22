"""Offline miss-classification analysis for a LongMemEval run.

Reads a completed run directory (``diagnostics.jsonl`` plus per-question
``memory.db`` files) and the source dataset, then buckets every judged-recall
miss into exactly one failing-stage class:

* **class 1** -- no live Tier 3 fact contains the gold answer: an
  extraction or promotion miss (``sub_reason`` distinguishes the two from the
  run's own stage diagnostics).
* **class 2** -- a gold fact exists but ranked out of the returned top-k:
  ``below_return_k`` when the recomputed full RRF rank exceeds ``RETURN_K``,
  ``outside_retrieve_k`` when the fact never entered either top-``RETRIEVE_K``
  retriever array.
* **class 3** -- candidate multi-fact/derivation cases needing human review:
  the gold fact was returned yet judged unsupported, or the answer appears
  verbatim nowhere in the transcript so no single extracted fact could ever
  contain it (aggregation/temporal answers).

Rows whose answers are unmatchable by token containment (yes/no, very short)
are reported unclassified with ``needs_manual_review``.

This module only *reads* run artifacts: every ``memory.db`` is opened with
SQLite's read-only URI mode, and the sole output is ``analysis_report.json``
in the run directory plus a stdout summary. It deliberately imports the
retrieval constants and ``reciprocal_rank_fusion`` from
``vexic.subagents.retrieval`` so the offline rank recompute can never drift
from live fusion semantics; the diagnostic changes no retrieval or promotion
behavior.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ValidationError

from vexic.longmemeval import (
    PREFERENCE_QUESTION_TYPES,
    LongMemEvalRecallJudgeVerdictValue,
    _contains_answer_tokens,
    _load_dataset,
    _matchable_answer_tokens,
    _question_path_component,
)
from vexic.storage.connection import StorageConnection, connect
from vexic.subagents.retrieval import RETURN_K, reciprocal_rank_fusion

MissSubReason = Literal[
    "extraction_miss",
    "promotion_miss",
    "below_return_k",
    "outside_retrieve_k",
    "retrieved_but_judged_miss",
    "answer_not_verbatim_requires_join",
    "unmatchable_answer",
    "missing_question_db",
    "missing_dataset_row",
    "analysis_error",
]


class MissClassification(BaseModel):
    question_id: str
    question_type: str | None
    miss_class: Literal[1, 2, 3] | None
    sub_reason: MissSubReason
    needs_manual_review: bool
    gold_fact_ids: list[int]
    gold_fused_rank: int | None
    evidence: dict[str, Any]


class SubjectHistogram(BaseModel):
    question_id: str
    total_facts: int
    distinct_subjects: int
    median_facts_per_subject: float
    max_facts_per_subject: int
    top_subjects: list[tuple[str, int]]


class AggregateHistogram(BaseModel):
    total_facts: int
    distinct_subjects: int
    median_facts_per_subject: float
    max_facts_per_subject: int


class PreferenceRescoreRow(BaseModel):
    question_id: str
    question_type: str
    original_verdict: str | None
    rubric_verdict: LongMemEvalRecallJudgeVerdictValue
    rubric_reason: str
    rubric_confidence: float
    judge_model_id: str | None
    judge_prompt_version: str
    reconstruction_complete: bool


class PreferenceRow(BaseModel):
    question_id: str
    question_type: str
    original_verdict: str | None
    evidence: dict[str, Any]


class PreferenceReportSection(BaseModel):
    rows: list[PreferenceRow]
    rescore_available: bool
    verdict_delta: dict[str, int] | None


class RunAnalysisReport(BaseModel):
    run_dir: str
    judged_recall_by_question_type: dict[str, dict[str, int]]
    misses: list[MissClassification]
    class_counts: dict[str, int]
    subject_histograms: list[SubjectHistogram]
    aggregate_histogram: AggregateHistogram
    skipped_diagnostics_lines: int = 0
    preference: PreferenceReportSection | None = None


def _open_readonly(db_path: Path) -> StorageConnection:
    # SQLite read-only URI mode: the analysis must never mutate run artifacts.
    return connect(f"file:{db_path}?mode=ro", uri=True)


def _memory_db_path(run_dir: Path, question_id: str) -> Path:
    return run_dir / _question_path_component(question_id) / "memory.db"


def _load_diagnostics(run_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse diagnostics rows, skipping (and counting) malformed lines.

    A run that crashed mid-write commonly leaves one truncated trailing line;
    one bad record must not cost the report for every well-formed row.
    """
    diagnostics_path = run_dir / "diagnostics.jsonl"
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in diagnostics_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def _live_facts(conn: StorageConnection) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, fact_text FROM long_term_memory WHERE retired = 0"
    ).fetchall()
    return [(int(fact_id), fact_text) for fact_id, fact_text in rows]


def _parsed_id_list(value: Any) -> list[int]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in parsed
    ):
        raise ValueError(
            f"retrieval event column is not a JSON list of fact ids: {value!r}"
        )
    return parsed


def _answer_retrieval_arrays(
    conn: StorageConnection,
    question_id: str,
) -> tuple[list[int], list[int], list[int]]:
    row = conn.execute(
        """
        SELECT keyword_fact_ids, vector_fact_ids, fused_fact_ids
        FROM retrieval_events
        WHERE session_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"longmemeval:{question_id}:answer",),
    ).fetchone()
    if row is None:
        return [], [], []
    keyword_ids, vector_ids, fused_ids = (_parsed_id_list(value) for value in row)
    return keyword_ids, vector_ids, fused_ids


def _fact_texts(
    conn: StorageConnection,
    fact_ids: Sequence[int],
) -> list[str]:
    texts: list[str] = []
    for fact_id in fact_ids:
        row = conn.execute(
            "SELECT fact_text FROM long_term_memory WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if row is not None:
            texts.append(row[0])
    return texts


def _classify_miss(
    diagnostics_row: Mapping[str, Any],
    dataset_row: Mapping[str, Any] | None,
    db_path: Path,
) -> MissClassification:
    question_id = diagnostics_row["question_id"]
    question_type = diagnostics_row.get("question_type")
    answer = None if dataset_row is None else dataset_row.get("answer")
    evidence: dict[str, Any] = {
        "question": None if dataset_row is None else dataset_row.get("question"),
        "answer": answer,
    }

    def result(
        miss_class: Literal[1, 2, 3] | None,
        sub_reason: MissSubReason,
        *,
        needs_manual_review: bool,
        gold_fact_ids: list[int] = [],
        gold_fused_rank: int | None = None,
    ) -> MissClassification:
        return MissClassification(
            question_id=question_id,
            question_type=question_type,
            miss_class=miss_class,
            sub_reason=sub_reason,
            needs_manual_review=needs_manual_review,
            gold_fact_ids=gold_fact_ids,
            gold_fused_rank=gold_fused_rank,
            evidence=evidence,
        )

    if dataset_row is None:
        # A diagnostics question_id absent from the supplied --dataset is a
        # dataset/run mismatch, not an answer-shape limitation; labeling it
        # unmatchable would send the investigation down the wrong path.
        return result(None, "missing_dataset_row", needs_manual_review=True)

    if not db_path.exists():
        return result(None, "missing_question_db", needs_manual_review=True)

    answer_tokens = _matchable_answer_tokens(answer)
    if not answer_tokens:
        return result(None, "unmatchable_answer", needs_manual_review=True)

    with closing(_open_readonly(db_path)) as conn:
        facts = _live_facts(conn)
        gold_fact_ids = [
            fact_id
            for fact_id, fact_text in facts
            if _contains_answer_tokens(fact_text, answer_tokens)
        ]
        keyword_ids, vector_ids, fused_returned = _answer_retrieval_arrays(
            conn,
            question_id,
        )
        evidence["retrieved_fact_texts"] = _fact_texts(conn, fused_returned)
        evidence["gold_fact_texts"] = _fact_texts(conn, gold_fact_ids)

    if not gold_fact_ids:
        if diagnostics_row.get("answer_found_in_tier1") is False:
            # The answer appears verbatim nowhere in the ingested transcript,
            # so no extracted fact could ever contain it: deriving it needs a
            # join over multiple facts. Human confirmation required.
            return result(
                3,
                "answer_not_verbatim_requires_join",
                needs_manual_review=True,
            )
        sub_reason: MissSubReason = (
            "promotion_miss"
            if diagnostics_row.get("answer_extracted_to_tier2")
            else "extraction_miss"
        )
        return result(1, sub_reason, needs_manual_review=False)

    # Recompute the untruncated fused ranking from the persisted per-retriever
    # arrays; the stored fused_fact_ids column is truncated to the returned
    # top-k and cannot show where a ranked-out fact landed.
    fused = reciprocal_rank_fusion([list(keyword_ids), list(vector_ids)])
    ranks = [
        fused.index(fact_id) + 1 for fact_id in gold_fact_ids if fact_id in fused
    ]
    gold_fused_rank = min(ranks) if ranks else None
    evidence["keyword_fact_ids"] = list(keyword_ids)
    evidence["vector_fact_ids"] = list(vector_ids)

    if gold_fused_rank is None:
        return result(
            2,
            "outside_retrieve_k",
            needs_manual_review=False,
            gold_fact_ids=gold_fact_ids,
        )
    if gold_fused_rank > RETURN_K:
        return result(
            2,
            "below_return_k",
            needs_manual_review=False,
            gold_fact_ids=gold_fact_ids,
            gold_fused_rank=gold_fused_rank,
        )
    return result(
        3,
        "retrieved_but_judged_miss",
        needs_manual_review=True,
        gold_fact_ids=gold_fact_ids,
        gold_fused_rank=gold_fused_rank,
    )


def _subject_counts(db_path: Path) -> list[tuple[str, int]] | None:
    if not db_path.exists():
        return None
    # Subject is stored verbatim, so one entity is case-split across
    # exact-string keys ("User"/"user"/"  User "). Fold case/whitespace variants
    # of the same token into one bucket, keyed by the SAME `lower(trim(subject))`
    # the dedup gate uses (`_nearest_candidate`), so parity is exact -- SQLite
    # ASCII-fold / space-strip, not Python's broader Unicode `.strip().lower()`.
    # distinct_subjects then counts entities, not spellings. The display label
    # is the most frequent raw variant (correlated subquery; ties broken
    # lexicographically for determinism).
    with closing(_open_readonly(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT
                (
                    SELECT v.subject
                    FROM long_term_memory AS v
                    WHERE v.retired = 0
                        AND lower(trim(v.subject)) = lower(trim(o.subject))
                    GROUP BY v.subject
                    ORDER BY COUNT(*) DESC, v.subject
                    LIMIT 1
                ) AS label,
                COUNT(*) AS n
            FROM long_term_memory AS o
            WHERE o.retired = 0
            GROUP BY lower(trim(o.subject))
            ORDER BY n DESC, label
            """
        ).fetchall()
    # An empty list is a real observation (the DB exists but holds zero live
    # facts -- itself a diagnostic signal); ``None`` means the DB is absent.
    return [(label, int(count)) for label, count in rows]


def _subject_histogram(
    question_id: str,
    counts: Sequence[tuple[str, int]],
    *,
    top_n: int = 10,
) -> SubjectHistogram:
    values = [count for _, count in counts]
    return SubjectHistogram(
        question_id=question_id,
        total_facts=sum(values),
        distinct_subjects=len(counts),
        median_facts_per_subject=statistics.median(values) if values else 0,
        max_facts_per_subject=max(values) if values else 0,
        top_subjects=list(counts[:top_n]),
    )


def _aggregate_histogram(all_counts: Sequence[int]) -> AggregateHistogram:
    # Pools every (db, subject) count as one observation. Subjects are not
    # deduplicated across question DBs: each DB is an isolated corpus, so the
    # same subject string in two DBs is two observations.
    if not all_counts:
        return AggregateHistogram(
            total_facts=0,
            distinct_subjects=0,
            median_facts_per_subject=0,
            max_facts_per_subject=0,
        )
    return AggregateHistogram(
        total_facts=sum(all_counts),
        distinct_subjects=len(all_counts),
        median_facts_per_subject=statistics.median(all_counts),
        max_facts_per_subject=max(all_counts),
    )


def _recall_by_question_type(
    diagnostics_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    recall: dict[str, dict[str, int]] = {}
    for row in diagnostics_rows:
        if row.get("status") != "ok" or row.get("judged_recall_pass") is None:
            continue
        question_type = row.get("question_type") or "<unknown>"
        bucket = recall.setdefault(question_type, {"supported": 0, "total": 0})
        bucket["total"] += 1
        if row.get("judged_recall_pass"):
            bucket["supported"] += 1
    return recall


def _analysis_error(
    question_id: str,
    question_type: Any,
    exc: Exception,
) -> MissClassification:
    return MissClassification(
        question_id=question_id,
        question_type=question_type if isinstance(question_type, str) else None,
        miss_class=None,
        sub_reason="analysis_error",
        needs_manual_review=True,
        gold_fact_ids=[],
        gold_fused_rank=None,
        evidence={"error": f"{type(exc).__name__}: {exc}"},
    )


def _load_preference_rescore(run_dir: Path) -> list[PreferenceRescoreRow] | None:
    """Parse a precomputed ``preference_rescore.jsonl``, or None when absent.

    Mirrors ``_load_diagnostics`` malformed-line tolerance: a truncated or
    non-conforming line is skipped (via a local counter, not the diagnostics
    ``skipped`` count, which belongs to a different artifact) rather than
    sinking the whole rescore file. Absence of the file is distinct from an
    empty file: absence returns ``None`` (rescore never ran), an empty or
    all-malformed file returns ``[]`` (ran, produced nothing usable).
    """
    rescore_path = run_dir / "preference_rescore.jsonl"
    if not rescore_path.exists():
        return None
    rows: list[PreferenceRescoreRow] = []
    for line in rescore_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(PreferenceRescoreRow.model_validate_json(line))
        except ValidationError:
            continue
    return rows


def _verdict_delta(rescore_rows: Sequence[PreferenceRescoreRow]) -> dict[str, int]:
    # Only fully reconstructed rows feed the literal-vs-rubric headline; rows
    # whose Tier-1 reconstruction was incomplete are bucketed separately and
    # never silently dropped, so the counts always sum to the row total.
    delta = {
        "flipped_to_supported": 0,
        "unchanged": 0,
        "flipped_from_supported": 0,
        "incomplete_reconstruction": 0,
    }
    for row in rescore_rows:
        if not row.reconstruction_complete:
            delta["incomplete_reconstruction"] += 1
            continue
        was_supported = row.original_verdict == "supported"
        now_supported = row.rubric_verdict == "supported"
        if now_supported and not was_supported:
            delta["flipped_to_supported"] += 1
        elif was_supported and not now_supported:
            delta["flipped_from_supported"] += 1
        else:
            delta["unchanged"] += 1
    return delta


def _build_preference_section(
    preference_rows: Sequence[PreferenceRow],
    rescore_rows: Sequence[PreferenceRescoreRow] | None,
) -> PreferenceReportSection | None:
    # Report the section when there is anything to say: at least one preference
    # miss row, or a rescore artifact present (even if empty). Otherwise None.
    if not preference_rows and rescore_rows is None:
        return None
    verdict_delta = None if rescore_rows is None else _verdict_delta(rescore_rows)
    return PreferenceReportSection(
        rows=list(preference_rows),
        rescore_available=rescore_rows is not None,
        verdict_delta=verdict_delta,
    )


def _build_preference_row(
    diagnostics_row: Mapping[str, Any],
    dataset_row: Mapping[str, Any] | None,
    db_path: Path,
) -> PreferenceRow:
    # Preference misses are held out of stage classification: the literal
    # answer-token containment that classes 1-3 rely on is the wrong lens for
    # a rubric-judged preference. We keep the question, the rubric answer, and
    # the returned facts as review evidence and defer the verdict to rescore.
    question_id = diagnostics_row["question_id"]
    question_type = diagnostics_row.get("question_type")
    evidence: dict[str, Any] = {
        "question": None if dataset_row is None else dataset_row.get("question"),
        "answer": None if dataset_row is None else dataset_row.get("answer"),
        "retrieved_fact_texts": [],
    }
    if db_path.exists():
        try:
            with closing(_open_readonly(db_path)) as conn:
                _, _, fused_returned = _answer_retrieval_arrays(conn, question_id)
                evidence["retrieved_fact_texts"] = _fact_texts(conn, fused_returned)
        except Exception:
            # A corrupt question DB must not drop the preference row; the row
            # is still surfaced with empty retrieval evidence for review.
            pass
    return PreferenceRow(
        question_id=question_id,
        question_type=str(question_type),
        original_verdict=diagnostics_row.get("judge_verdict"),
        evidence=evidence,
    )


def analyze_run(run_dir: Path, dataset_path: Path) -> RunAnalysisReport:
    """Classify every judged-recall miss in a run and build subject histograms."""

    diagnostics_rows, skipped_lines = _load_diagnostics(run_dir)
    # A resumed/retried run can emit several rows for one question_id; the
    # chronologically last row is the question's final state, and each
    # question DB must be counted exactly once.
    rows_by_question_id: dict[str, dict[str, Any]] = {}
    for row in diagnostics_rows:
        question_id = row.get("question_id")
        if isinstance(question_id, str):
            rows_by_question_id[question_id] = row
    # First dataset row wins on duplicate question_ids, deterministically.
    dataset_by_id: dict[Any, dict[str, Any]] = {}
    for row in _load_dataset(dataset_path):
        dataset_by_id.setdefault(row.get("question_id"), row)

    misses: list[MissClassification] = []
    preference_rows: list[PreferenceRow] = []
    histograms: list[SubjectHistogram] = []
    pooled_counts: list[int] = []
    for question_id, row in rows_by_question_id.items():
        db_path = _memory_db_path(run_dir, question_id)
        # One corrupt question DB must not cost the report for every other
        # question: classify what fails as analysis_error and keep going.
        try:
            counts = _subject_counts(db_path)
        except Exception:
            counts = None
        if counts is not None:
            histograms.append(_subject_histogram(question_id, counts))
            pooled_counts.extend(count for _, count in counts)
        if row.get("status") != "ok" or row.get("judged_recall_pass") is not False:
            continue
        # Preference misses use a rubric, not literal answer containment, so
        # they must never enter stage classification (classes 1-3). Route them
        # to the dedicated preference section instead.
        if row.get("question_type") in PREFERENCE_QUESTION_TYPES:
            preference_rows.append(
                _build_preference_row(row, dataset_by_id.get(question_id), db_path)
            )
            continue
        try:
            misses.append(
                _classify_miss(row, dataset_by_id.get(question_id), db_path)
            )
        except Exception as exc:
            misses.append(_analysis_error(question_id, row.get("question_type"), exc))

    class_counts: dict[str, int] = {}
    for miss in misses:
        key = "unclassified" if miss.miss_class is None else f"class_{miss.miss_class}"
        class_counts[key] = class_counts.get(key, 0) + 1

    rescore_rows = _load_preference_rescore(run_dir)
    preference_section = _build_preference_section(preference_rows, rescore_rows)

    return RunAnalysisReport(
        run_dir=str(run_dir),
        judged_recall_by_question_type=_recall_by_question_type(
            list(rows_by_question_id.values())
        ),
        misses=misses,
        class_counts=class_counts,
        subject_histograms=histograms,
        aggregate_histogram=_aggregate_histogram(pooled_counts),
        skipped_diagnostics_lines=skipped_lines,
        preference=preference_section,
    )


def _print_summary(report: RunAnalysisReport) -> None:
    print(f"Run: {report.run_dir}")
    for question_type, bucket in sorted(
        report.judged_recall_by_question_type.items()
    ):
        print(
            f"  {question_type}: {bucket['supported']}/{bucket['total']} supported"
        )
    print(
        "Miss classes: "
        f"class 1 (fact absent) = {report.class_counts.get('class_1', 0)}, "
        f"class 2 (ranked out) = {report.class_counts.get('class_2', 0)}, "
        f"class 3 (join/manual) = {report.class_counts.get('class_3', 0)}, "
        f"unclassified = {report.class_counts.get('unclassified', 0)}"
    )
    manual = [miss.question_id for miss in report.misses if miss.needs_manual_review]
    if manual:
        print(f"Needs manual review: {', '.join(manual)}")
    if report.preference is not None:
        preference = report.preference
        line = (
            f"Preference (held out of stage classes): "
            f"{len(preference.rows)} rows"
        )
        if preference.verdict_delta is not None:
            delta = preference.verdict_delta
            line += (
                ", rubric delta: "
                f"+{delta['flipped_to_supported']} to supported, "
                f"{delta['unchanged']} unchanged, "
                f"-{delta['flipped_from_supported']} from supported, "
                f"{delta['incomplete_reconstruction']} incomplete"
            )
        else:
            line += ", no rescore artifact"
        print(line)
    aggregate = report.aggregate_histogram
    print(
        "Subjects (pooled across question DBs): "
        f"{aggregate.total_facts} facts, "
        f"{aggregate.distinct_subjects} distinct subjects, "
        f"median {aggregate.median_facts_per_subject} facts/subject, "
        f"max {aggregate.max_facts_per_subject}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classify LongMemEval judged-recall misses by failing stage and "
            "build per-subject fact histograms. Read-only over run "
            "artifacts."
        )
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="Where to write the JSON report (default: <run-dir>/analysis_report.json).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report_path = args.report_path or args.run_dir / "analysis_report.json"
    if report_path.suffix != ".json":
        # A stray --report-path aimed at a run artifact (e.g. a question's
        # memory.db) would overwrite it; the report is the module's only write.
        parser.error(f"--report-path must end in .json, got: {report_path}")
    if not (args.run_dir / "diagnostics.jsonl").exists():
        print(
            f"error: no diagnostics.jsonl in {args.run_dir}; "
            "is --run-dir a completed LongMemEval run directory?",
            file=sys.stderr,
        )
        return 2
    report = analyze_run(args.run_dir, args.dataset)
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    _print_summary(report)
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
