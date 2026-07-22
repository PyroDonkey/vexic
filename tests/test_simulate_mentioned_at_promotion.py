"""Tests for the Promotion-eligibility simulation harness.

Fully deterministic: the harness makes no provider calls, so every path is
exercised directly. Each test builds its own synthetic run DB (the pattern in
tests/test_oracle_evidence_experiment.py), seeded to look like a frozen
pre-ADR-0037 artifact: undated ``event`` candidates whose ``mentioned_at`` is
NULL, with transcript rows the init-time backfill can derive a date from.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
from contextlib import closing, redirect_stdout
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import TestCase

from vexic.longmemeval_analysis import _question_path_component
from vexic.storage.schema import _ensure_vector_memory_schema, init_vector_memory
from vexic.storage.connection import connect as storage_connect
from vexic.storage.schema import EMBEDDING_DIM, _serialize_float32

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "simulate_mentioned_at_promotion.py"


def _load_module() -> ModuleType:
    """Load the script by path: scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "simulate_mentioned_at_promotion", _MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sim = _load_module()


class _RunFixture(TestCase):
    """Shared synthetic-run scaffolding."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _db_path(self, question_id: str) -> Path:
        return self.run_dir / _question_path_component(question_id) / "memory.db"

    def _seed_db(
        self,
        question_id: str,
        candidates: list[dict],
        *,
        messages: dict[int, str] | None = None,
        facts: list[dict] | None = None,
    ) -> Path:
        """candidates: category/occurred_at/source_message_ids/embed keys.

        ``messages`` maps message id -> timestamp; those rows are what the
        init-time ``mentioned_at`` backfill derives dates from.
        """
        db_path = self._db_path(question_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_vector_memory(str(db_path))
        embedding = _serialize_float32([1.0] + [0.0] * (EMBEDDING_DIM - 1))
        with closing(storage_connect(db_path)) as conn:
            # Loads the sqlite-vec extension on this connection so the vec0
            # embeddings table is writable (its CREATEs are no-ops here).
            _ensure_vector_memory_schema(conn)
            for message_id, timestamp in (messages or {}).items():
                conn.execute(
                    """
                    INSERT INTO messages (id, session_id, agent_id, timestamp, message_json)
                    VALUES (?, 'session', NULL, ?, '{}')
                    """,
                    (message_id, timestamp),
                )
            for index, candidate in enumerate(candidates, start=1):
                conn.execute(
                    """
                    INSERT INTO memory_candidates (
                        id, fact_text, subject, category, importance, confidence,
                        source_message_ids, hit_count, rem_boost, occurred_at,
                        mentioned_at, last_seen_at, created_at,
                        retired, stale, needs_review
                    ) VALUES (?, ?, 'user', ?, ?, 0.9, ?, 1, 0.0, ?, ?,
                              ?, '2023-06-01 00:00:00', ?, ?, ?)
                    """,
                    (
                        candidate.get("id", index),
                        candidate.get("fact_text", f"Candidate {index}."),
                        candidate.get("category", "event"),
                        candidate.get("importance", 5),
                        json.dumps(candidate.get("source_message_ids", [1])),
                        candidate.get("occurred_at"),
                        candidate.get("mentioned_at"),
                        candidate.get("last_seen_at", "2023-06-01 00:00:00"),
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
            for fact in facts or []:
                conn.execute(
                    """
                    INSERT INTO long_term_memory (
                        id, fact_text, subject, category, importance, confidence,
                        source_message_ids, occurred_at, mentioned_at,
                        promoted_from_candidate_id
                    ) VALUES (?, ?, 'user', ?, 5, 0.9, ?, ?, NULL, ?)
                    """,
                    (
                        fact["id"],
                        fact.get("fact_text", "A fact."),
                        fact.get("category", "event"),
                        json.dumps(fact.get("source_message_ids", [1])),
                        fact.get("occurred_at"),
                        fact.get("promoted_from_candidate_id", 1),
                    ),
                )
            conn.commit()
        return db_path

    def _fixture(self, questions: list[dict]) -> Path:
        path = self.root / "gaps.json"
        path.write_text(json.dumps({"questions": questions}), encoding="utf-8")
        return path

    def _question(self, question_id: str, gaps: list[dict], **overrides) -> dict:
        entry = {
            "question_id": question_id,
            "run_dir": str(self.run_dir),
            "bucket": "undated-event",
            "gaps": gaps,
        }
        entry.update(overrides)
        return entry

    def _simulate(self, entry_path: Path) -> list[dict]:
        entries = sim.load_gap_fixture(entry_path)
        with TemporaryDirectory() as workspace:
            return [
                sim.simulate_question(entry, workspace=Path(workspace))
                for entry in entries
            ]


class GapFixtureTests(_RunFixture):
    def test_load_fixture_returns_typed_entries(self) -> None:
        path = self._fixture(
            [
                self._question("q1", [{"gap_id": "g1", "kind": "tier2-undated-event"}]),
                self._question("q2", []),
            ]
        )

        entries = sim.load_gap_fixture(path)

        self.assertEqual([entry.question_id for entry in entries], ["q1", "q2"])
        self.assertEqual(entries[0].gaps[0].gap_id, "g1")

    def test_load_fixture_accepts_a_bare_list(self) -> None:
        path = self.root / "bare.json"
        path.write_text(json.dumps([self._question("q1", [])]), encoding="utf-8")

        self.assertEqual(len(sim.load_gap_fixture(path)), 1)

    def test_load_fixture_rejects_duplicate_question_id(self) -> None:
        path = self._fixture([self._question("q1", []), self._question("q1", [])])

        with self.assertRaises(sim.GapFixtureError):
            sim.load_gap_fixture(path)

    def test_load_fixture_rejects_duplicate_gap_id(self) -> None:
        gaps = [
            {"gap_id": "g1", "kind": "tier2-undated-event"},
            {"gap_id": "g1", "kind": "transcript-only"},
        ]
        path = self._fixture([self._question("q1", gaps)])

        with self.assertRaises(sim.GapFixtureError):
            sim.load_gap_fixture(path)

    def test_missing_run_db_fails_loud(self) -> None:
        path = self._fixture([self._question("q1", [])])

        with self.assertRaises(sim.GapFixtureError):
            self._simulate(path)


class SimulationTests(_RunFixture):
    def test_undated_event_flips_eligible_after_backfill(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 36, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertTrue(gap["mentioned_at_healed"])
        self.assertEqual(gap["healed_mentioned_at"], "2023-04-29")
        self.assertFalse(gap["eligible_before"])
        self.assertTrue(gap["eligible_after"])
        self.assertEqual(gap["rank_after"], 1)
        self.assertTrue(gap["within_deep_top_n"])
        # A lone flipping candidate ranks trivially, so the verdict is the
        # degenerate-pool class rather than the informative ranked class.
        self.assertTrue(gap["top_n_covers_pool"])
        self.assertEqual(gap["verdict"], "flips-eligible-degenerate-pool")

    def test_populated_frozen_mentioned_at_does_not_flip(self) -> None:
        # The frozen artifact already carries mentioned_at, so the before-pool
        # must reflect that value rather than blanking it: no spurious flip.
        self._seed_db(
            "q1",
            [
                {
                    "id": 36,
                    "category": "event",
                    "mentioned_at": "2023-04-29",
                    "source_message_ids": [1],
                }
            ],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertTrue(gap["eligible_before"])
        self.assertFalse(gap["mentioned_at_healed"])
        self.assertEqual(gap["verdict"], "already-eligible")

    def test_unresolvable_sources_stay_ineligible(self) -> None:
        # The cited message does not exist, so no date can be derived: the
        # candidate stays in Tier 2 exactly as ADR 0037 specifies.
        self._seed_db(
            "q1",
            [{"id": 7, "category": "event", "source_message_ids": [999]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 7,
                        }
                    ],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertFalse(gap["mentioned_at_healed"])
        self.assertFalse(gap["eligible_after"])
        self.assertIsNone(gap["rank_after"])
        self.assertEqual(gap["verdict"], "still-ineligible")

    def test_dated_event_was_already_eligible(self) -> None:
        self._seed_db(
            "q1",
            [
                {
                    "id": 5,
                    "category": "event",
                    "occurred_at": "2023-03-01",
                    "source_message_ids": [1],
                }
            ],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 5,
                        }
                    ],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertTrue(gap["eligible_before"])
        self.assertEqual(gap["verdict"], "already-eligible")

    def test_flip_in_degenerate_pool_is_reported_separately(self) -> None:
        # A single flipping candidate ranks 1 trivially, so the flip says
        # nothing about ranking. Reported as a degenerate-pool flip, distinct
        # from the informative ranked class.
        self._seed_db(
            "q1",
            [{"id": 36, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertTrue(gap["top_n_covers_pool"])
        self.assertEqual(gap["verdict"], "flips-eligible-degenerate-pool")

    def test_informative_flip_within_top_n(self) -> None:
        # 16 healable event candidates overflow the default top-n slice, so the
        # pool is informative. The strongest one flips eligible and ranks first.
        candidates = [
            {"id": index, "category": "event", "importance": 5, "source_message_ids": [1]}
            for index in range(2, 17)
        ]
        candidates.insert(
            0,
            {"id": 1, "category": "event", "importance": 9, "source_message_ids": [1]},
        )
        self._seed_db("q1", candidates, messages={1: "2023-04-29 10:00:00"})
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 1,
                        }
                    ],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertFalse(gap["top_n_covers_pool"])
        self.assertEqual(gap["rank_after"], 1)
        self.assertEqual(gap["verdict"], "flips-eligible-and-ranked")

    def test_rank_outside_deep_top_n_is_reported_separately(self) -> None:
        # One low-importance gap candidate behind three stronger ones, with
        # deep_top_n=2: eligible, but not in the slice Deep would promote.
        candidates = [
            {"id": index, "category": "fact", "importance": 9, "source_message_ids": [1]}
            for index in range(1, 4)
        ]
        candidates.append(
            {"id": 4, "category": "event", "importance": 1, "source_message_ids": [1]}
        )
        self._seed_db("q1", candidates, messages={1: "2023-04-29 10:00:00"})
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 4,
                        }
                    ],
                )
            ]
        )
        entries = sim.load_gap_fixture(path)
        with TemporaryDirectory() as workspace:
            result = sim.simulate_question(
                entries[0], workspace=Path(workspace), deep_top_n=2
            )

        gap = result["gaps"][0]
        self.assertTrue(gap["eligible_after"])
        self.assertFalse(gap["within_deep_top_n"])
        self.assertEqual(gap["verdict"], "flips-eligible-outside-top-n")

    def test_transcript_only_gap_is_not_simulatable(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [self._question("q1", [{"gap_id": "g1", "kind": "transcript-only"}])]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertFalse(gap["simulated"])
        self.assertEqual(gap["verdict"], "not-simulatable")

    def test_undated_tier3_fact_reports_its_healed_date(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "fact", "source_message_ids": [1]}],
            messages={1: "2023-01-15 09:00:00"},
            facts=[{"id": 70, "source_message_ids": [1]}],
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [{"gap_id": "g1", "kind": "tier3-undated", "frozen_fact_id": 70}],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertTrue(gap["simulated"])
        self.assertEqual(gap["healed_mentioned_at"], "2023-01-15")
        self.assertEqual(gap["verdict"], "fact-now-dated")

    def test_unknown_candidate_id_fails_loud(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 1, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 404,
                        }
                    ],
                )
            ]
        )

        with self.assertRaises(sim.GapFixtureError):
            self._simulate(path)

    def test_source_run_db_is_never_mutated(self) -> None:
        db_path = self._seed_db(
            "q1",
            [{"id": 36, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        # Freeze the artifact the way .eval-runs/** is frozen: a copy taken
        # before the run, compared byte for byte after it.
        frozen = self.root / "frozen.db"
        shutil.copy2(db_path, frozen)
        before = sha256(db_path.read_bytes()).hexdigest()
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

        self._simulate(path)

        self.assertEqual(sha256(db_path.read_bytes()).hexdigest(), before)
        self.assertEqual(
            sha256(frozen.read_bytes()).hexdigest(),
            sha256(db_path.read_bytes()).hexdigest(),
        )

    def test_source_sidecars_are_not_touched(self) -> None:
        # A frozen WAL-mode artifact with its sidecars collapsed. Reading the
        # source in mode=ro would re-create a -shm next to it, so the harness
        # must open only the copy: the source dir stays byte-for-byte frozen.
        db_path = self._seed_db(
            "q1",
            [{"id": 36, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE memory_candidates SET hit_count = 2 WHERE id = 36")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        before_files = {entry.name for entry in db_path.parent.iterdir()}
        before_hash = sha256(db_path.read_bytes()).hexdigest()
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

        self._simulate(path)

        self.assertFalse(db_path.with_name(db_path.name + "-wal").exists())
        self.assertFalse(db_path.with_name(db_path.name + "-shm").exists())
        self.assertEqual({entry.name for entry in db_path.parent.iterdir()}, before_files)
        self.assertEqual(sha256(db_path.read_bytes()).hexdigest(), before_hash)

    def test_scoring_time_is_pool_max_last_seen_at(self) -> None:
        self._seed_db(
            "q1",
            [
                {
                    "id": 1,
                    "category": "event",
                    "last_seen_at": "2023-06-01 00:00:00",
                    "source_message_ids": [1],
                },
                {
                    "id": 2,
                    "category": "event",
                    "last_seen_at": "2023-06-15 08:00:00",
                    "source_message_ids": [1],
                },
            ],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture([self._question("q1", [])])

        result = self._simulate(path)[0]

        self.assertEqual(result["scoring_time"], "2023-06-15T08:00:00+00:00")

    def test_read_only_frozen_db_is_healed_on_a_writable_copy(self) -> None:
        # A frozen provenance artifact chmod'd read-only (0444) must still heal:
        # the copy has to be made user-writable before init_db writes to it.
        db_path = self._seed_db(
            "q1",
            [{"id": 36, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        frozen = [db_path]
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                frozen.append(sidecar)
        for target in frozen:
            # Restore write permission in cleanup so the tempdir can be removed.
            self.addCleanup(os.chmod, target, 0o644)
            os.chmod(target, 0o444)
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

        gap = self._simulate(path)[0]["gaps"][0]

        self.assertTrue(gap["mentioned_at_healed"])
        self.assertEqual(gap["verdict"], "flips-eligible-degenerate-pool")

    def test_scoring_time_is_deterministic_for_an_empty_pool(self) -> None:
        # Every candidate is retired, so the deep-eligible pool is empty. The
        # scoring clock must fall back to a fixed anchor, not wall-clock now, so
        # identical reruns emit an identical scoring_time.
        self._seed_db(
            "q1",
            [
                {
                    "id": 1,
                    "category": "event",
                    "retired": 1,
                    "source_message_ids": [1],
                }
            ],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture([self._question("q1", [])])

        first = self._simulate(path)[0]
        second = self._simulate(path)[0]

        self.assertEqual(first["scoring_time"], second["scoring_time"])
        self.assertEqual(first["scoring_time"], "1970-01-01T00:00:00+00:00")

    def test_copy_question_db_copies_wal_sidecars(self) -> None:
        source = self.root / "src"
        source.mkdir()
        db = source / "memory.db"
        db.write_bytes(b"main")
        db.with_name("memory.db-wal").write_bytes(b"wal")
        db.with_name("memory.db-shm").write_bytes(b"shm")
        dest = self.root / "dest"

        copy_path = sim._copy_question_db(db, dest)

        self.assertEqual(copy_path.read_bytes(), b"main")
        self.assertEqual(
            copy_path.with_name(copy_path.name + "-wal").read_bytes(), b"wal"
        )
        self.assertEqual(
            copy_path.with_name(copy_path.name + "-shm").read_bytes(), b"shm"
        )

    def test_pre_column_artifact_is_flagged_and_healed(self) -> None:
        # A genuinely pre-ADR-0037 artifact has no mentioned_at column at all.
        db_path = self._seed_db(
            "q1",
            [{"id": 36, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("ALTER TABLE memory_candidates DROP COLUMN mentioned_at")
            conn.commit()
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

        result = self._simulate(path)[0]

        self.assertTrue(result["pre_mentioned_at_column"])
        self.assertEqual(
            result["gaps"][0]["verdict"], "flips-eligible-degenerate-pool"
        )

    def test_frozen_candidate_rows_handles_missing_needs_review(self) -> None:
        # A genuinely old artifact: memory_candidates has neither needs_review
        # nor mentioned_at. The pre-heal read runs before init_db adds either
        # column, so it must guard both rather than raise OperationalError.
        db_path = self.root / "old.db"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE memory_candidates (
                    id INTEGER PRIMARY KEY,
                    category TEXT,
                    occurred_at TEXT,
                    promoted INTEGER,
                    retired INTEGER,
                    stale INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO memory_candidates "
                "(id, category, occurred_at, promoted, retired, stale) "
                "VALUES (1, 'event', NULL, 0, 0, 0)"
            )
            conn.commit()

        rows = sim.frozen_candidate_rows(db_path)

        self.assertIn(1, rows)
        self.assertFalse(rows[1]["needs_review"])
        self.assertIsNone(rows[1]["mentioned_at"])
        self.assertTrue(rows[1]["pre_mentioned_at_column"])

    def test_healed_copy_stays_inside_workspace(self) -> None:
        # A question_id carrying a path separator must not steer the healed
        # copy out of the workspace: the destination has to be sanitized with
        # _question_path_component, exactly as the source path is.
        qid = "q1/../escape"
        self._seed_db(
            qid,
            [{"id": 1, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    qid,
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 1,
                        }
                    ],
                )
            ]
        )
        entries = sim.load_gap_fixture(path)
        workspace = self.root / "workspace"
        workspace.mkdir()

        sim.simulate_question(entries[0], workspace=workspace)

        component = _question_path_component(qid)
        self.assertTrue((workspace / component / "memory.db").exists())
        # Nothing may be written outside the workspace TemporaryDirectory.
        self.assertFalse((self.root / "escape").exists())


class ModuleTests(TestCase):
    def test_workspace_prefix_carries_no_tracker_id(self) -> None:
        # The temp-workspace prefix must not smuggle a tracker id into scratch
        # paths. The pattern is split so this test file stays boundary-clean.
        self.assertIsNone(
            re.search(r"(?i)\bC" + r"OA[-_]?\d+", sim._WORKSPACE_PREFIX)
        )


class CliTests(_RunFixture):
    def _seed_one(self) -> Path:
        # An informative pool: 16 healable events overflow the top-n slice, so
        # the named candidate's flip lands in the ranked class, not degenerate.
        candidates = [
            {"id": index, "category": "event", "importance": 5, "source_message_ids": [1]}
            for index in range(1, 16)
        ]
        candidates.append(
            {"id": 36, "category": "event", "importance": 9, "source_message_ids": [1]}
        )
        self._seed_db("q1", candidates, messages={1: "2023-04-29 10:00:00"})
        return self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                )
            ]
        )

    def test_main_writes_artifacts(self) -> None:
        path = self._seed_one()
        out_dir = self.root / "out"

        with redirect_stdout(StringIO()):
            exit_code = sim.main(["--gaps", str(path), "--out", str(out_dir)])

        self.assertEqual(exit_code, 0)
        doc = json.loads((out_dir / "promotion_simulation_metrics.json").read_text())
        self.assertEqual(doc["summary"]["flips_eligible_and_ranked"], 1)
        self.assertIn("flips-eligible-and-ranked", (out_dir / "promotion_simulation_table.md").read_text())

    def test_main_runs_only_requested_question(self) -> None:
        self._seed_db(
            "q1",
            [{"id": 36, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        self._seed_db(
            "q2",
            [{"id": 37, "category": "event", "source_message_ids": [1]}],
            messages={1: "2023-04-29 10:00:00"},
        )
        path = self._fixture(
            [
                self._question(
                    "q1",
                    [
                        {
                            "gap_id": "g1",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 36,
                        }
                    ],
                ),
                self._question(
                    "q2",
                    [
                        {
                            "gap_id": "g2",
                            "kind": "tier2-undated-event",
                            "frozen_candidate_id": 37,
                        }
                    ],
                ),
            ]
        )
        out_dir = self.root / "out"

        with redirect_stdout(StringIO()):
            exit_code = sim.main(
                ["--gaps", str(path), "--question-id", "q1", "--out", str(out_dir)]
            )

        self.assertEqual(exit_code, 0)
        doc = json.loads((out_dir / "promotion_simulation_metrics.json").read_text())
        self.assertEqual([q["question_id"] for q in doc["questions"]], ["q1"])

    def test_main_filters_by_question_id(self) -> None:
        path = self._seed_one()
        buffer = StringIO()

        with redirect_stdout(buffer):
            exit_code = sim.main(["--gaps", str(path), "--question-id", "nope"])

        self.assertEqual(exit_code, 2)

    def test_main_rejects_a_broken_fixture(self) -> None:
        path = self.root / "broken.json"
        path.write_text("{not json", encoding="utf-8")

        self.assertEqual(sim.main(["--gaps", str(path)]), 2)
