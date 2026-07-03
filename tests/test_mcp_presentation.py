import unittest

from vexic.contract import CandidateNote, LongTermFact, MemoryCategory, TranscriptHit
from vexic.formatting import UNVERIFIED_NOTES_PREAMBLE
from vexic.mcp_presentation import (
    render_long_term,
    render_transcript_hits,
    server_instructions,
)


def _hit(message_id: int, body: str, timestamp: str | None = "2026-07-01T00:00:00Z") -> TranscriptHit:
    return TranscriptHit(
        message_id=message_id,
        session_id="session-a",
        timestamp=timestamp,
        body=body,
    )


def _fact(fact_text: str) -> LongTermFact:
    return LongTermFact(
        fact_id=12,
        fact_text=fact_text,
        subject="user",
        category=MemoryCategory.PREFERENCE,
        importance=5,
        confidence=0.9,
        source_message_ids=[256, 258],
        editable=True,
        created_at="2026-06-29T00:00:00Z",
    )


def _note(fact_text: str) -> CandidateNote:
    return CandidateNote(
        candidate_id=7,
        fact_text=fact_text,
        category=MemoryCategory.PREFERENCE,
        source_message_ids=[301],
        created_at="2026-07-01T00:00:00Z",
    )


class ServerInstructionsTests(unittest.TestCase):
    def test_default_instructions_direct_proactive_search_and_natural_presentation(self) -> None:
        instructions = server_instructions(False)

        self.assertIn("proactively", instructions)
        self.assertIn("recall_user_memory", instructions)
        self.assertIn("recall_conversation_history", instructions)
        self.assertIn("answer naturally", instructions)
        self.assertIn("No transcript append", instructions)
        self.assertIn("verbatim history expansion", instructions)
        self.assertNotIn("expand_history", instructions)

    def test_expand_enabled_instructions_mention_expand_history(self) -> None:
        instructions = server_instructions(True)

        self.assertIn("expand_history", instructions)
        self.assertIn("proactively", instructions)
        self.assertIn("No transcript append", instructions)


class RenderTranscriptHitsTests(unittest.TestCase):
    def test_default_rendering_omits_internal_metadata(self) -> None:
        text = render_transcript_hits([_hit(256, "my favourite pizza is pineapple")])

        self.assertIn("my favourite pizza is pineapple", text)
        self.assertIn("(2026-07-01T00:00:00Z)", text)
        self.assertNotIn("message_id", text)
        self.assertNotIn("session_id", text)
        self.assertNotIn("session-a", text)
        self.assertNotIn("[message", text)
        self.assertNotIn("256", text)

    def test_rendering_without_timestamp_has_no_empty_marker(self) -> None:
        text = render_transcript_hits([_hit(256, "no timestamp body", timestamp=None)])

        self.assertIn("- no timestamp body", text)
        self.assertNotIn("()", text)

    def test_message_ids_included_only_for_expand_history(self) -> None:
        text = render_transcript_hits(
            [_hit(256, "expandable body")],
            include_message_ids=True,
        )

        self.assertIn("[message 256, 2026-07-01T00:00:00Z]", text)
        self.assertIn("expand_history", text)
        self.assertIn("never show them to the user", text)

    def test_empty_hits_return_no_match_message(self) -> None:
        text = render_transcript_hits([])

        self.assertEqual(
            text,
            "No matching messages found in recorded conversation history.",
        )


class RenderLongTermTests(unittest.TestCase):
    def test_facts_render_without_ids_confidence_or_sources(self) -> None:
        text = render_long_term([_fact("The user's favourite pizza is pineapple")], [])

        self.assertIn("Long-term memory about the user:", text)
        self.assertIn("- The user's favourite pizza is pineapple (preference)", text)
        self.assertNotIn("fact_id", text)
        self.assertNotIn("confidence", text)
        self.assertNotIn("0.9", text)
        self.assertNotIn("256", text)
        self.assertNotIn("created_at", text)

    def test_candidate_notes_keep_tentative_semantics(self) -> None:
        text = render_long_term([], [_note("might prefer thin crust")])

        self.assertIn(UNVERIFIED_NOTES_PREAMBLE, text)
        self.assertIn("- tentative: might prefer thin crust (preference)", text)
        self.assertNotIn("candidate_id", text)
        self.assertNotIn("301", text)

    def test_facts_take_precedence_over_candidate_notes(self) -> None:
        text = render_long_term(
            [_fact("verified fact")],
            [_note("tentative note")],
        )

        self.assertIn("verified fact", text)
        self.assertNotIn("tentative note", text)
        self.assertNotIn(UNVERIFIED_NOTES_PREAMBLE, text)

    def test_empty_results_return_no_match_message(self) -> None:
        text = render_long_term([], [])

        self.assertEqual(text, "No long-term memories found for this query.")


if __name__ == "__main__":
    unittest.main()
