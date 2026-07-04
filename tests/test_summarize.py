from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.ports import HostPortNotConfigured
from vexic.storage import (
    fetch_session_summary_frontier,
    init_db,
    record_session_summary,
    save_messages,
)
from vexic.summarize import CONDENSE_MAX_FRONTIER_LEAVES, run_summarize_phase


def _msg(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _save_session(
    db_path: str,
    session_id: str,
    *,
    count: int,
    start: datetime,
    agent_id: str | None = None,
    text: str = "message body padding to add tokens " * 3,
) -> list[int]:
    ids: list[int] = []
    for index in range(count):
        timestamp = start + timedelta(minutes=index)
        ids.extend(
            save_messages(
                db_path,
                [_msg(f"{text} #{index}")],
                session_id=session_id,
                agent_id=agent_id,
                timestamp=timestamp.isoformat(),
            )
        )
    return ids


class FakeSummaryAgent:
    """Fake AgentFactory-compatible agent: summarizes by echoing a marker."""

    def __init__(self, on_run=None):
        self.calls: list[str] = []
        self._on_run = on_run

    async def run(self, prompt: str):
        self.calls.append(prompt)
        if self._on_run is not None:
            self._on_run(prompt)
        return SimpleNamespace(
            output=f"summary of: {prompt[:20]}",
            usage=lambda: SimpleNamespace(
                requests=1,
                input_tokens=5,
                output_tokens=3,
                total_tokens=8,
            ),
        )


class SummarizePhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fails_closed_without_summary_agent_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            with self.assertRaises(HostPortNotConfigured):
                await run_summarize_phase(db_path, "glm")

    async def test_leaf_pass_writes_leaf_rows_and_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            # A > 2h gap between messages 5 and 6 creates a boundary so the
            # leaf pass has more than one span to walk through.
            _save_session(db_path, "default", count=5, start=start)
            _save_session(
                db_path,
                "default",
                count=5,
                start=start + timedelta(hours=3),
            )

            agent = FakeSummaryAgent()

            def factory(model_group: str, secrets=None):
                return agent

            usage = await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                now_utc=start + timedelta(hours=6),
            )

            frontier = fetch_session_summary_frontier(db_path, session_id="default")
            self.assertTrue(len(frontier) >= 1)
            self.assertTrue(all(s.kind == "leaf" for s in frontier))
            self.assertGreater(usage.total_tokens, 0)
            self.assertGreaterEqual(usage.model_requests, 1)

    async def test_condense_pass_triggers_on_frontier_leaf_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)

            leaf_count = CONDENSE_MAX_FRONTIER_LEAVES + 1
            message_ids = _save_session(
                db_path,
                "default",
                count=leaf_count,
                start=start,
            )
            # Pre-seed a fully-covering run of tiny leaf summaries (one per
            # message, contiguous, no gaps) so `find_session_compaction_span`
            # reports nothing left to summarize -- only the condense pass
            # should fire, triggered purely by frontier leaf count.
            for message_id in message_ids:
                record_session_summary(
                    db_path,
                    session_id="default",
                    kind="leaf",
                    first_message_id=message_id,
                    last_message_id=message_id,
                    summary_text=f"leaf summary for message {message_id}",
                )

            agent = FakeSummaryAgent()

            def factory(model_group: str, secrets=None):
                return agent

            await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                now_utc=start + timedelta(hours=1),
            )

            frontier = fetch_session_summary_frontier(db_path, session_id="default")
            self.assertEqual(len(frontier), 1)
            self.assertEqual(frontier[0].kind, "condensed")
            self.assertEqual(len(frontier[0].replaces_summary_ids), leaf_count)
            self.assertEqual(frontier[0].first_message_id, message_ids[0])
            self.assertEqual(frontier[0].last_message_id, message_ids[-1])

    async def test_condense_pass_triggers_on_frontier_token_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

            # Few leaves (well under CONDENSE_MAX_FRONTIER_LEAVES) but each
            # with a huge summary_text so the frontier's token_estimate total
            # exceeds TAU_SOFT // 3 (6000 tokens): 3 leaves x 10_000 chars
            # ~= 7500 tokens. Only the token branch can trigger condense here.
            leaf_count = 3
            message_ids = _save_session(
                db_path,
                "default",
                count=leaf_count,
                start=start,
            )
            for message_id in message_ids:
                record_session_summary(
                    db_path,
                    session_id="default",
                    kind="leaf",
                    first_message_id=message_id,
                    last_message_id=message_id,
                    summary_text="verbose summary " * 625,  # 10_000 chars
                )

            agent = FakeSummaryAgent()

            def factory(model_group: str, secrets=None):
                return agent

            await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                now_utc=start + timedelta(hours=1),
            )

            frontier = fetch_session_summary_frontier(db_path, session_id="default")
            self.assertEqual(len(frontier), 1)
            self.assertEqual(frontier[0].kind, "condensed")
            self.assertEqual(len(frontier[0].replaces_summary_ids), leaf_count)
            self.assertEqual(frontier[0].first_message_id, message_ids[0])
            self.assertEqual(frontier[0].last_message_id, message_ids[-1])

    async def test_condense_pass_condenses_only_oldest_contiguous_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

            # 12 messages; leaves cover messages 0..8 contiguously, then a
            # gap (message index 9 uncovered), then leaves for 10 and 11.
            # The uncovered gap is small (no time boundary, well under
            # tau_soft), so no new leaf span fires and the gapped frontier
            # persists into the condense pass. The frontier count (11)
            # exceeds CONDENSE_MAX_FRONTIER_LEAVES, but only the oldest
            # contiguous run (the first 9 leaves) may be condensed -- the
            # condensed row must never span the uncovered gap.
            message_ids = _save_session(
                db_path,
                "default",
                count=12,
                start=start,
            )
            covered_indices = [*range(9), 10, 11]
            for index in covered_indices:
                record_session_summary(
                    db_path,
                    session_id="default",
                    kind="leaf",
                    first_message_id=message_ids[index],
                    last_message_id=message_ids[index],
                    summary_text=f"leaf summary for message {message_ids[index]}",
                )

            agent = FakeSummaryAgent()

            def factory(model_group: str, secrets=None):
                return agent

            await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                now_utc=start + timedelta(hours=1),
            )

            frontier = fetch_session_summary_frontier(db_path, session_id="default")
            self.assertEqual(
                [summary.kind for summary in frontier],
                ["condensed", "leaf", "leaf"],
            )
            condensed = frontier[0]
            self.assertEqual(condensed.first_message_id, message_ids[0])
            self.assertEqual(condensed.last_message_id, message_ids[8])
            self.assertEqual(len(condensed.replaces_summary_ids), 9)
            self.assertEqual(
                [summary.first_message_id for summary in frontier[1:]],
                [message_ids[10], message_ids[11]],
            )

    async def test_redaction_failure_records_error_and_continues_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            _save_session(
                db_path,
                "leaky-session",
                count=3,
                start=start,
                text="LEAKYMARKER body padding to add tokens " * 3,
            )
            _save_session(
                db_path,
                "clean-session",
                count=3,
                start=start + timedelta(hours=1),
                text="CLEANMARKER body padding to add tokens " * 3,
            )

            def factory(model_group: str, secrets=None):
                return _LeakOnMarkerAgent()

            class _LeakOnMarkerAgent(FakeSummaryAgent):
                async def run(self, prompt: str):
                    self.calls.append(prompt)
                    if "LEAKYMARKER" in prompt:
                        return SimpleNamespace(
                            output="the secret is s3cr3t-value",
                            usage=lambda: SimpleNamespace(
                                requests=1, input_tokens=1, output_tokens=1, total_tokens=2
                            ),
                        )
                    return await super().run(prompt)

            usage = await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                forbidden_secret_values=("s3cr3t-value",),
                now_utc=start + timedelta(hours=6),
            )

            leaky_frontier = fetch_session_summary_frontier(db_path, session_id="leaky-session")
            clean_frontier = fetch_session_summary_frontier(db_path, session_id="clean-session")
            self.assertEqual(leaky_frontier, [])
            self.assertTrue(len(clean_frontier) >= 1)
            self.assertGreater(usage.total_tokens, 0)

    async def test_per_session_error_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            _save_session(
                db_path,
                "session-a",
                count=3,
                start=start,
                text="FAILMARKER body padding to add tokens " * 3,
            )
            _save_session(
                db_path,
                "session-b",
                count=3,
                start=start + timedelta(hours=1),
                text="OKMARKER body padding to add tokens " * 3,
            )

            class FailOnFirstSessionAgent(FakeSummaryAgent):
                async def run(self, prompt: str):
                    if "FAILMARKER" in prompt:
                        raise RuntimeError("boom")
                    return await super().run(prompt)

            def factory(model_group: str, secrets=None):
                return FailOnFirstSessionAgent()

            usage = await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                now_utc=start + timedelta(hours=6),
            )

            session_a_frontier = fetch_session_summary_frontier(db_path, session_id="session-a")
            session_b_frontier = fetch_session_summary_frontier(db_path, session_id="session-b")
            self.assertEqual(session_a_frontier, [])
            self.assertTrue(len(session_b_frontier) >= 1)
            self.assertGreater(usage.total_tokens, 0)

    async def test_leaf_pass_never_invokes_agent_on_forbidden_input(self) -> None:
        # Fail-closed on the *input* side: a forbidden secret value present
        # in the source transcript must be caught before `agent.run` is ever
        # called for that session -- not merely scrubbed from the output.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            _save_session(
                db_path,
                "leaky-session",
                count=3,
                start=start,
                text="s3cr3t-value body padding to add tokens " * 3,
            )
            _save_session(
                db_path,
                "clean-session",
                count=3,
                start=start + timedelta(hours=1),
                text="CLEANMARKER body padding to add tokens " * 3,
            )

            leaky_agent = FakeSummaryAgent()
            clean_agent = FakeSummaryAgent()

            # Route by marker text instead of session id, since the rendered
            # transcript source does not literally include the session id.
            class MarkerRoutingAgent:
                async def run(self, prompt: str):
                    if "s3cr3t-value" in prompt:
                        return await leaky_agent.run(prompt)
                    return await clean_agent.run(prompt)

            def factory(model_group: str, secrets=None):
                return MarkerRoutingAgent()

            usage = await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                forbidden_secret_values=("s3cr3t-value",),
                now_utc=start + timedelta(hours=6),
            )

            self.assertEqual(leaky_agent.calls, [])
            leaky_frontier = fetch_session_summary_frontier(db_path, session_id="leaky-session")
            clean_frontier = fetch_session_summary_frontier(db_path, session_id="clean-session")
            self.assertEqual(leaky_frontier, [])
            self.assertTrue(len(clean_frontier) >= 1)
            self.assertGreater(usage.total_tokens, 0)

    async def test_condense_pass_never_invokes_agent_on_forbidden_input(self) -> None:
        # Same fail-closed guarantee for the condense pass: a forbidden
        # secret value already present in a frontier summary's text must
        # stop the condense agent from ever being called.
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)

            leaf_count = CONDENSE_MAX_FRONTIER_LEAVES + 1
            message_ids = _save_session(
                db_path,
                "default",
                count=leaf_count,
                start=start,
            )
            # Seed the frontier with *no* forbidden values present (the
            # write-side guard on record_session_summary would otherwise
            # reject this setup), then run condense with forbidden set so
            # the violation is only visible when building the condense
            # source from the previously recorded summaries.
            for index, message_id in enumerate(message_ids):
                summary_text = (
                    "s3cr3t-value leaf summary" if index == 0 else f"leaf summary for message {message_id}"
                )
                record_session_summary(
                    db_path,
                    session_id="default",
                    kind="leaf",
                    first_message_id=message_id,
                    last_message_id=message_id,
                    summary_text=summary_text,
                )

            agent = FakeSummaryAgent()

            def factory(model_group: str, secrets=None):
                return agent

            await run_summarize_phase(
                db_path,
                "glm",
                summary_agent_factory=factory,
                forbidden_secret_values=("s3cr3t-value",),
                now_utc=start + timedelta(hours=1),
            )

            self.assertEqual(agent.calls, [])
            frontier = fetch_session_summary_frontier(db_path, session_id="default")
            self.assertEqual(len(frontier), leaf_count)
            self.assertTrue(all(s.kind == "leaf" for s in frontier))


if __name__ == "__main__":
    unittest.main()
