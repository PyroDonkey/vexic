"""Tier-3 coverage probe for the class-3 miss gaps.

Read-only over a LongMemEval run directory, with no provider calls. For every
gap named in the gap fixture it asks one question: does this run's Tier 3
contain the missing constituent?

  * ``covered``        -- a live ``long_term_memory`` fact matches every match token
  * ``tier2-only``     -- no Tier-3 match, but an active ``memory_candidates`` row
                          matches (the constituent was extracted but never promoted)
  * ``tier3-undated``  -- a fact matches but carries no date, for gaps whose whole
                          miss is the missing date (the fact existing is not enough)
  * ``absent``         -- neither tier holds it (extraction never captured it)

A question is oracle-complete when every one of its gaps is ``covered``;
questions the earlier curation already found complete carry no gaps and are
complete by construction.

This probe reads run databases read-only and copies nothing. Opening a WAL-mode
run database in read-only mode may create or update ``-wal``/``-shm`` sidecars
next to it; byte-frozen provenance is the simulation harness's guarantee (it
heals a copy), not this probe's.

Point it at the frozen benchmark shards to reproduce the baseline classification,
or at a fresh run to measure the same gaps on current code:

    uv run python scripts/probe_class3_gaps.py --gaps .eval-runs/class3-gaps/gaps.json
    uv run python scripts/probe_class3_gaps.py --gaps .eval-runs/class3-gaps/gaps.json \\
        --run-dir .eval-runs/class3-gaps/sentinel/<stamp> --out .eval-runs/class3-gaps

Matching is deterministic substring containment, case-folded, over the tokens
curated in the fixture. It is a probe, not a judge: a `covered` verdict means
the constituent text is present in Tier 3, not that the run answered the
question.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra_path in (str(_REPO_ROOT / "src"), str(_REPO_ROOT)):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

from vexic.longmemeval_analysis import _question_path_component  # noqa: E402

_SIM_PATH = _REPO_ROOT / "scripts" / "simulate_mentioned_at_promotion.py"


def _load_gap_fixture_module() -> Any:
    """Reuse the gap-fixture models from the simulation harness.

    scripts/ is not an importable package, so the sibling script is loaded by
    path (the pattern tests use for these harnesses).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "simulate_mentioned_at_promotion", _SIM_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sim = _load_gap_fixture_module()
GapFixtureError = _sim.GapFixtureError
load_gap_fixture = _sim.load_gap_fixture


def _reject_empty_match_tokens(entries: list[Any]) -> None:
    """Fail loud on any gap with no match tokens or a blank one.

    An empty token list can never match (``_matches`` returns False), so it
    would silently classify as ``absent`` rather than covered -- a fixture
    defect masquerading as a real miss. A blank token (empty or whitespace) is
    the opposite defect: a substring of every text, it would classify covered
    against ANY Tier-3 fact even alongside a real token. It validates the
    entries that will be probed (after any ``--question-id`` filter), fires
    before any question is probed, and is independent of ``--run-dir``, which
    only turns a missing per-question DB into a reported skip.
    """
    for entry in entries:
        for gap in entry.gaps:
            if not gap.match_tokens:
                raise GapFixtureError(
                    f"question {entry.question_id} gap {gap.gap_id}: "
                    "match_tokens is empty; an empty token list never matches"
                )
            if any(not token.strip() for token in gap.match_tokens):
                raise GapFixtureError(
                    f"question {entry.question_id} gap {gap.gap_id}: "
                    "match_tokens contains a blank token; a blank token is a "
                    "substring of every text and would match anything"
                )


def resolve_run_dir(entry: Any, override: Path | None) -> Path:
    return Path(entry.run_dir) if override is None else override


def question_db_path(entry: Any, run_dir: Path) -> Path:
    db_path = run_dir / _question_path_component(entry.question_id) / "memory.db"
    if not db_path.exists():
        raise GapFixtureError(f"question {entry.question_id}: no run DB at {db_path}")
    return db_path


def _matches(text: str, tokens: list[str]) -> bool:
    """Every token must appear; an empty list or a blank token never matches.

    A blank token (empty or whitespace) is a substring of every text, so it
    would make ``all(...)`` skip past it and match anything. Guarding it here,
    consistent with the empty-list guard, keeps matching skip-proof even if a
    blank token reaches this function; the load path rejects such fixtures up
    front via ``_reject_empty_match_tokens``.
    """
    if not tokens or any(not token.strip() for token in tokens):
        return False
    folded = text.casefold()
    return all(token.casefold() in folded for token in tokens)


def _tier_texts(db_path: Path) -> tuple[list[tuple[str, bool]], list[str]]:
    """Live Tier-3 facts (text, is-dated) and active Tier-2 candidate texts.

    A fact counts as dated when it carries ``occurred_at`` (event time) or the
    derived ``mentioned_at`` provenance date (ADR 0037); a pre-migration
    artifact has no ``mentioned_at`` column at all, and likewise may have no
    ``needs_review`` column on ``memory_candidates`` (retired/stale predate
    both, so those terms stay unconditional).
    """
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(long_term_memory)")}
        mentioned_at_select = "mentioned_at" if "mentioned_at" in columns else "NULL"
        candidate_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")
        }
        needs_review_filter = (
            "AND needs_review = 0" if "needs_review" in candidate_columns else ""
        )
        facts = [
            (
                str(row[0]),
                bool((row[1] or "").strip() or (row[2] or "").strip()),
            )
            for row in conn.execute(
                "SELECT fact_text, occurred_at, "
                f"{mentioned_at_select} FROM long_term_memory WHERE retired = 0"
            )
        ]
        candidates = [
            str(row[0])
            for row in conn.execute(
                "SELECT fact_text FROM memory_candidates "
                f"WHERE retired = 0 AND stale = 0 {needs_review_filter}"
            )
        ]
    return facts, candidates


def _diagnostics_row(run_dir: Path, question_id: str) -> dict[str, Any]:
    path = run_dir / "diagnostics.jsonl"
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("question_id") == question_id:
            return payload
    return {}


def probe_question(entry: Any, *, run_dir_override: Path | None = None) -> dict[str, Any]:
    run_dir = resolve_run_dir(entry, run_dir_override)
    db_path = question_db_path(entry, run_dir)
    facts, candidates = _tier_texts(db_path)
    diagnostics = _diagnostics_row(run_dir, entry.question_id)

    gap_results: list[dict[str, Any]] = []
    for gap in entry.gaps:
        matching_facts = [
            dated for text, dated in facts if _matches(text, gap.match_tokens)
        ]
        if matching_facts:
            # A tier3-undated gap is not closed by the fact merely existing --
            # the whole gap is that the fact carried no date to derive from.
            coverage = (
                "covered"
                if gap.kind != "tier3-undated" or any(matching_facts)
                else "tier3-undated"
            )
        elif any(_matches(text, gap.match_tokens) for text in candidates):
            coverage = "tier2-only"
        else:
            coverage = "absent"
        gap_results.append(
            {
                "gap_id": gap.gap_id,
                "kind": gap.kind,
                "match_tokens": gap.match_tokens,
                "coverage": coverage,
            }
        )

    return {
        "question_id": entry.question_id,
        "bucket": entry.bucket,
        "run_dir": str(run_dir),
        "tier3_fact_count": len(facts),
        "tier2_candidate_count": len(candidates),
        "gaps": gap_results,
        "oracle_complete": all(gap["coverage"] == "covered" for gap in gap_results),
        "judge_verdict": diagnostics.get("judge_verdict"),
        "status": diagnostics.get("status"),
        "answer_promoted_to_tier3": diagnostics.get("answer_promoted_to_tier3"),
        "answer_retrieved_from_tier3": diagnostics.get("answer_retrieved_from_tier3"),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    gaps = [gap for result in results for gap in result["gaps"]]
    by_bucket: dict[str, int] = {}
    for result in results:
        by_bucket[result["bucket"]] = by_bucket.get(result["bucket"], 0) + 1
    return {
        "questions": len(results),
        "oracle_complete": sum(1 for result in results if result["oracle_complete"]),
        "gaps_total": len(gaps),
        "gaps_covered": sum(1 for gap in gaps if gap["coverage"] == "covered"),
        "gaps_tier2_only": sum(1 for gap in gaps if gap["coverage"] == "tier2-only"),
        "gaps_tier3_undated": sum(
            1 for gap in gaps if gap["coverage"] == "tier3-undated"
        ),
        "gaps_absent": sum(1 for gap in gaps if gap["coverage"] == "absent"),
        "questions_by_bucket": by_bucket,
    }


def render_table(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Class-3 gap probe",
        "",
        "| question | bucket | gap | coverage | oracle-complete | judge |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        complete = "yes" if result["oracle_complete"] else "no"
        judge = result["judge_verdict"] or "-"
        if not result["gaps"]:
            lines.append(
                f"| {result['question_id']} | {result['bucket']} | (none) | - | {complete} | {judge} |"
            )
            continue
        for gap in result["gaps"]:
            lines.append(
                f"| {result['question_id']} | {result['bucket']} | {gap['gap_id']} "
                f"| {gap['coverage']} | {complete} | {judge} |"
            )
    return "\n".join(lines) + "\n"


def _write_artifacts(out_dir: Path, doc: dict[str, Any], table: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "class3_gap_probe.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "class3_gap_probe.md").write_text(table, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Tier-3 coverage of the class-3 miss gaps in a LongMemEval "
            "run directory."
        )
    )
    parser.add_argument("--gaps", required=True, type=Path, help="Gap fixture JSON.")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help=(
            "Probe this run directory instead of each question's frozen run_dir. "
            "Questions with no DB under it are skipped."
        ),
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--question-id",
        action="append",
        default=[],
        help="Restrict the probe to these question ids. Repeatable.",
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
    # Validate only the entries that will be probed, and before any DB access:
    # a malformed UNSELECTED question must not abort a targeted probe.
    try:
        _reject_empty_match_tokens(entries)
    except GapFixtureError as exc:
        print(f"gap fixture error: {exc}", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in entries:
        try:
            results.append(probe_question(entry, run_dir_override=args.run_dir))
        except GapFixtureError as exc:
            # A --run-dir holding only some questions is the normal shape of a
            # narrow sentinel re-run; skipping is reported, never silent.
            if args.run_dir is None:
                print(f"gap fixture error: {exc}", file=sys.stderr)
                return 2
            skipped.append(entry.question_id)

    table = render_table(results)
    doc = {
        "run_dir_override": None if args.run_dir is None else str(args.run_dir),
        "skipped_question_ids": skipped,
        "summary": summarize(results),
        "questions": results,
    }
    if args.out is not None:
        _write_artifacts(args.out, doc, table)
    print(table)
    if skipped:
        print(f"skipped (no DB under --run-dir): {', '.join(skipped)}", file=sys.stderr)
    print(json.dumps(doc["summary"], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
