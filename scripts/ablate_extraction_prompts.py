"""Repeated extraction-prompt ablation runner.

Reconstructs the *exact* persisted Light windows of one or more LongMemEval
Vexic databases and runs a 4-condition factorial over two additive extraction
instructions, scoring five target-fact "did the answer survive extraction"
hits with an explicit CNF rubric plus candidate-volume and token-cost metrics.

The four conditions add nothing (``control``), a granularity/detail paragraph
(``G``), an update-scanning paragraph (``U``), or both (``G+U``) to the shipped
``adapters.openrouter_live_adapter.EXTRACTION_INSTRUCTIONS``. Each condition
runs against the same persisted transcript per window, so this is a prompt-only
ablation: one rendered transcript per window is shared by all four conditions.

This is a live, opt-in evidence harness gated behind ``--allow-live`` and a
provider-call budget cap, mirroring ``src/vexic/live_retrieval_baseline.py`` and
its sibling ``scripts/ablate_light_time_context.py``. It is not Vexic
runtime: it lives under ``scripts/`` and imports only public ``vexic.*`` and the
repo-local ``adapters.*`` consumer. ``docs/ai/REVIEW.md`` flags live harnesses
as do-not-run during review; only the deterministic surface is unit-tested (see
``tests/test_ablate_extraction_prompts.py``).

Evidence caveats (what these numbers do and do not prove):

- The "exact persisted Light windows" claim holds only when that database's
  history was consumed with the default batch size (``LIGHT_PHASE_BATCH_SIZE``
  = 50), the default (shared) agent scope, full-batch consumption, and the same
  ``onboarding:`` session exclusion. A run that used a different batch size, an
  agent-scoped history, or stopped mid-batch reconstructs different windows.
- A rubric HIT means one single candidate's ``fact_text`` carried every
  answer-bearing token; it is not a semantic-equivalence judgment. A miss can
  be a real extraction failure or merely different phrasing than the rubric
  anticipated.
- The provider budget counts ``agent.run()`` calls; a provider's
  structured-output retries may push actual upstream calls moderately above the
  cap.

Empty-denominator metrics return None (not 0.0) so an absent measurement never
reads as a spurious zero.

Usage:
    uv run python scripts/ablate_extraction_prompts.py \\
        --db .eval-runs/<run>/<question-id>/memory.db \\
        --allow-live --repeats 5 \\
        --out .eval-runs/extraction-prompt-ablation

    # Deterministic binding check -- no provider calls, no --allow-live needed:
    uv run python scripts/ablate_extraction_prompts.py --bind-only \\
        --db .eval-runs/<run>/<question-id>/memory.db
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
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
    apply_occurred_at_guards,
    keep_candidates_with_valid_source_ids,
    render_transcript,
    rendered_message_ids,
)
from vexic.storage import load_messages_since  # noqa: E402


class AblationConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Instruction conditions.
# ---------------------------------------------------------------------------

CONDITIONS = ("control", "G", "U", "G+U")

# U_ADDITION is V1_ADDITION from the prior extraction experiment verbatim: an
# update-scanning paragraph, with its original leading newline so it appends
# cleanly onto the base instructions.
U_ADDITION = """\

Statements that revise or update a previously stated fact are high-priority
facts: when the user mentions that something has changed -- including brief
asides, parentheticals, and passing remarks inside a message that is mostly
about another topic ("actually", "just", "now", "again", "these days") --
always extract the updated value as its own fact, even if it is incidental
to the main subject of the conversation. Scan every user message, including
long multi-topic ones, for these embedded updates; do not let an update be
absorbed into a fact about the surrounding topic.\
"""

# G_ADDITION is V2_ADDITION from the same experiment, truncated *before* its
# final promotion-policy sentence (see EXCLUDED_PROMOTION_SENTENCE): a
# detail/granularity paragraph that ends at "...the table's overall shape."
G_ADDITION = """\

Preserve exact answer-bearing details in fact_text: dollar amounts,
quantities, frequencies, dates, institution and product names, store names,
and per-row or per-cell assignments in tables and schedules. A specific
transaction, redemption, approval, or individual assignment (for example,
one person's shift on one day of a rotation) is its own separate fact; do
not collapse it into a generic summary fact, and when the transcript
contains a table or schedule, extract each answer-bearing cell assignment
as a distinct fact rather than only describing the table's overall shape.\
"""

# The sentence deliberately dropped from the tail of V2_ADDITION. It is
# promotion policy (a completed past occurrence with no date becomes category
# "fact"), deliberately excluded from this ablation as a Memory Invariant 11
# bypass (ADR 0037 owns the undated-event promotion path). Kept as a named,
# testable constant so the exclusion is pinned: no built condition may
# contain it.
EXCLUDED_PROMOTION_SENTENCE = """\
A completed past occurrence whose outcome remains true (an approval amount,
a purchase, a redemption) should be categorized as "fact" when the
transcript gives no date for it, so the durable detail is not lost behind
an undatable event.\
"""


def normalize(value: str) -> str:
    """Lowercase and collapse every run of whitespace (newlines and tabs
    included) to a single space, then strip. Shared by the locator/rubric
    matcher and every drift guard; mirrors the prior experiment's score.py norm."""
    return re.sub(r"\s+", " ", value.lower()).strip()


def build_condition_instructions(
    condition: str, base: str = EXTRACTION_INSTRUCTIONS
) -> str:
    """Assemble the extraction instructions for one ablation condition.

    ``control`` returns ``base`` unchanged; ``G`` / ``U`` append their single
    addition; ``G+U`` appends both in the fixed canonical order G-then-U. The
    adapter module constant is never mutated -- callers build a per-condition
    agent from the returned text.

    Fails loudly (``AblationConfigError``) if either addition is already
    present (normalized) in ``base``: that means the shipped prompt has drifted
    to include the experiment text, and appending again would double it.
    """
    if condition not in CONDITIONS:
        raise AblationConfigError(f"unknown condition: {condition!r}")
    base_norm = normalize(base)
    if normalize(G_ADDITION) in base_norm:
        raise AblationConfigError(
            "G_ADDITION is already present in the base extraction instructions; "
            "the shipped prompt has drifted to include this experiment text."
        )
    if normalize(U_ADDITION) in base_norm:
        raise AblationConfigError(
            "U_ADDITION is already present in the base extraction instructions; "
            "the shipped prompt has drifted to include this experiment text."
        )
    if condition == "control":
        built = base
    elif condition == "G":
        built = base + G_ADDITION
    elif condition == "U":
        built = base + U_ADDITION
    else:
        # "G+U": canonical order is base, then G, then U.
        built = base + G_ADDITION + U_ADDITION
    # Runtime twin of the test pin: the promotion-policy sentence must never
    # ship in any built condition, even if a future base prompt absorbs it.
    if normalize(EXCLUDED_PROMOTION_SENTENCE) in normalize(built):
        raise AblationConfigError(
            "the excluded promotion-policy sentence is present in the built "
            f"{condition!r} instructions; it must never ship in any condition."
        )
    return built


# ---------------------------------------------------------------------------
# Targets, rubric, and window binding.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A LongMemEval answer fact, bound by locating one answer-bearing user
    turn's window and scored by a CNF rubric over extracted candidates."""

    target_id: str
    window_locators: tuple[str, ...]
    rubric: tuple[tuple[str, ...], ...]


# Locators are distinctive normalized substrings from ONE answer-bearing user
# turn per case (co-resident with the answer content because a single stored
# message never spans a window boundary). Rubric = CNF: a list of any_of groups
# that ALL must match within a SINGLE candidate's normalized fact_text. Every
# locator and token is pre-normalized (token == normalize(token)).
TARGETS: tuple[Target, ...] = (
    Target(
        target_id="830ce83f",
        window_locators=("my friend rachel actually just moved back to the suburbs again",),
        rubric=(("rachel",), ("suburb",)),
    ),
    Target(
        target_id="945e3d21",
        window_locators=("i'm more focused on days when i attend yoga classes",),
        rubric=(
            ("yoga",),
            ("three times a week", "3 times a week", "three times per week", "3x"),
        ),
    ),
    Target(
        target_id="852ce960",
        window_locators=("worth it to have a backyard like the one i'll have",),
        rubric=(
            ("400,000", "400000", "400k"),
            ("pre-approv", "preapprov", "wells fargo", "mortgage"),
        ),
    ),
    Target(
        target_id="51a45a95",
        window_locators=(
            "which was a nice surprise since i didn't know i had it in my email inbox",
        ),
        rubric=(("creamer",), ("$5", "5 coupon"), ("target", "cartwheel")),
    ),
    Target(
        target_id="7161e7e2",
        window_locators=("admon magdy ehab sara mostafa nemr adam",),
        rubric=(("admon",), ("sunday",)),
    ),
)


def rubric_hit(fact_texts: Sequence[str], rubric: tuple[tuple[str, ...], ...]) -> bool:
    """True when ONE SINGLE candidate's normalized ``fact_text`` satisfies
    every any_of group in the rubric. Two candidates each covering half is a
    miss -- the answer must survive as one durable fact, not be scattered."""
    for text in fact_texts:
        norm = normalize(text)
        if all(any(token in norm for token in group) for group in rubric):
            return True
    return False


@dataclass
class Window:
    """One reconstructed Light window: the exact rows Light saw, plus the
    single shared rendered transcript (all four conditions see this)."""

    db: str
    key: str
    rows: list[tuple[int, str | None, Any]] = field(default_factory=list, repr=False)
    transcript: str = ""
    normalized: str = ""


@dataclass
class BindingResult:
    target_id: str
    dbs: list[str]
    windows: list[str]
    multi_match: bool
    rubric: tuple[tuple[str, ...], ...]


def _bind_target(target: Target, windows: Sequence[Window]) -> BindingResult:
    """Bind a target to every window whose normalized transcript contains all
    of the target's locators. Zero matches is a hard config error naming the
    target and, per window, the first locator that missed. >1 match binds all
    windows and flags ``multi_match``."""
    matched = [
        window
        for window in windows
        if all(locator in window.normalized for locator in target.window_locators)
    ]
    if not matched:
        if windows:
            diagnostics = []
            for window in windows:
                missing = next(
                    locator
                    for locator in target.window_locators
                    if locator not in window.normalized
                )
                diagnostics.append(f"{window.key} missing locator {missing!r}")
            detail = "; ".join(diagnostics)
        else:
            detail = f"no windows collected; first locator {target.window_locators[0]!r}"
        raise AblationConfigError(
            f"target {target.target_id!r} bound 0 of {len(windows)} windows: {detail}"
        )
    return BindingResult(
        target_id=target.target_id,
        dbs=sorted({window.db for window in matched}),
        windows=[window.key for window in matched],
        multi_match=len(matched) > 1,
        rubric=target.rubric,
    )


def bind_targets(
    targets: Sequence[Target], windows: Sequence[Window]
) -> dict[str, BindingResult]:
    return {target.target_id: _bind_target(target, windows) for target in targets}


# ---------------------------------------------------------------------------
# Copied helpers (scripts stay standalone; do not extract a shared module).
# ---------------------------------------------------------------------------


class ProviderBudgetExhausted(RuntimeError):
    pass


class ProviderBudget:
    # Copied from scripts/ablate_light_time_context.py; scripts stay standalone.
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


def _paired_variant_schedule(
    n_repeats: int,
    variants: Sequence[str],
    budget_remaining: int,
) -> list[tuple[int, str]]:
    # Copied from scripts/ablate_light_time_context.py; scripts stay standalone.
    """Interleaved ``(repeat_idx, variant)`` call plan that fits within
    ``budget_remaining``. Each repeat runs every variant in order before the
    next repeat starts, and the plan stops the moment the next call would
    exceed budget. This keeps the conditions paired under a tight cap: a budget
    smaller than ``repeats * variants`` never starves the later conditions to
    zero while still counting them attempted."""
    plan: list[tuple[int, str]] = []
    remaining = budget_remaining
    for repeat_idx in range(n_repeats):
        for variant in variants:
            if remaining <= 0:
                return plan
            plan.append((repeat_idx, variant))
            remaining -= 1
    return plan


def _drop_out_of_window_candidates(
    candidates: list[FactCandidate],
    rows: list[tuple[int, str | None, Any]],
) -> tuple[list[FactCandidate], int]:
    # Copied from scripts/ablate_light_time_context.py; scripts stay standalone.
    """Mirror production's Light candidate drop (ADR 0031): keep only
    candidates whose source_message_ids sit inside this window's rendered
    evidence ids (``rendered_message_ids``). Out-of-window-cited candidates
    never reach Tier 2 in production, so scoring them here would skew reported
    candidate volume against what actually ships."""
    evidence_ids = rendered_message_ids(rows)
    return keep_candidates_with_valid_source_ids(candidates, evidence_ids)


# ---------------------------------------------------------------------------
# Window collection and answer-session cross-check.
# ---------------------------------------------------------------------------


def _collect_windows(dbs: list[str]) -> list[Window]:
    """Walk every DB's persisted history in ``LIGHT_PHASE_BATCH_SIZE`` batches,
    excluding the ``onboarding:`` prefix exactly as production Light does, and
    collect ALL windows (answer sessions sit mid-DB)."""
    windows: list[Window] = []
    for db in dbs:
        after_id = 0
        index = 0
        while True:
            rows = load_messages_since(
                db,
                after_id,
                limit=LIGHT_PHASE_BATCH_SIZE,
                exclude_session_prefixes=("onboarding:",),
            )
            if not rows:
                break
            transcript = render_transcript(rows)
            windows.append(
                Window(
                    db=db,
                    key=f"{db}#w{index}",
                    rows=rows,
                    transcript=transcript,
                    normalized=normalize(transcript),
                )
            )
            after_id = max(message_id for message_id, _, _ in rows)
            index += 1
    return windows


def _window_session_crosscheck(window: Window) -> tuple[list[str], bool]:
    """Read-only, informational cross-check (NOT the binding mechanism): the
    distinct session ids spanned by this window's message-id range, and whether
    any is an answer session (``:answer_`` in the id)."""
    message_ids = [message_id for message_id, _, _ in window.rows]
    if not message_ids:
        return [], False
    low, high = min(message_ids), max(message_ids)
    with sqlite3.connect(f"file:{window.db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM messages WHERE id BETWEEN ? AND ?",
            (low, high),
        ).fetchall()
    sessions = sorted(str(row[0]) for row in rows)
    contains_answer = any(":answer_" in session for session in sessions)
    return sessions, contains_answer


# ---------------------------------------------------------------------------
# Live runner (not exercised by tests).
# ---------------------------------------------------------------------------


def _build_condition_agents(model_group: str) -> dict[str, Any]:
    agents: dict[str, Any] = {}
    for condition in CONDITIONS:
        if condition == "control":
            agents[condition] = build_extraction_agent(model_group)
        else:
            agents[condition] = _build_agent(
                model_group,
                list[FactCandidate],
                build_condition_instructions(condition),
                default_max_tokens=EXTRACTION_MAX_OUTPUT_TOKENS,
            )
    return agents


async def _run_agent(
    agent: Any, transcript: str, budget: ProviderBudget
) -> tuple[list[FactCandidate], Any]:
    budget.take()
    result = await agent.run(transcript)
    return list(result.output), result.usage()


async def _run_ablation(args: argparse.Namespace) -> dict[str, Any]:
    windows = _collect_windows(args.db)
    bindings = bind_targets(TARGETS, windows)
    bound_keys = {key for binding in bindings.values() for key in binding.windows}
    targets_by_window: dict[str, list[str]] = defaultdict(list)
    for binding in bindings.values():
        for key in binding.windows:
            targets_by_window[key].append(binding.target_id)

    agents = _build_condition_agents(args.model_group)
    budget = ProviderBudget(args.max_provider_calls)

    candidate_records: list[dict[str, Any]] = []
    call_records: list[dict[str, Any]] = []
    audit_records: list[dict[str, Any]] = []
    # attempts[condition][window_key] -> list of repeat indices actually run.
    attempts: dict[str, dict[str, list[int]]] = {c: defaultdict(list) for c in CONDITIONS}
    budget_exhausted = False

    for window in windows:
        if window.key not in bound_keys:
            continue  # Extraction runs only on bound windows.
        sessions_spanned, contains_answer_session = _window_session_crosscheck(window)
        audit_records.append(
            {
                "record_type": "window_transcript_hash",
                "window": window.key,
                "db": window.db,
                "target_ids": targets_by_window[window.key],
                "message_count": len(window.rows),
                "message_id_range": [window.rows[0][0], window.rows[-1][0]],
                "transcript_sha256": hashlib.sha256(
                    window.transcript.encode()
                ).hexdigest(),
                "sessions_spanned": sessions_spanned,
                "contains_answer_session": contains_answer_session,
            }
        )
        if budget_exhausted:
            continue
        plan = _paired_variant_schedule(args.repeats, CONDITIONS, budget.remaining())
        if len(plan) < args.repeats * len(CONDITIONS):
            budget_exhausted = True
        for repeat_idx, condition in plan:
            attempts[condition][window.key].append(repeat_idx)
            raw_candidates, usage = await _run_agent(
                agents[condition], window.transcript, budget
            )
            raw_count = len(raw_candidates)
            kept_candidates, dropped = _drop_out_of_window_candidates(
                raw_candidates, window.rows
            )
            input_tokens = getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            call_records.append(
                {
                    "record_type": "call",
                    "condition": condition,
                    "repeat": repeat_idx,
                    "window": window.key,
                    "db": window.db,
                    "kept": len(kept_candidates),
                    "raw": raw_count,
                    "dropped": dropped,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            )
            # Production parity: run_light_phase applies the occurred_at
            # guards before persisting, so the audit records both the model's
            # raw value and what would actually reach Tier 2. Hit scoring uses
            # fact_text only; the guards never touch it.
            guarded_candidates = [
                candidate.model_copy(deep=True) for candidate in kept_candidates
            ]
            apply_occurred_at_guards(guarded_candidates, window.rows, window.transcript)
            for candidate, guarded in zip(
                kept_candidates, guarded_candidates, strict=True
            ):
                record = {
                    "condition": condition,
                    "repeat": repeat_idx,
                    "window": window.key,
                    "db": window.db,
                    "target_ids": targets_by_window[window.key],
                    "fact_text": candidate.fact_text,
                    "category": candidate.category,
                    "occurred_at_raw": candidate.occurred_at,
                    "occurred_at_guarded": guarded.occurred_at,
                    "source_message_ids": list(candidate.source_message_ids),
                }
                candidate_records.append(record)
                audit_records.append({"record_type": "candidate", **record})

    # Normalize the defaultdicts to plain dicts for the metrics builder.
    attempts_plain = {
        condition: {key: list(reps) for key, reps in per_window.items()}
        for condition, per_window in attempts.items()
    }
    metrics_document = _build_metrics_document(
        args=args,
        bindings=bindings,
        candidate_records=candidate_records,
        call_records=call_records,
        attempts=attempts_plain,
        budget=budget,
        budget_exhausted=budget_exhausted,
    )
    return {"metrics_document": metrics_document, "audit_records": audit_records}


# ---------------------------------------------------------------------------
# Pure metrics builder (unit-tested; fed synthetic records).
# ---------------------------------------------------------------------------


def _stats(values: list[float]) -> dict[str, Any]:
    """Total/mean/stdev/min/max over cell values, empty-denominator -> None."""
    if not values:
        return {"total": None, "mean": None, "stdev": None, "min": None, "max": None, "cells": 0}
    return {
        "total": sum(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if len(values) >= 2 else None,
        "min": min(values),
        "max": max(values),
        "cells": len(values),
    }


def _build_metrics_document(
    *,
    args: argparse.Namespace,
    bindings: dict[str, BindingResult],
    candidate_records: list[dict[str, Any]],
    call_records: list[dict[str, Any]],
    attempts: dict[str, dict[str, list[int]]],
    budget: ProviderBudget,
    budget_exhausted: bool,
) -> dict[str, Any]:
    conditions_doc: dict[str, Any] = {}
    for condition in CONDITIONS:
        cond_cands = [r for r in candidate_records if r["condition"] == condition]
        cond_calls = [r for r in call_records if r["condition"] == condition]
        cond_attempts = attempts.get(condition, {})

        # --- per-target hit accounting -------------------------------------
        per_target: dict[str, Any] = {}
        # per_repeat_hits_by_target[target][repeat] -> True/False/None
        per_repeat_hits_by_target: dict[str, list[bool | None]] = {}
        for target_id, binding in bindings.items():
            # A repeat counts as attempted for a target only when EVERY bound
            # window ran it (intersection, not union): a budget-truncated
            # panel is incomplete evidence and must score null, never a false
            # miss from the windows that happened to run.
            attempted_repeats: set[int] | None = None
            for window_key in binding.windows:
                window_repeats = set(cond_attempts.get(window_key, []))
                attempted_repeats = (
                    window_repeats
                    if attempted_repeats is None
                    else attempted_repeats & window_repeats
                )
            attempted_repeats = attempted_repeats or set()
            per_repeat_hits: list[bool | None] = []
            for repeat in range(args.repeats):
                if repeat not in attempted_repeats:
                    per_repeat_hits.append(None)
                    continue
                texts = [
                    r["fact_text"]
                    for r in cond_cands
                    if r["repeat"] == repeat and r["window"] in binding.windows
                ]
                per_repeat_hits.append(rubric_hit(texts, binding.rubric))
            per_repeat_hits_by_target[target_id] = per_repeat_hits
            hits = sum(1 for hit in per_repeat_hits if hit is True)
            repeats_attempted = sum(1 for hit in per_repeat_hits if hit is not None)
            per_target[target_id] = {
                "per_repeat_hits": per_repeat_hits,
                "hits": hits,
                "repeats_attempted": repeats_attempted,
                "hit_rate": (hits / repeats_attempted) if repeats_attempted else None,
                "multi_match": binding.multi_match,
            }

        # --- overall hit rate: per-repeat fraction of attempted targets hit --
        per_repeat_fraction: list[float | None] = []
        for repeat in range(args.repeats):
            attempted = [
                target_id
                for target_id, hits in per_repeat_hits_by_target.items()
                if hits[repeat] is not None
            ]
            if not attempted:
                per_repeat_fraction.append(None)
                continue
            hit_here = sum(
                1 for target_id in attempted if per_repeat_hits_by_target[target_id][repeat] is True
            )
            per_repeat_fraction.append(hit_here / len(attempted))
        present = [value for value in per_repeat_fraction if value is not None]
        overall_hit_rate = {
            "per_repeat": per_repeat_fraction,
            "mean": statistics.fmean(present) if present else None,
            "stdev": statistics.stdev(present) if len(present) >= 2 else None,
            "repeats_with_data": len(present),
        }

        # --- candidate volume over (window, repeat) cells ------------------
        kept_values = [float(r["kept"]) for r in cond_calls]
        raw_values = [float(r["raw"]) for r in cond_calls]
        candidate_volume = {
            "kept": _stats(kept_values),
            "raw": _stats(raw_values),
            "dropped_out_of_window_total": sum(r["dropped"] for r in cond_calls),
        }

        # --- token cost ----------------------------------------------------
        # Per-field accounting: a call reporting only one side of usage must
        # not skew the other side's mean, so each field carries its own
        # denominator, and a field nobody reported is None, never 0.
        input_values = [
            r["input_tokens"] for r in cond_calls if r.get("input_tokens") is not None
        ]
        output_values = [
            r["output_tokens"] for r in cond_calls if r.get("output_tokens") is not None
        ]
        tokens = {
            "calls_total": len(cond_calls),
            "calls_with_input": len(input_values),
            "calls_with_output": len(output_values),
            "input_total": sum(input_values) if input_values else None,
            "output_total": sum(output_values) if output_values else None,
            "input_mean_per_call": statistics.fmean(input_values)
            if input_values
            else None,
            "output_mean_per_call": statistics.fmean(output_values)
            if output_values
            else None,
        }

        conditions_doc[condition] = {
            "per_target": per_target,
            "overall_hit_rate": overall_hit_rate,
            "candidate_volume": candidate_volume,
            "tokens": tokens,
        }

    return {
        "dbs": list(args.db),
        "model_group": args.model_group,
        "caps": {
            "repeats": args.repeats,
            "max_provider_calls": args.max_provider_calls,
        },
        "provider_calls_used": budget.used,
        "budget_exhausted": budget_exhausted,
        "bindings": {
            target_id: {
                "dbs": binding.dbs,
                "windows": binding.windows,
                "multi_match": binding.multi_match,
                "rubric": [list(group) for group in binding.rubric],
            }
            for target_id, binding in bindings.items()
        },
        "conditions": conditions_doc,
    }


# ---------------------------------------------------------------------------
# Artifacts and CLI.
# ---------------------------------------------------------------------------


def _write_artifacts(out_dir: Path, result: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ablation_metrics.json").write_text(
        json.dumps(result["metrics_document"], indent=2, sort_keys=True) + "\n"
    )
    with (out_dir / "ablation_audit.jsonl").open("w", encoding="utf-8") as handle:
        for record in result["audit_records"]:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _print_binding_table(bindings: dict[str, BindingResult]) -> None:
    print("target      multi  db / window keys")
    print("----------  -----  -------------------------------------------")
    for target_id, binding in bindings.items():
        flag = "yes" if binding.multi_match else "no"
        print(f"{target_id:<10}  {flag:<5}  {', '.join(binding.windows)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the opt-in live extraction-prompt ablation."
    )
    parser.add_argument("--db", action="append", default=[])
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--bind-only", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--max-provider-calls", type=int, default=140)
    parser.add_argument("--out")
    parser.add_argument("--model-group", default="extraction")
    return parser


def _require(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise AblationConfigError(f"{name} is required with --allow-live.")
    return value


def _validate_dbs(dbs: list[str]) -> None:
    if not dbs:
        raise AblationConfigError("--db is required (repeatable).")
    for db in dbs:
        if not Path(db).exists():
            raise AblationConfigError(f"--db not found: {db}")


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("repeats", "max_provider_calls"):
        if getattr(args, name) <= 0:
            raise AblationConfigError(f"--{name.replace('_', '-')} must be greater than 0.")
    _validate_dbs(args.db)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.bind_only:
        # Deterministic collection + binding only: no provider calls, no
        # artifacts, works without --allow-live. Exit 2 on a binding failure.
        try:
            _validate_dbs(args.db)
            windows = _collect_windows(args.db)
            bindings = bind_targets(TARGETS, windows)
        except AblationConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        _print_binding_table(bindings)
        return 0

    if not args.allow_live:
        print("Extraction-prompt ablation skipped; pass --allow-live to run provider calls.")
        return 0

    try:
        _validate_args(args)
        out_dir = Path(_require(args.out, "--out"))
        result = asyncio.run(_run_ablation(args))
        _write_artifacts(out_dir, result)
    except AblationConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"Extraction-prompt ablation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
