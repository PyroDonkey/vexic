"""Shared memory retrieval presentation helpers."""

from vexic.storage import CandidateNote

UNVERIFIED_NOTES_PREAMBLE = (
    "No durable long-term memories matched. Found lower-confidence recent notes "
    "not yet verified by the memory consolidation pass — treat as tentative and "
    "confirm with the user before relying on them:"
)


def format_candidate_note(note: CandidateNote) -> str:
    sources = ", ".join(str(message_id) for message_id in note.source_message_ids)
    return (
        f"[unverified note] {note.fact_text}\n"
        f"(category: {note.category}, recently noted, not yet confirmed, "
        f"source messages: {sources})"
    )
