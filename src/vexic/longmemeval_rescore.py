"""Opt-in live rubric-delta rescore over an existing LongMemEval run.

Reopens a completed run directory, re-judges every *preference* judged-recall
MISS row through the rubric-aware recall-judge render, and
writes ``<run-dir>/preference_rescore.jsonl``. This closes the loop that the
offline analysis section (``vexic.longmemeval_analysis``) only marks
``rescore_available``: analysis holds preference misses out of literal
stage classification, and this module supplies the live rubric verdict that
lets ``_verdict_delta`` report the literal-vs-rubric headline.

Read-only over run artifacts: every per-question ``memory.db`` is opened with
SQLite's read-only URI mode (via ``vexic.longmemeval_analysis._open_readonly``)
and the sole write is the rescore JSONL. It never re-runs retrieval or
promotion; it reconstructs exactly the fact set the eval-time judge saw from
the persisted ``retrieval_events`` fused-id array, then re-sorts event-category
facts with the same ``_with_events_sorted`` retrieval uses so the rubric judge
sees the identical positional fact ordering.

Reconstruction can be *incomplete* -- a candidate-note fallback answer stored
no Tier-3 fused ids, a missing question DB, or a reconstructed fact count that
disagrees with the diagnostics count. Such rows are still judged over whatever
was reconstructed (possibly an empty fact set, which the render labels
``None``) and still written, but flagged ``reconstruction_complete = False`` so
the downstream delta buckets them separately instead of trusting a verdict over
the wrong facts. Silently dropping them would understate the miss population.

Import direction is one-way: this leaf imports from ``vexic.longmemeval`` and
``vexic.longmemeval_analysis``; neither imports back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

from vexic.longmemeval import (
    LONGMEMEVAL_RECALL_JUDGE_PREFERENCE_PROMPT_VERSION,
    LONGMEMEVAL_RECALL_JUDGE_PROMPT,
    PREFERENCE_QUESTION_TYPES,
    LongMemEvalRecallJudge,
    LongMemEvalRecallJudgeInput,
    _append_jsonl,
    _judge_model_id_from_agent,
    _load_dataset,
    _load_eval_adapter,
    _render_recall_judge_input,
    score_longmemeval_recall,
)
from vexic.longmemeval_analysis import (
    PreferenceRescoreRow,
    _answer_retrieval_arrays,
    _load_diagnostics,
    _memory_db_path,
    _open_readonly,
)
from vexic.ports import AgentFactory
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import LongTermFact
from vexic.storage.connection import StorageConnection
from vexic.subagents.retrieval import _with_events_sorted

RESCORE_ARTIFACT_NAME = "preference_rescore.jsonl"


def _is_preference_miss(diagnostics_row: dict[str, Any]) -> bool:
    # Same predicate the analysis preference section uses: a preference-typed,
    # judged-recall row that completed OK yet the eval-time judge scored a miss.
    return (
        diagnostics_row.get("question_type") in PREFERENCE_QUESTION_TYPES
        and diagnostics_row.get("answer_mode") == "judged-recall"
        and diagnostics_row.get("status") == "ok"
        and diagnostics_row.get("judged_recall_pass") is False
    )


def _fetch_long_term_facts_readonly(
    conn: StorageConnection,
    fact_ids: Sequence[int],
) -> list[LongTermFact]:
    """Load Tier-3 facts by id in the given order, read-only.

    Selects the same columns as ``vexic.storage.fetch_long_term_facts`` (by
    primary-key id, without its ``agent_id`` filter) so the reconstructed
    ``LongTermFact`` objects carry the fields ``_with_events_sorted`` reads
    (category, occurred_at, mentioned_at, created_at), but never calls
    ``init_db`` -- the run artifact must not be mutated. Unknown ids are
    skipped, preserving the requested order.
    """
    if not fact_ids:
        return []
    placeholders = ", ".join("?" for _ in fact_ids)
    rows = conn.execute(
        f"""
        SELECT id, fact_text, subject, category, importance, confidence,
               source_message_ids, retrieved_count, used_count, editable,
               created_at, occurred_at, mentioned_at
        FROM long_term_memory
        WHERE id IN ({placeholders})
        """,
        list(fact_ids),
    ).fetchall()
    by_id = {
        int(row[0]): LongTermFact(
            fact_id=int(row[0]),
            fact_text=str(row[1]),
            subject=str(row[2]),
            category=str(row[3]),
            importance=int(row[4]),
            confidence=float(row[5]),
            source_message_ids=[int(value) for value in json.loads(row[6])],
            retrieved_count=int(row[7]),
            used_count=int(row[8]),
            editable=bool(row[9]),
            created_at=str(row[10]),
            occurred_at=row[11],
            mentioned_at=row[12],
        )
        for row in rows
    }
    return [by_id[fact_id] for fact_id in fact_ids if fact_id in by_id]


def _reconstruct_retrieved_facts(
    run_dir: Path,
    question_id: str,
    diagnostics_row: dict[str, Any],
) -> tuple[list[LongTermFact], bool]:
    """Rebuild the fact ordering the eval-time judge saw for one question.

    Returns ``(facts, reconstruction_complete)``. The persisted
    ``fused_fact_ids`` array is the pre-event-sort RRF order (retrieval stores
    it before ``_with_events_sorted``), so fetch by that order then re-apply
    the same event sort to match the live judge's positional rendering.
    """
    db_path = _memory_db_path(run_dir, question_id)
    expected_count = diagnostics_row.get("retrieved_long_term_fact_count")
    candidate_fallback = bool(diagnostics_row.get("candidate_fallback_used"))

    if not db_path.exists():
        return [], False

    try:
        with closing(_open_readonly(db_path)) as conn:
            _, _, fused_returned = _answer_retrieval_arrays(conn, question_id)
            facts = _fetch_long_term_facts_readonly(conn, fused_returned)
    except Exception:
        # A corrupt question DB must not sink the rescore; the row is still
        # written, judged over an empty fact set, flagged incomplete.
        return [], False

    facts = _with_events_sorted(facts)

    complete = True
    # (a) candidate-note fallback answer: no Tier-3 fused ids were the source.
    # candidate_fallback_used is True only when fallback notes were actually
    # retrieved; a zero-fact row with no fallback means the eval-time judge saw
    # an empty fact list, which reconstructs exactly (0 == expected 0 below).
    if candidate_fallback:
        complete = False
    # (b) reconstructed count disagrees with what the run recorded returning.
    if not isinstance(expected_count, int) or len(facts) != expected_count:
        complete = False
    return facts, complete


async def _judge_verdict(
    judge_input: LongMemEvalRecallJudgeInput,
    *,
    judge_model_group: str,
    judge_scorer: LongMemEvalRecallJudge | None,
    recall_judge_agent: Any,
    judge_agent_factory: AgentFactory | None,
    secrets: dict[str, str] | None,
    forbidden_secret_values: Sequence[str],
) -> Any:
    if judge_scorer is not None:
        # Mirror the harness fake-judge branch: guard the rendered input (which
        # carries the fact texts) and the verdict output against forbidden
        # secret values before either can be written.
        rendered_input = _render_recall_judge_input(judge_input)
        assert_no_forbidden_secret_values(
            forbidden_secret_values,
            LONGMEMEVAL_RECALL_JUDGE_PROMPT,
            rendered_input,
        )
        verdict = await judge_scorer(judge_input)
        assert_no_forbidden_secret_values(
            forbidden_secret_values,
            verdict.model_dump_json(),
        )
        return verdict
    # No fake scorer: fail closed through score_longmemeval_recall, which raises
    # HostPortNotConfigured when neither an agent nor a factory is supplied.
    return await score_longmemeval_recall(
        judge_input,
        judge_model_group=judge_model_group,
        agent=recall_judge_agent,
        judge_agent_factory=judge_agent_factory,
        secrets=secrets,
        forbidden_secret_values=forbidden_secret_values,
    )


async def rescore_preference_rows(
    run_dir: Path,
    dataset_path: Path,
    *,
    judge_model_group: str,
    judge_agent_factory: AgentFactory | None = None,
    judge_scorer: LongMemEvalRecallJudge | None = None,
    secrets: dict[str, str] | None = None,
    forbidden_secret_values: Sequence[str] = (),
) -> Path:
    """Re-judge preference judged-recall misses under the rubric-aware render.

    Idempotent regeneration: any prior ``preference_rescore.jsonl`` is removed
    up front so re-running never appends duplicate rows. Returns the artifact
    path (created empty when no preference miss rows exist).
    """
    # Mirror the harness precedent (run_longmemeval_subset): a secret VALUE
    # supplied via ``secrets`` is as forbidden as one named in
    # ``forbidden_secret_values``. Merge both (dedup, order-preserving) and use
    # the merged tuple everywhere renders/verdicts/artifact writes are guarded.
    loaded_secret_values = tuple((secrets or {}).values())
    guarded_secret_values = tuple(
        dict.fromkeys((*forbidden_secret_values, *loaded_secret_values))
    )

    artifact_path = run_dir / RESCORE_ARTIFACT_NAME
    # Overwrite, don't append: rescore is a regeneration of this run's verdicts,
    # not an accumulation across invocations. Truncate up front (rather than
    # only creating on first row) so a completed rescore that found zero
    # preference misses still leaves an empty artifact -- which the analysis
    # loader reads as "ran, nothing usable" (``[]``), distinct from "never ran"
    # (absent -> ``None``).
    artifact_path.write_text("", encoding="utf-8")

    diagnostics_rows, _ = _load_diagnostics(run_dir)
    # A resumed/retried run can emit several rows per question_id; the last row
    # is the question's final state.
    rows_by_question_id: dict[str, dict[str, Any]] = {}
    for row in diagnostics_rows:
        question_id = row.get("question_id")
        if isinstance(question_id, str):
            rows_by_question_id[question_id] = row

    dataset_by_id: dict[Any, dict[str, Any]] = {}
    for row in _load_dataset(dataset_path):
        dataset_by_id.setdefault(row.get("question_id"), row)

    # Build the judge agent once (live path) so its model id is stable across
    # rows; the fake-scorer path has no agent and reports a null model id.
    recall_judge_agent: Any = None
    judge_model_id: str | None = None
    if judge_scorer is None and judge_agent_factory is not None:
        recall_judge_agent = judge_agent_factory(judge_model_group, secrets=secrets)
        judge_model_id = _judge_model_id_from_agent(recall_judge_agent)

    for question_id, diagnostics_row in rows_by_question_id.items():
        if not _is_preference_miss(diagnostics_row):
            continue
        dataset_row = dataset_by_id.get(question_id)
        if dataset_row is None:
            # A preference miss whose question_id is absent from --dataset is a
            # dataset/run mismatch: there is no gold rubric to judge against, so
            # skip it (with a warning) rather than fabricate one.
            print(
                f"warning: skipping {question_id}: not found in dataset "
                f"{dataset_path}",
                file=sys.stderr,
            )
            continue

        facts, reconstruction_complete = _reconstruct_retrieved_facts(
            run_dir, question_id, diagnostics_row
        )
        judge_input = LongMemEvalRecallJudgeInput(
            question=dataset_row.get("question"),
            gold_answer=dataset_row.get("answer"),
            retrieved_fact_texts=tuple(fact.fact_text for fact in facts),
            question_type=diagnostics_row.get("question_type"),
        )
        verdict = await _judge_verdict(
            judge_input,
            judge_model_group=judge_model_group,
            judge_scorer=judge_scorer,
            recall_judge_agent=recall_judge_agent,
            judge_agent_factory=judge_agent_factory,
            secrets=secrets,
            forbidden_secret_values=guarded_secret_values,
        )

        rescore_row = PreferenceRescoreRow(
            question_id=question_id,
            question_type=str(diagnostics_row.get("question_type")),
            original_verdict=diagnostics_row.get("judge_verdict"),
            rubric_verdict=verdict.verdict,
            rubric_reason=verdict.reason,
            rubric_confidence=verdict.confidence,
            judge_model_id=judge_model_id,
            judge_prompt_version=LONGMEMEVAL_RECALL_JUDGE_PREFERENCE_PROMPT_VERSION,
            reconstruction_complete=reconstruction_complete,
        )
        _append_jsonl(
            artifact_path,
            rescore_row.model_dump(),
            guarded_secret_values,
        )

    return artifact_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-judge preference judged-recall misses in a completed "
            "LongMemEval run under the rubric-aware recall-judge render, and "
            "write preference_rescore.jsonl. Live-provider, opt-in."
        )
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--adapter", type=Path, default=None)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--judge-model-group", default="claude")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allow_live:
        # Opt-in like live_retrieval_baseline: without --allow-live the adapter
        # is never imported or loaded, so a dry invocation makes no provider
        # call and touches no provider wiring.
        print(
            "Preference rescore re-judges misses through a live provider "
            "adapter; pass --allow-live to run. Skipping."
        )
        return 0
    if args.adapter is None:
        print("--adapter is required with --allow-live.", file=sys.stderr)
        return 2
    adapter = _load_eval_adapter(args.adapter, require_judge=True)
    artifact_path = asyncio.run(
        rescore_preference_rows(
            args.run_dir,
            args.dataset,
            judge_model_group=args.judge_model_group,
            judge_agent_factory=adapter.build_longmemeval_recall_judge_agent,
        )
    )
    print(f"Preference rescore: {artifact_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
