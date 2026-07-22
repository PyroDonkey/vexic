"""Tests for the Deep-backlog replay harness.

Fully deterministic: the harness makes no provider calls, so every path is
exercised directly against synthetic run DBs. The seeding scaffolding extends
the pattern in tests/test_simulate_mentioned_at_promotion.py with controllable
``dream_runs``, ``long_term_memory`` and ``memory_dedup_events`` rows.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import TestCase

from vexic.storage.schema import (
    EMBEDDING_DIM,
    _ensure_vector_memory_schema,
    _serialize_float32,
    init_vector_memory,
)
from vexic.storage.connection import connect as storage_connect
from vexic.longmemeval import _question_path_component

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "replay_deep_backlog.py"


def _load_module() -> ModuleType:
    """Load the script by path: scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("replay_deep_backlog", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replay = _load_module()


class _RunFixture(TestCase):
    """Shared synthetic-run scaffolding with dream_runs / facts / dedup rows."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._db_index = 0

    def _new_db(self) -> Path:
        self._db_index += 1
        db_path = self.root / f"memory_{self._db_index}.db"
        init_vector_memory(str(db_path))
        return db_path

    def _seed(
        self,
        *,
        dream_runs: list[dict] | None = None,
        facts: list[dict] | None = None,
        dedup_events: list[dict] | None = None,
        candidates: list[dict] | None = None,
        messages: dict[int, str] | None = None,
        db_path: Path | None = None,
    ) -> Path:
        """Seed a fresh DB.

        ``dream_runs`` rows accept any dream_runs column; missing counters
        default to 0, ``status`` to 'ok', ``agent_id`` to NULL, and
        ``started_at``/``finished_at`` to a monotonic per-row second stamp.
        ``facts`` rows carry id/promoted_from_candidate_id/created_at/retired.
        ``candidates`` rows carry id/category/importance/created_at/
        source_message_ids/occurred_at/mentioned_at/flags/embed; an embedding
        row is written unless ``embed`` is False. ``messages`` maps message id
        to timestamp string, the rows a per-cycle ``mentioned_at`` derives from.
        """
        if db_path is None:
            db_path = self._new_db()
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            init_vector_memory(str(db_path))
        embedding = _serialize_float32([1.0] + [0.0] * (EMBEDDING_DIM - 1))
        with closing(storage_connect(db_path)) as conn:
            _ensure_vector_memory_schema(conn)
            for message_id, timestamp in (messages or {}).items():
                conn.execute(
                    """
                    INSERT INTO messages (id, session_id, agent_id, timestamp, message_json)
                    VALUES (?, 'session', NULL, ?, '{}')
                    """,
                    (message_id, timestamp),
                )
            for index, candidate in enumerate(candidates or [], start=1):
                conn.execute(
                    """
                    INSERT INTO memory_candidates (
                        id, fact_text, subject, category, importance, confidence,
                        source_message_ids, agent_id, hit_count, rem_boost,
                        occurred_at, mentioned_at, last_seen_at, created_at,
                        promoted, promoted_fact_id, retired, stale, needs_review
                    ) VALUES (?, ?, 'user', ?, ?, 0.9, ?, ?, 1, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.get("id", index),
                        candidate.get("fact_text", f"Candidate {index}."),
                        candidate.get("category", "event"),
                        candidate.get("importance", 5),
                        json.dumps(candidate.get("source_message_ids", [1])),
                        candidate.get("agent_id"),
                        candidate.get("rem_boost", 0.0),
                        candidate.get("occurred_at"),
                        candidate.get("mentioned_at"),
                        candidate.get("last_seen_at", "2023-06-01 00:00:00"),
                        candidate.get("created_at", "2023-06-01 00:00:00"),
                        candidate.get("promoted", 0),
                        candidate.get("promoted_fact_id"),
                        candidate.get("retired", 0),
                        candidate.get("stale", 0),
                        candidate.get("needs_review", 0),
                    ),
                )
                if candidate.get("embed", True):
                    conn.execute(
                        """
                        INSERT INTO memory_candidate_embeddings (candidate_id, embedding)
                        VALUES (?, ?)
                        """,
                        (candidate.get("id", index), embedding),
                    )
            for index, run in enumerate(dream_runs or [], start=1):
                stamp = f"2023-06-01 00:00:{index:02d}"
                conn.execute(
                    """
                    INSERT INTO dream_runs (
                        id, started_at, finished_at, status, agent_id,
                        messages_processed, candidates_inserted, candidates_merged,
                        candidates_review, candidates_boosted, last_processed_message_id,
                        promotions, retirements
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.get("id", index),
                        run.get("started_at", stamp),
                        run.get("finished_at", run.get("started_at", stamp)),
                        run.get("status", "ok"),
                        run.get("agent_id"),
                        run.get("candidates_inserted", 0),
                        run.get("candidates_merged", 0),
                        run.get("candidates_review", 0),
                        run.get("candidates_boosted", 0),
                        run.get("last_processed_message_id", 0),
                        run.get("promotions", 0),
                        run.get("retirements", 0),
                    ),
                )
            for fact in facts or []:
                conn.execute(
                    """
                    INSERT INTO long_term_memory (
                        id, fact_text, subject, category, importance, confidence,
                        source_message_ids, promoted_from_candidate_id, created_at,
                        occurred_at, mentioned_at, retired
                    ) VALUES (?, ?, 'user', ?, 5, 0.9, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        fact["id"],
                        fact.get("fact_text", "A fact."),
                        fact.get("category", "event"),
                        json.dumps(fact.get("source_message_ids", [1])),
                        fact.get("promoted_from_candidate_id", fact["id"]),
                        fact.get("created_at"),
                        fact.get("occurred_at", "2023-01-01"),
                        fact.get("retired", 0),
                    ),
                )
            for event in dedup_events or []:
                conn.execute(
                    """
                    INSERT INTO memory_dedup_events (
                        id, created_at, candidate_id, matched_candidate_id,
                        best_similarity, decision, incoming_fact_text,
                        incoming_source_message_ids
                    ) VALUES (?, ?, ?, ?, ?, ?, '', '[]')
                    """,
                    (
                        event["id"],
                        event.get("created_at"),
                        event["candidate_id"],
                        event.get("matched_candidate_id"),
                        event.get("best_similarity"),
                        event.get("decision", "merge"),
                    ),
                )
            conn.commit()
        return db_path


class DreamCycleTimelineTests(_RunFixture):
    def test_watermark_only_row_is_classified_light(self) -> None:
        # A watermark-only Light row (inserted=0) still counts as Light under
        # the OR-semantics classifier, and produces no Deep cycle.
        db_path = self._seed(
            dream_runs=[{"last_processed_message_id": 5, "candidates_inserted": 0}]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual(timeline.deep_cycles, [])
        self.assertEqual([run.watermark for run in timeline.light_runs], [5])

    def test_realistic_batch_signature_classification(self) -> None:
        # Light-to-fixpoint -> REM (boosted>0) -> Deep (promotions>0): one Deep
        # cycle, its counters read from the Deep row, phase not inferred.
        db_path = self._seed(
            dream_runs=[
                {"last_processed_message_id": 3, "candidates_inserted": 2},
                {"last_processed_message_id": 6, "candidates_inserted": 1},
                {"candidates_boosted": 4},
                {"promotions": 9, "retirements": 1},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual(len(timeline.deep_cycles), 1)
        cycle = timeline.deep_cycles[0]
        self.assertEqual(cycle.run_id, 4)
        self.assertEqual(cycle.promotions, 9)
        self.assertEqual(cycle.retirements, 1)
        self.assertFalse(cycle.phase_inferred)
        self.assertEqual([run.watermark for run in timeline.light_runs], [3, 6])

    def test_all_zero_rem_deep_pair_uses_positional_fallback(self) -> None:
        # A cycle that boosted and promoted nothing: both tail rows are all-zero
        # and classify ambiguous. Positional REM-before-Deep resolves them and
        # the resulting Deep cycle is flagged phase_inferred.
        db_path = self._seed(
            dream_runs=[
                {"last_processed_message_id": 4, "candidates_inserted": 2},
                {"id": 8},
                {"id": 9},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual(len(timeline.deep_cycles), 1)
        cycle = timeline.deep_cycles[0]
        self.assertEqual(cycle.run_id, 9)
        self.assertEqual(cycle.promotions, 0)
        self.assertTrue(cycle.phase_inferred)

    def test_no_op_light_row_does_not_steal_the_rem_slot(self) -> None:
        # A superseded no-op Light run (all-zero, watermark 0) sits ahead of a
        # clearly-classified REM and Deep. Chosen behavior: the all-zero row is
        # a leftover no-op Light, so the REM slot stays with the boosted row and
        # the Deep cycle keeps its real, non-inferred counters.
        db_path = self._seed(
            dream_runs=[
                {"id": 1},
                {"id": 2, "candidates_boosted": 3},
                {"id": 3, "promotions": 7},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual(len(timeline.deep_cycles), 1)
        cycle = timeline.deep_cycles[0]
        self.assertEqual(cycle.run_id, 3)
        self.assertEqual(cycle.promotions, 7)
        self.assertFalse(cycle.phase_inferred)

    def test_rem_without_deep_trailing_group_yields_no_phantom_cycle(self) -> None:
        # REM-without-Deep dream mode ends the stream on a boosted REM row with
        # no Deep after it. That trailing REM must not be misread as a Deep.
        db_path = self._seed(
            dream_runs=[
                {"id": 1, "last_processed_message_id": 2, "candidates_inserted": 1},
                {"id": 2, "candidates_boosted": 3},
                {"id": 3, "promotions": 5},
                {"id": 4, "last_processed_message_id": 4, "candidates_inserted": 1},
                {"id": 5, "candidates_boosted": 2},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual([cycle.run_id for cycle in timeline.deep_cycles], [3])

    def test_consecutive_deep_rows_do_not_collapse(self) -> None:
        # Two clearly-Deep rows in a row: the earlier must not be consumed as the
        # later's REM slot. A clearly-Deep earlier row emits its own DeepCycle, so
        # the pair yields two cycles with both run_ids present.
        db_path = self._seed(
            dream_runs=[
                {"id": 1, "promotions": 3},
                {"id": 2, "promotions": 5},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual([cycle.run_id for cycle in timeline.deep_cycles], [1, 2])
        self.assertEqual(
            [cycle.promotions for cycle in timeline.deep_cycles], [3, 5]
        )

    def test_error_rows_are_excluded(self) -> None:
        # A status='error' Deep row is a failed cycle and must not appear. It is
        # dropped before classification, so the surviving REM has no Deep pair.
        db_path = self._seed(
            dream_runs=[
                {"id": 1, "last_processed_message_id": 2, "candidates_inserted": 1},
                {"id": 2, "candidates_boosted": 3},
                {"id": 3, "promotions": 4, "status": "error"},
                {"id": 4, "last_processed_message_id": 5, "candidates_inserted": 1},
                {"id": 5, "candidates_boosted": 2},
                {"id": 6, "promotions": 8},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual([cycle.run_id for cycle in timeline.deep_cycles], [6])

    def test_agent_scoped_rows_are_excluded(self) -> None:
        # Production Deep does not span agent scopes; an agent_id-bearing row is
        # a different scope and must be filtered out before reconstruction.
        db_path = self._seed(
            dream_runs=[
                {"id": 1, "last_processed_message_id": 2, "candidates_inserted": 1},
                {"id": 2, "candidates_boosted": 3},
                {"id": 3, "promotions": 6, "agent_id": "agent-a"},
                {"id": 4, "promotions": 9},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual([cycle.run_id for cycle in timeline.deep_cycles], [4])

    def test_non_increasing_watermarks_are_dropped_from_light_runs(self) -> None:
        # Only strictly-increasing watermarks advance the transcript; a stalled
        # or regressed Light row (same or lower watermark) is dropped.
        db_path = self._seed(
            dream_runs=[
                {"id": 1, "last_processed_message_id": 4, "candidates_inserted": 1},
                {"id": 2, "last_processed_message_id": 4, "candidates_inserted": 1},
                {"id": 3, "last_processed_message_id": 2, "candidates_inserted": 1},
                {"id": 4, "last_processed_message_id": 9, "candidates_inserted": 1},
            ]
        )

        timeline = replay._dream_cycle_timeline(db_path)

        self.assertEqual(
            [(run.run_id, run.watermark) for run in timeline.light_runs],
            [(1, 4), (4, 9)],
        )


def _deep_cycle(
    run_id: int,
    started_at: str,
    finished_at: str | None,
    *,
    promotions: int = 1,
    retirements: int = 0,
) -> "replay.DeepCycle":
    return replay.DeepCycle(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        promotions=promotions,
        retirements=retirements,
        phase_inferred=False,
    )


class PromotionEventsTests(_RunFixture):
    def test_in_interval_fact_attributes_to_its_cycle(self) -> None:
        # A fact created between a Deep run's started_at and finished_at is
        # attributed to that cycle by candidate id.
        db_path = self._seed(
            facts=[
                {
                    "id": 70,
                    "promoted_from_candidate_id": 12,
                    "created_at": "2023-06-01 12:00:05",
                }
            ]
        )
        cycles = [
            _deep_cycle(3, "2023-06-01 12:00:00", "2023-06-01 12:00:10"),
        ]

        attribution = replay._promotion_events(db_path, cycles)

        self.assertEqual(attribution.by_candidate, {12: 0})
        self.assertEqual(attribution.per_cycle_counts, [1])
        self.assertEqual(attribution.unattributed, [])
        self.assertTrue(attribution.attribution_consistent)

    def test_second_truncated_boundary_fact_still_attributes(self) -> None:
        # The run's started_at carries microseconds (12:00:05.900000) but the
        # fact created_at is second-truncated to the same second (12:00:05). A
        # naive comparison would place the fact before the run; flooring the
        # interval start to the second keeps it inside.
        db_path = self._seed(
            facts=[
                {
                    "id": 70,
                    "promoted_from_candidate_id": 12,
                    "created_at": "2023-06-01 12:00:05",
                }
            ]
        )
        cycles = [
            _deep_cycle(3, "2023-06-01 12:00:05.900000", "2023-06-01 12:00:08.100000"),
        ]

        attribution = replay._promotion_events(db_path, cycles)

        self.assertEqual(attribution.by_candidate, {12: 0})
        self.assertTrue(attribution.attribution_consistent)

    def test_pre_first_cycle_fact_is_unattributed_and_inconsistent(self) -> None:
        # A promoted fact created before the first Deep cycle's start cannot be
        # attributed: Deep is the only long_term_memory writer, so a fact ahead
        # of the first cycle signals corruption. It lands in unattributed and
        # breaks attribution_consistent.
        db_path = self._seed(
            facts=[
                {
                    "id": 70,
                    "promoted_from_candidate_id": 12,
                    "created_at": "2023-06-01 06:00:00",
                }
            ]
        )
        cycles = [
            _deep_cycle(3, "2023-06-01 12:00:00", "2023-06-01 12:00:10"),
        ]

        attribution = replay._promotion_events(db_path, cycles)

        self.assertEqual(attribution.by_candidate, {})
        self.assertEqual(attribution.per_cycle_counts, [0])
        self.assertEqual(attribution.unattributed, [12])
        self.assertFalse(attribution.attribution_consistent)

    def test_delayed_commit_after_finished_attributes_to_that_cycle(self) -> None:
        # commit_deep_cycle persists facts after the run's finished_at is
        # captured, so a delayed write can land past ceil(finished_at). The
        # interval for cycle k is [floor(started_at_k), floor(started_at_{k+1})),
        # so a fact created 3s after cycle 0's finished_at but before cycle 1
        # starts still attributes to cycle 0.
        db_path = self._seed(
            facts=[
                {"id": 1, "promoted_from_candidate_id": 11, "created_at": "2023-06-01 12:00:13"},
                {"id": 2, "promoted_from_candidate_id": 13, "created_at": "2023-06-01 13:00:04"},
            ]
        )
        cycles = [
            _deep_cycle(3, "2023-06-01 12:00:00", "2023-06-01 12:00:10", promotions=1),
            _deep_cycle(6, "2023-06-01 13:00:00", "2023-06-01 13:00:10", promotions=1),
        ]

        attribution = replay._promotion_events(db_path, cycles)

        self.assertEqual(attribution.by_candidate, {11: 0, 13: 1})
        self.assertEqual(attribution.per_cycle_counts, [1, 1])
        self.assertEqual(attribution.unattributed, [])
        self.assertTrue(attribution.attribution_consistent)

    def test_multi_cycle_counts_cross_check_passes(self) -> None:
        # Two Deep cycles, three facts split across them. Each cycle's attributed
        # count matches its recorded promotions, so the cross-check passes.
        db_path = self._seed(
            facts=[
                {"id": 1, "promoted_from_candidate_id": 11, "created_at": "2023-06-01 12:00:03"},
                {"id": 2, "promoted_from_candidate_id": 12, "created_at": "2023-06-01 12:00:07"},
                {"id": 3, "promoted_from_candidate_id": 13, "created_at": "2023-06-01 13:00:04"},
            ]
        )
        cycles = [
            _deep_cycle(3, "2023-06-01 12:00:00", "2023-06-01 12:00:10", promotions=2),
            _deep_cycle(6, "2023-06-01 13:00:00", "2023-06-01 13:00:10", promotions=1),
        ]

        attribution = replay._promotion_events(db_path, cycles)

        self.assertEqual(attribution.by_candidate, {11: 0, 12: 0, 13: 1})
        self.assertEqual(attribution.per_cycle_counts, [2, 1])
        self.assertTrue(attribution.attribution_consistent)

    def test_count_mismatch_against_promotions_is_inconsistent(self) -> None:
        # Every fact attributes and unattributed is empty, but the cycle records
        # more promotions than facts landed in its interval: the per-cycle count
        # cross-check against dream_runs.promotions still fails.
        db_path = self._seed(
            facts=[
                {"id": 1, "promoted_from_candidate_id": 11, "created_at": "2023-06-01 12:00:03"},
            ]
        )
        cycles = [
            _deep_cycle(3, "2023-06-01 12:00:00", "2023-06-01 12:00:10", promotions=2),
        ]

        attribution = replay._promotion_events(db_path, cycles)

        self.assertEqual(attribution.per_cycle_counts, [1])
        self.assertEqual(attribution.unattributed, [])
        self.assertFalse(attribution.attribution_consistent)

    def test_retired_fact_still_attributes(self) -> None:
        # Supersession retires facts in place without deleting them (Invariant
        # 6). A retired fact was still promoted in its Deep cycle, so it must be
        # attributed exactly like a live one.
        db_path = self._seed(
            facts=[
                {
                    "id": 70,
                    "promoted_from_candidate_id": 12,
                    "created_at": "2023-06-01 12:00:05",
                    "retired": 1,
                }
            ]
        )
        cycles = [
            _deep_cycle(3, "2023-06-01 12:00:00", "2023-06-01 12:00:10"),
        ]

        attribution = replay._promotion_events(db_path, cycles)

        self.assertEqual(attribution.by_candidate, {12: 0})
        self.assertEqual(attribution.per_cycle_counts, [1])
        self.assertTrue(attribution.attribution_consistent)


class CandidateStateTests(_RunFixture):
    def _state_at(
        self,
        db_path: Path,
        candidate_id: int,
        started_at: str,
        *,
        cycle_index: int = 0,
        promoted: dict[int, int] | None = None,
    ) -> "replay.CandidateState | None":
        """Load one DB and reconstruct candidate ``candidate_id`` at ``T``."""
        with replay._connection(db_path) as conn:
            candidates = {
                candidate.candidate_id: candidate
                for candidate in replay._load_replay_candidates(conn)
            }
            merge_events = replay._load_merge_events(conn)
            message_times = replay._load_message_times(conn)
        return replay._candidate_state_at(
            candidates[candidate_id],
            replay._timestamp_datetime(started_at),
            cycle_index=cycle_index,
            merge_events=merge_events.get(candidate_id, []),
            message_times=message_times,
            promoted_at_cycle=(promoted or {}).get(candidate_id),
        )

    def test_unmerged_candidate_reconstructs_exact_state(self) -> None:
        # No merge events: hit_count is 1 and last_seen_at is the candidate's
        # own created_at, and mentioned_at derives from its one source message.
        db_path = self._seed(
            candidates=[
                {
                    "id": 5,
                    "created_at": "2023-06-01 08:00:00",
                    "source_message_ids": [1],
                }
            ],
            messages={1: "2023-03-15 09:30:00"},
        )

        state = self._state_at(db_path, 5, "2023-06-10 00:00:00")

        assert state is not None
        self.assertEqual(state.hit_count, 1)
        self.assertEqual(
            state.last_seen_at, replay._timestamp_datetime("2023-06-01 08:00:00")
        )
        self.assertEqual(state.mentioned_at, "2023-03-15")

    def test_merge_events_step_hit_count_and_last_seen_across_cycles(self) -> None:
        # Two merge events land after creation. Scored at three cycle times, the
        # hit_count steps 1 -> 2 -> 3 and last_seen_at follows the latest merge
        # that had occurred by each cycle's started_at.
        db_path = self._seed(
            candidates=[
                {
                    "id": 5,
                    "created_at": "2023-06-01 08:00:00",
                    "source_message_ids": [1],
                }
            ],
            messages={1: "2023-03-15 09:30:00"},
            dedup_events=[
                {"id": 1, "candidate_id": 5, "created_at": "2023-06-02 10:00:00"},
                {"id": 2, "candidate_id": 5, "created_at": "2023-06-03 11:00:00"},
            ],
        )

        before = self._state_at(db_path, 5, "2023-06-01 09:00:00")
        after_one = self._state_at(db_path, 5, "2023-06-02 12:00:00")
        after_two = self._state_at(db_path, 5, "2023-06-04 00:00:00")

        assert before is not None and after_one is not None and after_two is not None
        self.assertEqual(before.hit_count, 1)
        self.assertEqual(
            before.last_seen_at, replay._timestamp_datetime("2023-06-01 08:00:00")
        )
        self.assertEqual(after_one.hit_count, 2)
        self.assertEqual(
            after_one.last_seen_at, replay._timestamp_datetime("2023-06-02 10:00:00")
        )
        self.assertEqual(after_two.hit_count, 3)
        self.assertEqual(
            after_two.last_seen_at, replay._timestamp_datetime("2023-06-03 11:00:00")
        )

    def test_candidate_created_after_cycle_is_absent(self) -> None:
        # A candidate whose created_at is after the cycle's started_at did not
        # exist yet at that cycle, so it is not in the pool.
        db_path = self._seed(
            candidates=[{"id": 5, "created_at": "2023-06-10 08:00:00"}],
            messages={1: "2023-03-15 09:30:00"},
        )

        state = self._state_at(db_path, 5, "2023-06-01 00:00:00")

        self.assertIsNone(state)

    def test_candidate_promoted_earlier_is_absent_from_later_pool(self) -> None:
        # A candidate promoted in an earlier Deep cycle (attribution input) has
        # left Tier 2 and must not reappear in a later cycle's pool.
        db_path = self._seed(
            candidates=[{"id": 5, "created_at": "2023-06-01 08:00:00"}],
            messages={1: "2023-03-15 09:30:00"},
        )

        promoted = {5: 0}
        at_promotion = self._state_at(
            db_path, 5, "2023-06-02 00:00:00", cycle_index=0, promoted=promoted
        )
        later = self._state_at(
            db_path, 5, "2023-06-03 00:00:00", cycle_index=2, promoted=promoted
        )

        self.assertIsNotNone(at_promotion)
        self.assertIsNone(later)

    def test_non_isoformat_message_stamp_is_skipped(self) -> None:
        # Parser parity with production _earliest_date_from_timestamps: only
        # datetime.fromisoformat is accepted (no permissive LongMemEval
        # "%Y/%m/%d %H:%M" fallback). A candidate whose only source carries a
        # slash-dated stamp stays undated rather than healing off a format
        # production would never accept.
        db_path = self._seed(
            candidates=[
                {
                    "id": 5,
                    "created_at": "2023-06-01 08:00:00",
                    "source_message_ids": [1],
                }
            ],
            messages={1: "2023/03/15 09:30"},
        )

        state = self._state_at(db_path, 5, "2023-06-10 00:00:00")

        assert state is not None
        self.assertIsNone(state.mentioned_at)

    def test_empty_source_message_ids_yields_none_mentioned_at(self) -> None:
        # A candidate citing no messages has no earliest-mention date to derive;
        # mentioned_at is None rather than raising.
        db_path = self._seed(
            candidates=[
                {
                    "id": 5,
                    "created_at": "2023-06-01 08:00:00",
                    "source_message_ids": [],
                }
            ]
        )

        state = self._state_at(db_path, 5, "2023-06-10 00:00:00")

        assert state is not None
        self.assertIsNone(state.mentioned_at)

    def test_unparseable_created_at_raises_loud(self) -> None:
        # The determinism guard: an unparseable candidate created_at must raise
        # rather than fall back to datetime.now() and go non-deterministic.
        db_path = self._seed(
            candidates=[{"id": 5, "created_at": "not-a-timestamp"}]
        )

        with self.assertRaises(ValueError):
            with replay._connection(db_path) as conn:
                replay._load_replay_candidates(conn)


class ReplayCyclesTests(_RunFixture):
    def test_exact_reconstruction_yields_full_overlap(self) -> None:
        # All candidates hit_count 1, rem_boost 0, distinct importance so the
        # score order is deterministic. The two highest-importance candidates
        # are the seeded actual promotions, so the as-run top-N prediction
        # matches the recorded promotions exactly: prediction_overlap 1.0.
        db_path = self._seed(
            candidates=[
                {"id": 1, "category": "fact", "importance": 9, "created_at": "2023-06-01 00:00:00"},
                {"id": 2, "category": "fact", "importance": 7, "created_at": "2023-06-01 00:00:00"},
                {"id": 3, "category": "fact", "importance": 5, "created_at": "2023-06-01 00:00:00"},
            ],
            messages={1: "2023-03-15 09:30:00"},
            dream_runs=[
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 3},
                {
                    "id": 2,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 2,
                },
            ],
            facts=[
                {"id": 100, "promoted_from_candidate_id": 1, "created_at": "2023-06-05 00:00:05"},
                {"id": 101, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        )

        cycles = replay._replay_cycles(db_path, top_n=2)

        self.assertEqual(len(cycles), 1)
        cycle = cycles[0]
        self.assertEqual(cycle.predicted_as_run_rem_zero, [1, 2])
        self.assertEqual(cycle.prediction_overlap_rem_zero, 1.0)
        self.assertEqual(cycle.prediction_overlap_rem_final, 1.0)

    def test_per_cycle_max_hit_count_does_not_leak_final_state(self) -> None:
        # F6b: candidate 1's hit_count grows to 11 *after* the scored cycle (10
        # merges dated after started_at); at the cycle it is still 1. Candidate 2
        # has one pre-cycle merge, so per-cycle hit_count is 2 and the per-cycle
        # max is 2. With the correct per-cycle normalizer candidate 2 wins the
        # single slot; if the final max (11) leaked into the hit_count norm,
        # candidate 1 would win instead. The order is constructed to flip.
        db_path = self._seed(
            candidates=[
                {"id": 1, "category": "fact", "importance": 8, "created_at": "2023-06-05 00:00:00"},
                {"id": 2, "category": "fact", "importance": 5, "created_at": "2023-06-05 00:00:00"},
            ],
            messages={1: "2023-03-15 09:30:00"},
            dedup_events=(
                [{"id": 1, "candidate_id": 2, "created_at": "2023-06-05 00:00:00"}]
                + [
                    {"id": 100 + n, "candidate_id": 1, "created_at": "2023-06-10 00:00:00"}
                    for n in range(10)
                ]
            ),
            dream_runs=[
                {
                    "id": 1,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 1,
                }
            ],
            facts=[
                {"id": 200, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        )

        cycles = replay._replay_cycles(db_path, top_n=1)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].predicted_as_run_rem_zero, [2])

    def test_undated_event_excluded_as_run_present_healed(self) -> None:
        # Candidate 1 is an undated event (no occurred_at, no stored
        # mentioned_at) citing a dated message. As-run mirrors what production
        # scored -> it is skipped. Healed derives mentioned_at from the message,
        # so it joins the healed pool. Candidate 2 (a dated fact) is in both.
        db_path = self._seed(
            candidates=[
                {
                    "id": 1,
                    "category": "event",
                    "occurred_at": None,
                    "mentioned_at": None,
                    "source_message_ids": [1],
                    "created_at": "2023-06-01 00:00:00",
                },
                {
                    "id": 2,
                    "category": "fact",
                    "created_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                },
            ],
            messages={1: "2023-03-15 09:30:00"},
            dream_runs=[
                {
                    "id": 1,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 1,
                }
            ],
            facts=[
                {"id": 200, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        )

        cycles = replay._replay_cycles(db_path, top_n=5)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].as_run_pool_size, 1)
        self.assertEqual(cycles[0].healed_pool_size, 2)

    def test_inflow_backlog_saturated_series_on_growing_pool(self) -> None:
        # Two Deep cycles. c1..c3 exist by cycle 0; c4,c5 are created between the
        # cycles (inflow at cycle 1). Cycle 0 promotes c1,c2; cycle 1 promotes
        # c4,c5. With top_n 2 both cycles are saturated. c3 is never promoted and
        # stays in the backlog. The pool at cycle 1 drops the earlier-promoted
        # c1,c2 and gains c4,c5.
        db_path = self._seed(
            candidates=[
                {"id": 1, "category": "fact", "created_at": "2023-06-01 00:00:00"},
                {"id": 2, "category": "fact", "created_at": "2023-06-01 00:00:00"},
                {"id": 3, "category": "fact", "created_at": "2023-06-01 00:00:00"},
                {"id": 4, "category": "fact", "created_at": "2023-06-10 00:00:00"},
                {"id": 5, "category": "fact", "created_at": "2023-06-10 00:00:00"},
            ],
            messages={1: "2023-03-15 09:30:00"},
            dream_runs=[
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 3},
                {
                    "id": 2,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 2,
                },
                {"id": 3, "last_processed_message_id": 9, "candidates_inserted": 2},
                {
                    "id": 4,
                    "started_at": "2023-06-15 00:00:00",
                    "finished_at": "2023-06-15 00:00:10",
                    "promotions": 2,
                },
            ],
            facts=[
                {"id": 100, "promoted_from_candidate_id": 1, "created_at": "2023-06-05 00:00:05"},
                {"id": 101, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
                {"id": 102, "promoted_from_candidate_id": 4, "created_at": "2023-06-15 00:00:05"},
                {"id": 103, "promoted_from_candidate_id": 5, "created_at": "2023-06-15 00:00:05"},
            ],
        )

        cycles = replay._replay_cycles(db_path, top_n=2)

        self.assertEqual(len(cycles), 2)
        first, second = cycles
        self.assertEqual(first.healed_pool_size, 3)
        self.assertEqual(first.newly_eligible_inflow, 3)
        self.assertTrue(first.saturated)
        self.assertEqual(first.backlog_after, 1)
        self.assertEqual(second.healed_pool_size, 3)
        self.assertEqual(second.newly_eligible_inflow, 2)
        self.assertTrue(second.saturated)
        self.assertEqual(second.backlog_after, 1)

    def test_late_merged_source_not_healed_before_its_watermark(self) -> None:
        # Anachronism gate: candidate 10's only source (message 5) sits above the
        # early cycle's Light watermark (3) -- it was unioned in by a later merge.
        # At cycle 0 the source is not yet processed, so no mentioned_at is
        # derived and the undated event is excluded from the healed pool. The
        # later cycle's watermark (9) covers message 5, so it heals there.
        db_path = self._seed(
            candidates=[
                {
                    "id": 10,
                    "category": "event",
                    "occurred_at": None,
                    "mentioned_at": None,
                    "source_message_ids": [5],
                    "created_at": "2023-06-01 00:00:00",
                }
            ],
            messages={5: "2023-03-01 00:00:00"},
            dream_runs=[
                {"id": 1, "last_processed_message_id": 3, "candidates_inserted": 1,
                 "started_at": "2023-06-02 00:00:00"},
                {"id": 2, "started_at": "2023-06-05 00:00:00",
                 "finished_at": "2023-06-05 00:00:10", "retirements": 1},
                {"id": 3, "last_processed_message_id": 9, "candidates_inserted": 1,
                 "started_at": "2023-06-10 00:00:00"},
                {"id": 4, "started_at": "2023-06-15 00:00:00",
                 "finished_at": "2023-06-15 00:00:10", "retirements": 1},
            ],
        )

        cycles = replay._replay_cycles(db_path, top_n=5)

        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0].healed_pool_size, 0)
        self.assertEqual(cycles[1].healed_pool_size, 1)

    def test_rem_bracket_produces_two_prediction_sets(self) -> None:
        # rem_boost history is unrecoverable (F6), so each cycle is scored twice.
        # Two candidates tie on every term except rem_boost. The rem_boost=0
        # variant breaks the tie by id (candidate 1); the final-rem variant lets
        # candidate 2's boost win. The single-slot prediction differs by variant.
        db_path = self._seed(
            candidates=[
                {"id": 1, "category": "fact", "importance": 5, "rem_boost": 0.0,
                 "created_at": "2023-06-01 00:00:00"},
                {"id": 2, "category": "fact", "importance": 5, "rem_boost": 0.5,
                 "created_at": "2023-06-01 00:00:00"},
            ],
            messages={1: "2023-03-15 09:30:00"},
            dream_runs=[
                {
                    "id": 1,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 1,
                }
            ],
            facts=[
                {"id": 200, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        )

        cycles = replay._replay_cycles(db_path, top_n=1)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].predicted_as_run_rem_zero, [1])
        self.assertEqual(cycles[0].predicted_as_run_rem_final, [2])
        self.assertNotEqual(
            cycles[0].predicted_as_run_rem_zero,
            cycles[0].predicted_as_run_rem_final,
        )

    def test_multi_agent_db_fails_loud(self) -> None:
        # Production Deep is single-scope; the loaders do not filter agent_id, so
        # a DB carrying any agent-scoped row is rejected loudly rather than
        # silently blending pools across scopes.
        db_path = self._seed(
            candidates=[
                {"id": 1, "category": "fact", "created_at": "2023-06-01 00:00:00",
                 "agent_id": "agent-a"},
            ],
            messages={1: "2023-03-15 09:30:00"},
            dream_runs=[
                {
                    "id": 1,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 1,
                }
            ],
        )

        with self.assertRaises(ValueError):
            replay._replay_cycles(db_path, top_n=2)


class ForwardSimulateTests(_RunFixture):
    def _healed_pool(self, db_path: Path) -> list:
        return replay._load_diagnostic_candidates(db_path)

    def test_twenty_eligible_top_n_fifteen_drains_in_two_rounds(self) -> None:
        # 20 eligible candidates, top_n 15: round 1 promotes 15, round 2 the
        # remaining 5. Every candidate is assigned a round, none left over.
        db_path = self._seed(
            candidates=[
                {
                    "id": cid,
                    "category": "fact",
                    "importance": 5,
                    "created_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                }
                for cid in range(1, 21)
            ],
            messages={1: "2023-03-15 09:30:00"},
        )

        result = replay._forward_simulate(
            self._healed_pool(db_path), top_n=15, max_rounds=50, gap_days=1.0
        )

        self.assertEqual(result.eligible_pool_size, 20)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(len(result.promotes_at_round), 20)
        self.assertEqual(sorted(result.promotes_at_round), list(range(1, 21)))
        self.assertEqual({r for r in result.promotes_at_round.values()}, {1, 2})
        self.assertEqual(result.never_eligible, [])
        self.assertEqual(result.unassigned, [])

    def test_equal_scores_break_ties_by_id_ascending(self) -> None:
        # All candidates share every scoring term, so ranking is a pure id-ASC
        # tie-break (like select_promotions). With top_n 2 the first round takes
        # the two lowest ids, the next round the following two.
        db_path = self._seed(
            candidates=[
                {
                    "id": cid,
                    "category": "fact",
                    "importance": 5,
                    "created_at": "2023-06-01 00:00:00",
                    "last_seen_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                }
                for cid in (10, 11, 12, 13)
            ],
            messages={1: "2023-03-15 09:30:00"},
        )

        result = replay._forward_simulate(
            self._healed_pool(db_path), top_n=2, max_rounds=50, gap_days=1.0
        )

        self.assertEqual(result.promotes_at_round[10], 1)
        self.assertEqual(result.promotes_at_round[11], 1)
        self.assertEqual(result.promotes_at_round[12], 2)
        self.assertEqual(result.promotes_at_round[13], 2)

    def test_never_eligible_candidates_are_reported_unassigned(self) -> None:
        # Candidate 1 has no embedding and candidate 2 is an event still undated
        # after the heal: both are rejected by _deep_eligible and land in
        # never_eligible, never receiving a forward round. Candidate 3 drains.
        db_path = self._seed(
            candidates=[
                {
                    "id": 1,
                    "category": "fact",
                    "created_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                    "embed": False,
                },
                {
                    "id": 2,
                    "category": "event",
                    "occurred_at": None,
                    "mentioned_at": None,
                    "source_message_ids": [],
                    "created_at": "2023-06-01 00:00:00",
                },
                {
                    "id": 3,
                    "category": "fact",
                    "created_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                },
            ],
            messages={1: "2023-03-15 09:30:00"},
        )

        result = replay._forward_simulate(
            self._healed_pool(db_path), top_n=5, max_rounds=50, gap_days=1.0
        )

        self.assertEqual(result.never_eligible, [1, 2])
        self.assertEqual(result.eligible_pool_size, 1)
        self.assertEqual(result.promotes_at_round, {3: 1})
        self.assertEqual(result.unassigned, [])

    def test_empty_pool_runs_zero_rounds(self) -> None:
        # No candidates at all: nothing to drain, zero rounds, empty result.
        result = replay._forward_simulate([], top_n=15, max_rounds=50, gap_days=1.0)

        self.assertEqual(result.eligible_pool_size, 0)
        self.assertEqual(result.rounds, 0)
        self.assertEqual(result.promotes_at_round, {})
        self.assertEqual(result.never_eligible, [])
        self.assertEqual(result.unassigned, [])

    def test_max_forward_rounds_cap_leaves_backlog_unassigned(self) -> None:
        # 10 eligible, top_n 2 would need 5 rounds; a cap of 2 stops early with
        # the 6 undrained candidates reported in unassigned.
        db_path = self._seed(
            candidates=[
                {
                    "id": cid,
                    "category": "fact",
                    "importance": 5,
                    "created_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                }
                for cid in range(1, 11)
            ],
            messages={1: "2023-03-15 09:30:00"},
        )

        result = replay._forward_simulate(
            self._healed_pool(db_path), top_n=2, max_rounds=2, gap_days=1.0
        )

        self.assertEqual(result.rounds, 2)
        self.assertEqual(len(result.promotes_at_round), 4)
        self.assertEqual(len(result.unassigned), 6)
        self.assertEqual(result.never_eligible, [])


class MissingMentionedAtColumnTests(_RunFixture):
    def test_replay_candidates_load_without_mentioned_at_column(self) -> None:
        # Flag A: a frozen pre-heal DB may predate the mentioned_at migration and
        # lack the column entirely. _load_replay_candidates must probe the schema
        # and treat the stored mentioned_at as None for all rows rather than
        # raising "no such column".
        db_path = self._seed(
            candidates=[
                {"id": 1, "category": "fact", "created_at": "2023-06-01 00:00:00",
                 "source_message_ids": [1]},
                {"id": 2, "category": "event", "mentioned_at": None,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1]},
            ],
            messages={1: "2023-03-15 09:30:00"},
        )
        with closing(storage_connect(db_path)) as conn:
            conn.execute("ALTER TABLE memory_candidates DROP COLUMN mentioned_at")
            conn.commit()

        with replay._connection(db_path) as conn:
            candidates = replay._load_replay_candidates(conn)

        self.assertEqual([c.candidate_id for c in candidates], [1, 2])
        self.assertTrue(all(c.mentioned_at is None for c in candidates))

    def test_replay_cycles_run_against_uncolumned_db(self) -> None:
        # The full as-run replay path must survive a pre-column DB: as-run mode
        # reads stored mentioned_at (None everywhere here), so the undated event
        # is excluded as-run but healed by the derived per-cycle mentioned_at.
        db_path = self._seed(
            candidates=[
                {
                    "id": 1,
                    "category": "event",
                    "occurred_at": None,
                    "mentioned_at": None,
                    "source_message_ids": [1],
                    "created_at": "2023-06-01 00:00:00",
                },
                {
                    "id": 2,
                    "category": "fact",
                    "created_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                },
            ],
            messages={1: "2023-03-15 09:30:00"},
            dream_runs=[
                {
                    "id": 1,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 1,
                }
            ],
            facts=[
                {"id": 200, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        )
        with closing(storage_connect(db_path)) as conn:
            conn.execute("ALTER TABLE memory_candidates DROP COLUMN mentioned_at")
            conn.commit()

        cycles = replay._replay_cycles(db_path, top_n=5)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].as_run_pool_size, 1)
        self.assertEqual(cycles[0].healed_pool_size, 2)


class TrackedRankTrajectoryTests(_RunFixture):
    def test_tracked_candidate_rank_trajectory_both_modes(self) -> None:
        # Flag B: a tracked undated event (candidate 1) is excluded as-run but
        # joins the healed pool via its derived mentioned_at. Each cycle exposes
        # the tracked candidate's rank + eligibility in *both* modes, so the
        # trajectory is recoverable. Candidate 2 is a dated fact present in both.
        db_path = self._seed(
            candidates=[
                {
                    "id": 1,
                    "category": "event",
                    "occurred_at": None,
                    "mentioned_at": None,
                    "importance": 9,
                    "source_message_ids": [1],
                    "created_at": "2023-06-01 00:00:00",
                },
                {
                    "id": 2,
                    "category": "fact",
                    "importance": 5,
                    "source_message_ids": [1],
                    "created_at": "2023-06-01 00:00:00",
                },
            ],
            messages={1: "2023-03-15 09:30:00"},
            dream_runs=[
                {
                    "id": 1,
                    "started_at": "2023-06-05 00:00:00",
                    "finished_at": "2023-06-05 00:00:10",
                    "promotions": 1,
                }
            ],
            facts=[
                {"id": 200, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        )

        cycles = replay._replay_cycles(db_path, top_n=5, tracked_ids=[1, 2])

        self.assertEqual(len(cycles), 1)
        tracked = cycles[0].tracked_ranks
        self.assertEqual(set(tracked), {1, 2})
        # Candidate 1: undated event, absent as-run, present (and top-ranked on
        # its higher importance) once healed.
        self.assertFalse(tracked[1].as_run_eligible)
        self.assertIsNone(tracked[1].as_run_rank)
        self.assertTrue(tracked[1].healed_eligible)
        self.assertEqual(tracked[1].healed_rank, 1)
        # Candidate 2: dated fact, present in both modes.
        self.assertTrue(tracked[2].as_run_eligible)
        self.assertIsNotNone(tracked[2].as_run_rank)
        self.assertTrue(tracked[2].healed_eligible)
        self.assertEqual(tracked[2].healed_rank, 2)


class _MainFixture(_RunFixture):
    """Fixture that lays out on-disk gap fixtures with resolvable run DBs."""

    def _write_fixture(self, questions: list[dict]) -> Path:
        """Write a gaps.json and seed each question's memory.db at its run_dir.

        ``questions`` items carry ``question_id``, ``bucket``, ``gaps`` (fixture
        gap dicts) and ``seed`` (kwargs forwarded to ``_seed``).
        """
        entries = []
        for index, question in enumerate(questions, start=1):
            qid = question["question_id"]
            run_dir = self.root / f"run_{index}"
            db_path = run_dir / _question_path_component(qid) / "memory.db"
            self._seed(db_path=db_path, **question.get("seed", {}))
            entries.append(
                {
                    "question_id": qid,
                    "run_dir": str(run_dir),
                    "bucket": question.get("bucket", "b"),
                    "gaps": question.get("gaps", []),
                }
            )
        fixture_path = self.root / "gaps.json"
        fixture_path.write_text(json.dumps({"questions": entries}), encoding="utf-8")
        return fixture_path

    def _drained_seed(self) -> dict:
        """A one-Deep-cycle DB whose entire eligible pool promotes (backlog 0)."""
        return {
            "candidates": [
                {"id": 1, "category": "fact", "importance": 9,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 100},
                {"id": 2, "category": "fact", "importance": 7,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 101},
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 2},
                {"id": 2, "started_at": "2023-06-05 00:00:00",
                 "finished_at": "2023-06-05 00:00:10", "promotions": 2},
            ],
            "facts": [
                {"id": 100, "promoted_from_candidate_id": 1, "created_at": "2023-06-05 00:00:05"},
                {"id": 101, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        }


class MainEndToEndTests(_MainFixture):
    def test_main_writes_artifacts_with_contract_keys(self) -> None:
        fixture = self._write_fixture(
            [
                {
                    "question_id": "q-one",
                    "bucket": "single-session",
                    "seed": self._drained_seed(),
                    "gaps": [
                        {"gap_id": "g1", "kind": "tier2-miss", "frozen_candidate_id": 1},
                    ],
                },
                {
                    "question_id": "q-two",
                    "bucket": "multi-session",
                    "seed": self._drained_seed(),
                    "gaps": [],
                },
            ]
        )
        out_dir = self.root / "out"

        rc = replay.main(["--gaps", str(fixture), "--out", str(out_dir)])

        self.assertEqual(rc, 0)
        metrics = out_dir / "deep_backlog_replay_metrics.json"
        table = out_dir / "deep_backlog_replay_table.md"
        self.assertTrue(metrics.exists())
        self.assertTrue(table.exists())
        doc = json.loads(metrics.read_text(encoding="utf-8"))
        self.assertEqual(doc["deep_top_n"], replay.DEFAULT_TOP_N)
        self.assertEqual(len(doc["questions"]), 2)
        summary = doc["questions"][0]["summary"]
        for key in (
            "merge_event_count",
            "deep_cycles",
            "saturated_cycles",
            "mean_eligible_inflow_per_cycle",
            "deep_top_n",
            "structural_starvation_condition",
            "final_backlog_as_run",
            "final_backlog_healed",
            "forward_rounds_to_drain",
            "drain_verdict",
        ):
            self.assertIn(key, summary)
        self.assertIn("cycles", doc["questions"][0])
        self.assertIn("tracked_candidates", doc["questions"][0])
        # The drained DB verdict string is surfaced in the markdown table.
        self.assertIn("drained-during-run", table.read_text(encoding="utf-8"))

    def test_question_id_filter_and_unknown_id_error(self) -> None:
        fixture = self._write_fixture(
            [
                {"question_id": "q-one", "seed": self._drained_seed()},
                {"question_id": "q-two", "seed": self._drained_seed()},
            ]
        )
        out_dir = self.root / "out"

        rc = replay.main(
            ["--gaps", str(fixture), "--out", str(out_dir), "--question-id", "q-one"]
        )
        self.assertEqual(rc, 0)
        doc = json.loads((out_dir / "deep_backlog_replay_metrics.json").read_text())
        self.assertEqual([q["question_id"] for q in doc["questions"]], ["q-one"])

        rc_bad = replay.main(
            ["--gaps", str(fixture), "--question-id", "nope"]
        )
        self.assertEqual(rc_bad, 2)

    def test_missing_db_exits_two(self) -> None:
        # A fixture that names a run_dir with no memory.db is a fatal error.
        fixture_path = self.root / "gaps.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "questions": [
                        {
                            "question_id": "ghost",
                            "run_dir": str(self.root / "missing"),
                            "bucket": "b",
                            "gaps": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        rc = replay.main(["--gaps", str(fixture_path)])

        self.assertEqual(rc, 2)

    def test_unknown_frozen_candidate_id_exits_two(self) -> None:
        # A gap fixture naming a candidate absent from the run DB is stale; the
        # harness fails loud (exit 2) rather than silently tracking nothing,
        # matching the sibling promotion simulator.
        fixture = self._write_fixture(
            [
                {
                    "question_id": "q-stale",
                    "seed": self._drained_seed(),
                    "gaps": [
                        {"gap_id": "g999", "kind": "tier2-miss", "frozen_candidate_id": 999},
                    ],
                }
            ]
        )

        rc = replay.main(["--gaps", str(fixture)])

        self.assertEqual(rc, 2)

    def test_state_reconstruction_flags_exact_on_zero_merge_db(self) -> None:
        # A zero-merge DB whose hit_count log reconciles reports
        # state_reconstruction_exact True and hit_count_reconciles True.
        fixture = self._write_fixture(
            [{"question_id": "q-exact", "seed": self._drained_seed()}]
        )
        out = self.root / "out"

        replay.main(["--gaps", str(fixture), "--out", str(out)])

        doc = json.loads((out / "deep_backlog_replay_metrics.json").read_text())
        summary = doc["questions"][0]["summary"]
        self.assertTrue(summary["hit_count_reconciles"])
        self.assertTrue(summary["state_reconstruction_exact"])

    def test_hit_count_mismatch_fails_reconciliation(self) -> None:
        # A candidate whose stored hit_count (1) disagrees with its merge
        # events (one merge -> expected 2) fails hit_count_reconciles, and the
        # merge event also forces state_reconstruction_exact False.
        seed = {
            "candidates": [
                {"id": 1, "category": "fact", "created_at": "2023-06-01 00:00:00",
                 "source_message_ids": [1]},
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dedup_events": [
                {"id": 1, "candidate_id": 1, "created_at": "2023-06-02 00:00:00"},
            ],
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 1},
            ],
        }
        fixture = self._write_fixture([{"question_id": "q-mismatch", "seed": seed}])
        out = self.root / "out"

        replay.main(["--gaps", str(fixture), "--out", str(out)])

        doc = json.loads((out / "deep_backlog_replay_metrics.json").read_text())
        summary = doc["questions"][0]["summary"]
        self.assertFalse(summary["hit_count_reconciles"])
        self.assertFalse(summary["state_reconstruction_exact"])

    def test_zero_deep_cycle_db_gets_no_deep_cycles_verdict(self) -> None:
        # A DB with only watermark-advancing Light runs and no Deep cycle must
        # not crash; its drain verdict is the defined no-deep-cycles sentinel.
        seed = {
            "candidates": [
                {"id": 1, "category": "fact", "created_at": "2023-06-01 00:00:00",
                 "source_message_ids": [1]},
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 1},
            ],
        }
        fixture = self._write_fixture(
            [{"question_id": "q-flat", "seed": seed}]
        )
        out_dir = self.root / "out"

        rc = replay.main(["--gaps", str(fixture), "--out", str(out_dir)])

        self.assertEqual(rc, 0)
        doc = json.loads((out_dir / "deep_backlog_replay_metrics.json").read_text())
        self.assertEqual(
            doc["questions"][0]["summary"]["drain_verdict"], "no-deep-cycles"
        )

    def test_byte_identical_json_across_two_runs(self) -> None:
        fixture = self._write_fixture(
            [
                {
                    "question_id": "q-one",
                    "seed": self._drained_seed(),
                    "gaps": [
                        {"gap_id": "g1", "kind": "tier2-miss", "frozen_candidate_id": 1},
                    ],
                },
            ]
        )
        out_a = self.root / "a"
        out_b = self.root / "b"

        replay.main(["--gaps", str(fixture), "--out", str(out_a)])
        replay.main(["--gaps", str(fixture), "--out", str(out_b)])

        self.assertEqual(
            (out_a / "deep_backlog_replay_metrics.json").read_bytes(),
            (out_b / "deep_backlog_replay_metrics.json").read_bytes(),
        )

    def test_deep_top_n_override_changes_saturation(self) -> None:
        # One Deep cycle promoting exactly 2. At top_n 2 it is saturated; at
        # top_n 3 the same cycle is below cap and no longer saturated.
        fixture = self._write_fixture(
            [{"question_id": "q-sat", "seed": self._drained_seed()}]
        )

        def _saturated(top_n: int) -> int:
            out = self.root / f"out_{top_n}"
            replay.main(
                ["--gaps", str(fixture), "--out", str(out), "--deep-top-n", str(top_n)]
            )
            doc = json.loads((out / "deep_backlog_replay_metrics.json").read_text())
            return doc["questions"][0]["summary"]["saturated_cycles"]

        self.assertEqual(_saturated(2), 1)
        self.assertEqual(_saturated(3), 0)


class DrainVerdictTests(_MainFixture):
    def _verdict(self, seed: dict, *, top_n: int = 2, max_rounds: int = 50) -> str:
        fixture = self._write_fixture([{"question_id": "q", "seed": seed}])
        out = self.root / "out"
        replay.main(
            [
                "--gaps", str(fixture), "--out", str(out),
                "--deep-top-n", str(top_n),
                "--max-forward-rounds", str(max_rounds),
            ]
        )
        doc = json.loads((out / "deep_backlog_replay_metrics.json").read_text())
        return doc["questions"][0]["summary"]["drain_verdict"]

    def test_draining_run_is_drained_during_run(self) -> None:
        # Every eligible candidate promoted during the run: healed backlog 0.
        self.assertEqual(self._verdict(self._drained_seed()), "drained-during-run")

    def test_saturated_with_leftover_backlog_is_transient(self) -> None:
        # One saturated Deep cycle (promotes 2 at top_n 2) with a third eligible
        # candidate left unpromoted. Inflow (3, one cycle) is below top_n only if
        # measured across cycles; here a single cycle with backlog and no
        # structural inflow classifies as transient rather than starvation.
        seed = {
            "candidates": [
                {"id": 1, "category": "fact", "importance": 9,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 100},
                {"id": 2, "category": "fact", "importance": 8,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 101},
                # Unpromoted survivor: stays in the healed backlog at run end.
                {"id": 3, "category": "fact", "importance": 5,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1]},
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 3},
                {"id": 2, "started_at": "2023-06-05 00:00:00",
                 "finished_at": "2023-06-05 00:00:10", "promotions": 2},
            ],
            "facts": [
                {"id": 100, "promoted_from_candidate_id": 1, "created_at": "2023-06-05 00:00:05"},
                {"id": 101, "promoted_from_candidate_id": 2, "created_at": "2023-06-05 00:00:05"},
            ],
        }
        self.assertEqual(
            self._verdict(seed, top_n=2), "backlog-at-run-end-transient"
        )

    def test_eligible_candidate_cut_off_by_round_cap_is_undrained(self) -> None:
        # A tracked candidate that is promotion-eligible in the healed pool but
        # never drains because --max-forward-rounds is too small must land the
        # distinct undrained-at-round-cap verdict, not the wrong never-eligible.
        # Six identical dated facts, top_n 2, one forward round: ids 1,2 promote,
        # ids 3-6 are cut off in ForwardSimulation.unassigned. There is no Deep
        # cycle, so the tracked candidate was never promoted historically.
        seed = {
            "candidates": [
                {"id": cid, "category": "fact", "importance": 5,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1]}
                for cid in range(1, 7)
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 6},
            ],
        }
        fixture = self._write_fixture(
            [
                {
                    "question_id": "q-cap",
                    "seed": seed,
                    "gaps": [
                        {"gap_id": "g6", "kind": "tier2-miss", "frozen_candidate_id": 6},
                    ],
                }
            ]
        )
        out = self.root / "out"

        rc = replay.main(
            ["--gaps", str(fixture), "--out", str(out),
             "--deep-top-n", "2", "--max-forward-rounds", "1"]
        )

        self.assertEqual(rc, 0)
        doc = json.loads((out / "deep_backlog_replay_metrics.json").read_text())
        tracked = doc["questions"][0]["tracked_candidates"][0]
        self.assertEqual(tracked["candidate_id"], 6)
        self.assertIsNone(tracked["promoted_at_cycle"])
        self.assertIsNone(tracked["forward_promotes_at_round"])
        self.assertEqual(tracked["verdict"], "undrained-at-round-cap")
        self.assertEqual(doc["summary"]["undrained_at_round_cap"], 1)

    def test_broken_attribution_gates_all_verdicts(self) -> None:
        # When attribution is inconsistent (a promoted fact lands ahead of the
        # first Deep cycle, signalling a corrupt reconstruction), the harness must
        # not emit authoritative classifications: the drain verdict and every
        # tracked candidate verdict are gated to unreliable-attribution, and the
        # gating count is rolled up. This also pins precedence over the promoted
        # flag (candidate 1 is promoted but unattributed).
        seed = {
            "candidates": [
                {"id": 1, "category": "fact", "importance": 5,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 100},
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 1},
                {"id": 2, "started_at": "2023-06-05 00:00:00",
                 "finished_at": "2023-06-05 00:00:10", "promotions": 1},
            ],
            # Fact created before the first Deep cycle's start -> unattributed.
            "facts": [
                {"id": 100, "promoted_from_candidate_id": 1, "created_at": "2023-06-01 00:00:00"},
            ],
        }
        fixture = self._write_fixture(
            [
                {
                    "question_id": "q-gated",
                    "seed": seed,
                    "gaps": [
                        {"gap_id": "g1", "kind": "tier2-miss", "frozen_candidate_id": 1},
                    ],
                }
            ]
        )
        out = self.root / "out"

        rc = replay.main(["--gaps", str(fixture), "--out", str(out), "--deep-top-n", "2"])

        self.assertEqual(rc, 0)
        doc = json.loads((out / "deep_backlog_replay_metrics.json").read_text())
        question = doc["questions"][0]
        self.assertFalse(question["summary"]["attribution_consistent"])
        self.assertEqual(question["summary"]["drain_verdict"], "unreliable-attribution")
        self.assertEqual(
            question["tracked_candidates"][0]["verdict"], "unreliable-attribution"
        )
        self.assertEqual(doc["summary"]["unreliable_attribution"], 1)

    def test_promoted_flag_without_attribution_is_promoted_unattributed(self) -> None:
        # A tracked candidate whose final promoted flag is set but which
        # attribution never placed in a Deep cycle (no matching fact row) lands
        # the promoted-unattributed verdict -- distinct from never-eligible.
        # Attribution stays consistent (the lone Deep cycle promotes nothing), so
        # the gate does not fire and this specific branch is exercised.
        seed = {
            "candidates": [
                {"id": 2, "category": "fact", "importance": 5,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 100},
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 1},
                {"id": 2, "started_at": "2023-06-05 00:00:00",
                 "finished_at": "2023-06-05 00:00:10", "retirements": 1},
            ],
        }
        fixture = self._write_fixture(
            [
                {
                    "question_id": "q-pu",
                    "seed": seed,
                    "gaps": [
                        {"gap_id": "g2", "kind": "tier2-miss", "frozen_candidate_id": 2},
                    ],
                }
            ]
        )
        out = self.root / "out"

        rc = replay.main(["--gaps", str(fixture), "--out", str(out), "--deep-top-n", "2"])

        self.assertEqual(rc, 0)
        doc = json.loads((out / "deep_backlog_replay_metrics.json").read_text())
        question = doc["questions"][0]
        self.assertTrue(question["summary"]["attribution_consistent"])
        tracked = question["tracked_candidates"][0]
        self.assertIsNone(tracked["promoted_at_cycle"])
        self.assertEqual(tracked["verdict"], "promoted-unattributed")
        self.assertEqual(doc["summary"]["promoted_unattributed"], 1)

    def test_inflow_at_or_above_top_n_is_structural_starvation(self) -> None:
        # Two Deep cycles; each cycle sees >= top_n newly-eligible candidates
        # arrive, so mean inflow >= top_n and the leftover backlog is classified
        # structural. top_n 1: cycle 0 pool {1,2,3} (inflow 3), cycle 1 gains
        # {4,5} minus the one promoted -> inflow >= 1 sustained, backlog remains.
        seed = {
            "candidates": [
                {"id": 1, "category": "fact", "importance": 9,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 100},
                {"id": 2, "category": "fact", "importance": 5,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1]},
                {"id": 3, "category": "fact", "importance": 4,
                 "created_at": "2023-06-01 00:00:00", "source_message_ids": [1]},
                {"id": 4, "category": "fact", "importance": 8,
                 "created_at": "2023-06-10 00:00:00", "source_message_ids": [1],
                 "promoted": 1, "promoted_fact_id": 101},
                {"id": 5, "category": "fact", "importance": 3,
                 "created_at": "2023-06-10 00:00:00", "source_message_ids": [1]},
            ],
            "messages": {1: "2023-03-15 09:30:00"},
            "dream_runs": [
                {"id": 1, "last_processed_message_id": 5, "candidates_inserted": 3},
                {"id": 2, "started_at": "2023-06-05 00:00:00",
                 "finished_at": "2023-06-05 00:00:10", "promotions": 1},
                {"id": 3, "last_processed_message_id": 9, "candidates_inserted": 2},
                {"id": 4, "started_at": "2023-06-15 00:00:00",
                 "finished_at": "2023-06-15 00:00:10", "promotions": 1},
            ],
            "facts": [
                {"id": 100, "promoted_from_candidate_id": 1, "created_at": "2023-06-05 00:00:05"},
                {"id": 101, "promoted_from_candidate_id": 4, "created_at": "2023-06-15 00:00:05"},
            ],
        }
        self.assertEqual(
            self._verdict(seed, top_n=1),
            "structural-starvation-during-ingestion",
        )
