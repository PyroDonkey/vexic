"""Model-facing presentation layer for the MCP memory surfaces.

Single source of truth for tool names, descriptions, annotations, server
instructions, and prose result rendering shared by the stdio and HTTP MCP
servers. The REST ``/v1`` endpoints remain the machine-readable contract;
MCP tool text is written for a model to read and relay naturally.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vexic.contract import CandidateNote, LongTermFact, TranscriptHit
from vexic.formatting import UNVERIFIED_NOTES_PREAMBLE

RECALL_CONVERSATION_HISTORY = "recall_conversation_history"
RECALL_USER_MEMORY = "recall_user_memory"
EXPAND_HISTORY = "expand_history"

TOOL_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

RECALL_CONVERSATION_HISTORY_DESCRIPTION = (
    "Search recorded conversation history with this user — the current "
    "conversation and earlier ones. Use proactively whenever the user "
    "references something said before ('as I mentioned', 'do you remember', "
    "'what did we decide last time') or asks about prior discussions or work. "
    "Read-only; does not expose verbatim history dumps or write memory."
)

RECALL_USER_MEMORY_DESCRIPTION = (
    "Search the user's durable long-term memory: stated preferences, personal "
    "facts, goals, decisions, and project context from past sessions. Use "
    "proactively whenever the user asks anything their history could answer — "
    "even if they don't mention memory, and before saying you don't know "
    "something about them. Returns verified facts, or clearly marked tentative "
    "notes when nothing verified matches. Read-only."
)

EXPAND_HISTORY_DESCRIPTION = (
    "Privileged verbatim expansion of a bounded range in the configured "
    "session transcript. Use only after recall_conversation_history has "
    "located the relevant messages; pass its message ids to read the "
    "surrounding verbatim context. Requires explicit local server opt-in."
)

_MESSAGE_ID_FOOTNOTE = (
    "Message ids are internal references for expand_history only — never show "
    "them to the user."
)

PRESENTATION_REMINDER = (
    "(Answer in your own words as if you simply remember this. Don't mention "
    "searching, memory tools, transcripts, or where the information came from "
    "unless the user asks.)"
)

_UNAVAILABLE = (
    "This memory surface is read-only. No transcript append, verbatim history "
    "expansion, export, delete, rebuild, or admin tools are available."
)

_UNAVAILABLE_WITH_EXPAND = (
    "This memory surface is read-only apart from expand_history, which is "
    "reserved for bounded privileged verbatim history egress. No transcript "
    "append, export, delete, rebuild, or admin tools are available."
)


def server_instructions(enable_expand_history: bool = False) -> str:
    closing = _UNAVAILABLE_WITH_EXPAND if enable_expand_history else _UNAVAILABLE
    return (
        "Vexic gives you persistent memory about this user and their projects "
        "across sessions.\n"
        "\n"
        "WHEN TO SEARCH (proactively, without being asked): the user asks about "
        "their preferences, personal facts, goals, or past decisions; the user "
        "references earlier conversation ('as I mentioned', 'do you remember', "
        "'last time'); or you are about to say you don't know something about "
        "the user — search first. Use recall_user_memory for durable facts and "
        "preferences, recall_conversation_history for what was said in current "
        "or earlier conversations.\n"
        "\n"
        "HOW TO PRESENT RESULTS: answer naturally in your own words, as if you "
        "simply remember. Never mention that you searched, looked something up, "
        "or used a tool; never mention transcripts, memory systems, priming, "
        "sessions, prior turns, or whether something is saved to long-term "
        "memory. Never show the user tool names, message ids, fact ids, raw "
        "timestamps, confidence scores, or raw result text. When timing matters "
        "to the answer, phrase dates as natural prose ('back in early July', "
        "'a few weeks ago') — never in metadata form. Mention where or when you "
        "learned something only if the user explicitly asks. Treat results "
        "marked tentative/unverified as uncertain and confirm with the user "
        "before relying on them.\n"
        "\n"
        f"{closing}"
    )


def render_transcript_hits(
    hits: Sequence[TranscriptHit],
    *,
    include_message_ids: bool = False,
) -> str:
    if not hits:
        return "No matching messages found in recorded conversation history."
    count = len(hits)
    plural = "message" if count == 1 else "messages"
    lines = [
        f"Found {count} matching {plural} in recorded conversation history "
        "(most relevant first):",
        "",
    ]
    for hit in hits:
        if include_message_ids:
            timestamp = f", {hit.timestamp}" if hit.timestamp else ""
            marker = f"[message {hit.message_id}{timestamp}] "
        elif hit.timestamp:
            marker = f"({hit.timestamp}) "
        else:
            marker = ""
        lines.append(f"- {marker}{hit.body}")
    if include_message_ids:
        lines.extend(["", _MESSAGE_ID_FOOTNOTE])
    lines.extend(["", PRESENTATION_REMINDER])
    return "\n".join(lines)


def render_long_term(
    facts: Sequence[LongTermFact],
    candidate_notes: Sequence[CandidateNote],
) -> str:
    if facts:
        lines = ["Long-term memory about the user:", ""]
        lines.extend(f"- {fact.fact_text} ({fact.category.value})" for fact in facts)
    elif candidate_notes:
        lines = [UNVERIFIED_NOTES_PREAMBLE, ""]
        lines.extend(
            f"- tentative: {note.fact_text} ({note.category.value})"
            for note in candidate_notes
        )
    else:
        return "No long-term memories found for this query."
    lines.extend(["", PRESENTATION_REMINDER])
    return "\n".join(lines)
