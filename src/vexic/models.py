"""Structured outputs exchanged with the host's model adapters.

These are the pydantic shapes the dream-phase agents produce (extraction,
contradiction judging, query rewriting) plus the retrieval-side fact view.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FactCandidate(BaseModel):
    """A memory candidate extracted from transcript messages by the Light phase."""

    fact_text: str
    subject: str
    category: Literal[
        "preference",
        "fact",
        "goal",
        "event",
        "relationship",
        "skill",
        "constraint",
        "context",
    ]
    importance: int = Field(ge=1, le=10)
    confidence: float = Field(ge=0.0, le=1.0)
    timestamp: int = 0
    source_message_ids: list[int] = Field(default_factory=list)
    editable: bool = True
    occurred_at: str | None = None


class ContradictionJudgment(BaseModel):
    """REM-phase verdict on whether a candidate contradicts an existing fact."""

    contradicts: bool
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class QueryRewrite(BaseModel):
    """Rewritten search terms produced by the retrieval query-rewrite agent."""

    search_terms: str


class RetrievedFact(BaseModel):
    """An immutable fact row as returned by long-term retrieval."""

    model_config = ConfigDict(frozen=True)

    fact_id: int
    fact_text: str
    event_id: int
