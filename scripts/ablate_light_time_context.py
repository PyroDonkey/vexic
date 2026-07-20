"""Repeated ablation runner for Light time-context.

Compares two Light-extraction variants over the *exact* persisted Light
windows of one or more LongMemEval Vexic databases:

- ``baseline``: the prior shape -- transcript rendered without
  ``observed=`` labels (``render_transcript_unlabeled``) and the old
  temporal paragraph in the extraction prompt (``OLD_TEMPORAL_PARAGRAPH``).
- ``treated``: the current shape -- ``vexic.pipeline.render_transcript``
  (with ``observed=`` labels) and the current
  ``adapters.openrouter_live_adapter.EXTRACTION_INSTRUCTIONS``.

Every candidate's raw (pre-guard) and guarded (post
``apply_occurred_at_guards``) ``occurred_at`` are recorded so the five
deterministic metrics below can be computed per repeat and aggregated as
mean/min/max across repeats. ``fabricated_year_rate`` is scored both ways --
``fabricated_year_rate_raw`` and ``fabricated_year_rate_guarded`` -- since the
acceptance-critical number is the post-guard rate, not the raw one. This is a
live, opt-in evidence harness: it is
gated behind ``--allow-live`` and a provider-call budget cap, mirroring
``src/vexic/live_retrieval_baseline.py`` conventions. `docs/ai/REVIEW.md`
flags live harnesses as do-not-run during review; only the deterministic
metric functions are unit-tested (see
``tests/test_ablate_light_time_context.py``).

Evidence caveats (what these numbers do and do not prove):

- The "exact persisted Light windows" claim holds only when that database's
  history was consumed with the default batch size (``LIGHT_PHASE_BATCH_SIZE``
  = 50), the default (shared) agent scope, and full-batch consumption. A run
  that used a different batch size, an agent-scoped history, or stopped
  mid-batch reconstructs windows that differ from what Light actually saw.
- ``occurred_at_raw`` is the model's output *after* the ``FactCandidate``
  validator (which ships in both variants); it is not the pre-branch raw model
  string. The prompt's effect therefore lives in the raw-vs-raw comparison
  between variants, not in raw-vs-guarded within one variant.
- ``fabricated_year_rate_guarded`` shares its year-plausibility predicate with
  the shipped guard (``apply_occurred_at_guards``), so it is 0 (or None, with
  no dated rows) by construction. It is an implementation-invariant check that
  the guard did its job, not independent evidence that fabrication is absent.
- The provider budget counts ``agent.run()`` calls; a provider's
  structured-output retries may push actual upstream calls moderately above the
  cap.

Metric functions return None (not 0.0) on an empty denominator; aggregation
skips None repeats and reports ``repeats_with_data`` per metric.

Usage:
    uv run python scripts/ablate_light_time_context.py \\
        --db .eval-runs/<run>/<question-id>/memory.db \\
        --allow-live --repeats 5 --max-windows 8 \\
        --out .eval-runs/light-time-context-ablation
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra_path in (str(_REPO_ROOT / "src"), str(_REPO_ROOT)):
    if _extra_path not in sys.path:
        sys.path.insert(0, _extra_path)

from adapters.openrouter_live_adapter import (  # noqa: E402
    EXTRACTION_INSTRUCTIONS,
    EXTRACTION_MAX_OUTPUT_TOKENS,
    _agent as _build_agent,
    build_extraction_agent,
)
from vexic.models import FactCandidate  # noqa: E402
from vexic.pipeline import (  # noqa: E402
    LIGHT_PHASE_BATCH_SIZE,
    _plausible_years,
    _single_intext_date,
    apply_occurred_at_guards,
    render_transcript,
)
from vexic.storage import load_messages_since  # noqa: E402

# The paragraph currently shipped in EXTRACTION_INSTRUCTIONS (Task 5,
# ad93f22): guarded absolute/relative resolution rules keyed off the
# observed= marker. Verified verbatim against
# adapters/openrouter_live_adapter.py at authoring time.
NEW_TEMPORAL_PARAGRAPH = """\
Each transcript line's marker may carry observed=YYYY-MM-DD Day -- the date
that message was recorded. Observed time is recording time, never event time:
never copy an observed date into occurred_at by itself.
Populate occurred_at only from temporal references in the transcript text:
- An absolute date stated in the text: copy it at exactly its stated
  precision -- "2025-03-14" for a full date, "2025-03" for a month, "2025"
  for a year. If the text states a month and day but no year, use the
  observed date's year only when tense and context make the year
  unambiguous; otherwise leave occurred_at null. Never invent a year.
- A relative reference ("last Sunday", "three weekends ago", "back in
  March"): resolve it against the observed date of the message that says it,
  and only when the resolution is unambiguous. Output only the precision the
  resolution supports: "last Sunday" against a known observed date gives a
  full date; "a few months ago" gives at most a year-month; "years ago"
  resolves to nothing -- leave it null.
Never fabricate any component: no invented days, months, or years, and no
defaulting missing components to 01. When in doubt, less precision or null.
Leave occurred_at null when no temporal reference exists. Look especially
hard for a date on category="event" facts.\
"""

# The paragraph it replaced (commit 9285439, immediately before Task 5's
# ad93f22): plain "state or clearly imply" wording, no observed= semantics.
OLD_TEMPORAL_PARAGRAPH = """\
When the transcript states or clearly implies a temporal reference for when
the fact occurred (a date, month, year, or relative time you can resolve
against context), populate occurred_at with an ISO 8601 string at whatever
precision is actually known: a full date ("2025-03-14"), a year-month
("2025-03"), or a year ("2025"). Never fabricate a day or month you were not
told. Leave occurred_at null when no temporal reference exists. Look
especially hard for a date on category="event" facts, since event facts
should carry an occurred_at whenever the transcript gives any basis for one.\
"""

_RELATIVE_KEYWORDS = ("last ", "ago", "yesterday", "next ", "weekend")

VARIANTS = ("baseline", "treated")


class AblationConfigError(ValueError):
    pass


def render_transcript_unlabeled(rows: list[tuple[int, str | None, Any]]) -> str:
    """Old (pre-Task-2) render: no ``observed=`` labels.

    Reuses ``render_transcript`` with every timestamp blanked, which
    reproduces the old ``[message_id=N] Role: text`` marker exactly -- no
    duplicated rendering logic.
    """
    unlabeled_rows = [(message_id, None, msg) for message_id, _timestamp, msg in rows]
    return render_transcript(unlabeled_rows)


def build_baseline_instructions(instructions: str = EXTRACTION_INSTRUCTIONS) -> str:
    """Swap the current temporal paragraph for the old one, failing loudly
    if the current paragraph is not present verbatim (the prompt has drifted
    out from under this harness's hardcoded fixture text)."""
    if NEW_TEMPORAL_PARAGRAPH not in instructions:
        raise AblationConfigError(
            "NEW_TEMPORAL_PARAGRAPH was not found verbatim in the supplied "
            "extraction instructions; adapters/openrouter_live_adapter.py's "
            "EXTRACTION_INSTRUCTIONS has drifted from this script's hardcoded "
            "fixture text. Update NEW_TEMPORAL_PARAGRAPH and "
            "OLD_TEMPORAL_PARAGRAPH in scripts/ablate_light_time_context.py."
        )
    return instructions.replace(NEW_TEMPORAL_PARAGRAPH, OLD_TEMPORAL_PARAGRAPH, 1)


def _has_relative_reference(line: str) -> bool:
    lowered = line.lower()
    return any(keyword in lowered for keyword in _RELATIVE_KEYWORDS)


# ---------------------------------------------------------------------------
# Deterministic metrics (unit-tested; no DB, no network, no agent).
# ---------------------------------------------------------------------------


def fabricated_year_rate(
    records: list[dict[str, Any]],
    plausible_years_by_window: dict[str, Iterable[int]],
    *,
    field: str = "occurred_at_raw",
) -> float | None:
    """Share of dated candidates (``field`` not null) whose year falls
    outside their window's plausible years (``_plausible_years``).

    ``field`` defaults to ``"occurred_at_raw"`` (pre-guard) but must also be
    callable with ``"occurred_at_guarded"`` (post
    ``apply_occurred_at_guards``) so the acceptance-critical "treated
    post-guard fabricated year rate" number can actually be computed: the
    guard can itself null a backfilled fabricated year, and that
    post-guard rate is not observable by only ever scoring the raw field.

    None when there are no dated candidates -- there is no fabrication to
    measure, and a 0.0 there would read as spurious evidence of "no
    fabrication observed". Aggregation skips None repeats.
    """
    dated = [record for record in records if record.get(field)]
    if not dated:
        return None
    fabricated = 0
    for record in dated:
        plausible = set(plausible_years_by_window.get(record["window"], ()))
        year = int(str(record[field])[:4])
        if year not in plausible:
            fabricated += 1
    return fabricated / len(dated)


def intext_copy_rate(records: list[dict[str, Any]]) -> float | None:
    """Share of event candidates with a single unambiguous in-text date
    (``_single_intext_date``) whose raw ``occurred_at`` copies that date
    exactly, at its stated precision.

    None when no event candidate has a resolvable single in-text date.
    """
    with_intext = [
        (record, _single_intext_date(str(record.get("fact_text", ""))))
        for record in records
        if record.get("category") == "event"
    ]
    with_intext = [(record, intext) for record, intext in with_intext if intext is not None]
    if not with_intext:
        return None
    matches = sum(
        1 for record, intext in with_intext if record.get("occurred_at_raw") == intext
    )
    return matches / len(with_intext)


def dated_event_rate(records: list[dict[str, Any]]) -> float | None:
    """Share of event candidates carrying a non-null post-guard
    ``occurred_at``. None when there are no event candidates."""
    events = [record for record in records if record.get("category") == "event"]
    if not events:
        return None
    dated = sum(1 for record in events if record.get("occurred_at_guarded"))
    return dated / len(events)


def full_date_from_partial_rate(records: list[dict[str, Any]]) -> float | None:
    """Share of candidates with a single in-text date where the raw
    ``occurred_at`` claims full-date precision but the in-text date itself
    only stated month or year granularity -- a precision-fabrication signal
    distinct from year fabrication.

    None when no candidate has a resolvable single in-text date.
    """
    with_intext = [
        (record, _single_intext_date(str(record.get("fact_text", ""))))
        for record in records
    ]
    with_intext = [(record, intext) for record, intext in with_intext if intext is not None]
    if not with_intext:
        return None
    mismatches = 0
    for record, intext in with_intext:
        raw = record.get("occurred_at_raw")
        if isinstance(raw, str) and len(raw) == 10 and len(intext) < 10:
            mismatches += 1
    return mismatches / len(with_intext)


_METRIC_NAMES = (
    "fabricated_year_rate_raw",
    "fabricated_year_rate_guarded",
    "intext_copy_rate",
    "dated_event_rate",
    "full_date_from_partial_rate",
)


# ---------------------------------------------------------------------------
# Live runner (not exercised by tests).
# ---------------------------------------------------------------------------


class ProviderBudgetExhausted(RuntimeError):
    pass


class ProviderBudget:
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


@dataclass
class WindowJob:
    db: str
    window_key: str
    rows: list[tuple[int, str | None, Any]] = field(repr=False)


def _collect_windows(dbs: list[str], max_windows: int, *, limit: int) -> list[WindowJob]:
    jobs: list[WindowJob] = []
    for db in dbs:
        after_id = 0
        index = 0
        while len(jobs) < max_windows:
            rows = load_messages_since(
                db, after_id, limit=limit, exclude_session_prefixes=("onboarding:",)
            )
            if not rows:
                break
            jobs.append(WindowJob(db=db, window_key=f"{db}#w{index}", rows=rows))
            after_id = max(message_id for message_id, _, _ in rows)
            index += 1
        if len(jobs) >= max_windows:
            break
    return jobs


def _line_by_message_id(rows: list[tuple[int, str | None, Any]], *, labeled: bool) -> dict[int, str]:
    lines: dict[int, str] = {}
    for message_id, timestamp, msg in rows:
        text = render_transcript([(message_id, timestamp if labeled else None, msg)])
        if text:
            lines[message_id] = text
    return lines


def _variant_transcript(rows: list[tuple[int, str | None, Any]], variant: str) -> str:
    return render_transcript(rows) if variant == "treated" else render_transcript_unlabeled(rows)


async def _run_agent(agent: Any, transcript: str, budget: ProviderBudget) -> list[FactCandidate]:
    budget.take()
    result = await agent.run(transcript)
    return list(result.output)


async def _run_ablation(args: argparse.Namespace) -> dict[str, Any]:
    jobs = _collect_windows(args.db, args.max_windows, limit=LIGHT_PHASE_BATCH_SIZE)
    baseline_instructions = build_baseline_instructions()
    agents = {
        "treated": build_extraction_agent(args.model_group),
        "baseline": _build_agent(
            args.model_group,
            list[FactCandidate],
            baseline_instructions,
            default_max_tokens=EXTRACTION_MAX_OUTPUT_TOKENS,
        ),
    }
    budget = ProviderBudget(args.max_provider_calls)

    candidate_records: list[dict[str, Any]] = []
    audit_records: list[dict[str, Any]] = []
    budget_exhausted = False

    for job in jobs:
        for variant in VARIANTS:
            transcript = _variant_transcript(job.rows, variant)
            plausible_years = sorted(_plausible_years(job.rows, transcript))
            message_lines = _line_by_message_id(job.rows, labeled=(variant == "treated"))
            audit_records.append(
                {
                    "record_type": "window_transcript_hash",
                    "window": job.window_key,
                    "db": job.db,
                    "variant": variant,
                    "message_count": len(job.rows),
                    "message_id_range": [job.rows[0][0], job.rows[-1][0]],
                    "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
                    "plausible_years": plausible_years,
                }
            )
            if budget_exhausted:
                continue
            for repeat_idx in range(args.repeats):
                if budget.remaining() <= 0:
                    budget_exhausted = True
                    break
                raw_candidates = await _run_agent(agents[variant], transcript, budget)
                guarded_candidates = [candidate.model_copy(deep=True) for candidate in raw_candidates]
                apply_occurred_at_guards(guarded_candidates, job.rows, transcript)
                for raw, guarded in zip(raw_candidates, guarded_candidates, strict=True):
                    relative_reference_lines = [
                        {"message_id": message_id, "text": message_lines[message_id]}
                        for message_id in raw.source_message_ids
                        if message_id in message_lines
                        and _has_relative_reference(message_lines[message_id])
                    ]
                    record = {
                        "window": job.window_key,
                        "db": job.db,
                        "variant": variant,
                        "repeat": repeat_idx,
                        "fact_text": raw.fact_text,
                        "category": raw.category,
                        "occurred_at_raw": raw.occurred_at,
                        "occurred_at_guarded": guarded.occurred_at,
                        "source_message_ids": list(raw.source_message_ids),
                    }
                    candidate_records.append(record)
                    audit_records.append(
                        {
                            "record_type": "candidate",
                            **record,
                            "relative_reference_lines": relative_reference_lines,
                        }
                    )

    metrics_document = _build_metrics_document(
        args=args,
        jobs=jobs,
        candidate_records=candidate_records,
        audit_records=audit_records,
        budget=budget,
        budget_exhausted=budget_exhausted,
    )
    return {"metrics_document": metrics_document, "audit_records": audit_records}


def _build_metrics_document(
    *,
    args: argparse.Namespace,
    jobs: list[WindowJob],
    candidate_records: list[dict[str, Any]],
    audit_records: list[dict[str, Any]],
    budget: ProviderBudget,
    budget_exhausted: bool,
) -> dict[str, Any]:
    variants_doc: dict[str, Any] = {}
    for variant in VARIANTS:
        variant_records = [record for record in candidate_records if record["variant"] == variant]
        plausible_years_by_window = {
            record["window"]: record["plausible_years"]
            for record in audit_records
            if record.get("record_type") == "window_transcript_hash" and record["variant"] == variant
        }
        per_repeat: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for record in variant_records:
            per_repeat[record["repeat"]].append(record)
        # Iterate over every attempted repeat (range(args.repeats), bounded by
        # budget truncation upstream), NOT sorted(per_repeat): a repeat that
        # produced zero candidates must appear as a None-metric slot, not
        # vanish and inflate the apparent denominator.
        repeat_indices = range(args.repeats)

        def _values(name: str) -> list[float | None]:
            if name == "fabricated_year_rate_raw":
                return [
                    fabricated_year_rate(
                        per_repeat[i], plausible_years_by_window, field="occurred_at_raw"
                    )
                    for i in repeat_indices
                ]
            if name == "fabricated_year_rate_guarded":
                return [
                    fabricated_year_rate(
                        per_repeat[i], plausible_years_by_window, field="occurred_at_guarded"
                    )
                    for i in repeat_indices
                ]
            fn = {
                "intext_copy_rate": intext_copy_rate,
                "dated_event_rate": dated_event_rate,
                "full_date_from_partial_rate": full_date_from_partial_rate,
            }[name]
            return [fn(per_repeat[i]) for i in repeat_indices]

        metrics: dict[str, Any] = {}
        for name in _METRIC_NAMES:
            values = _values(name)
            # A metric with an empty denominator in a repeat is None, not 0.0;
            # aggregate over the repeats that actually carried data and report
            # that count. All-None -> the metric is null in the JSON.
            present = [value for value in values if value is not None]
            metrics[name] = {
                "mean": statistics.fmean(present) if present else None,
                "min": min(present) if present else None,
                "max": max(present) if present else None,
                "repeats_with_data": len(present),
                "per_repeat": values,
            }

        variants_doc[variant] = {
            "candidate_count": len(variant_records),
            "event_candidate_count": sum(
                1 for record in variant_records if record["category"] == "event"
            ),
            "repeats_attempted": args.repeats,
            "repeats_with_candidates": sum(1 for i in repeat_indices if per_repeat[i]),
            "metrics": metrics,
        }

    return {
        "dbs": list(args.db),
        "model_group": args.model_group,
        "windows_used": len(jobs),
        "caps": {
            "repeats": args.repeats,
            "max_windows": args.max_windows,
            "max_provider_calls": args.max_provider_calls,
        },
        "provider_calls_used": budget.used,
        "budget_exhausted": budget_exhausted,
        "variants": variants_doc,
    }


def _write_artifacts(out_dir: Path, result: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ablation_metrics.json").write_text(
        json.dumps(result["metrics_document"], indent=2, sort_keys=True) + "\n"
    )
    with (out_dir / "ablation_audit.jsonl").open("w", encoding="utf-8") as handle:
        for record in result["audit_records"]:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the opt-in live Light time-context ablation."
    )
    parser.add_argument("--db", action="append", default=[])
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--max-windows", type=int, default=8)
    parser.add_argument("--max-provider-calls", type=int, default=120)
    parser.add_argument("--out")
    parser.add_argument("--model-group", default="extraction")
    return parser


def _require(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise AblationConfigError(f"{name} is required with --allow-live.")
    return value


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("repeats", "max_windows", "max_provider_calls"):
        if getattr(args, name) <= 0:
            raise AblationConfigError(f"--{name.replace('_', '-')} must be greater than 0.")
    if not args.db:
        raise AblationConfigError("--db is required with --allow-live (repeatable).")
    for db in args.db:
        if not Path(db).exists():
            raise AblationConfigError(f"--db not found: {db}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if not args.allow_live:
        print("Light time-context ablation skipped; pass --allow-live to run provider calls.")
        return 0

    try:
        out_dir = Path(_require(args.out, "--out"))
        _validate_args(args)
        result = asyncio.run(_run_ablation(args))
        _write_artifacts(out_dir, result)
    except AblationConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Light time-context ablation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
