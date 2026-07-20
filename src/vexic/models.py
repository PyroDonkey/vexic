"""Structured outputs exchanged with the host's model adapters.

These are the pydantic shapes the dream-phase agents produce (extraction,
contradiction judging, query rewriting) plus the retrieval-side fact view.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_OCCURRED_AT_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")
_FULL_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_real_date(text: str) -> bool:
    """True if `text` is a real calendar date in YYYY-MM-DD form."""
    if not _FULL_DATE_RE.fullmatch(text):
        return False
    year, month, day = (int(part) for part in text.split("-"))
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


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

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _occurred_at_partial_iso_or_none(cls, value: object) -> str | None:
        # Fail-safe, mirroring storage._normalized_date: a malformed date must
        # never drop the candidate; it degrades to undated (ADR 0037 sink).
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if _OCCURRED_AT_RE.fullmatch(text):
            parts = [int(p) for p in text.split("-")]
            try:
                date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
            except ValueError:
                return None
            return text
        # Rehydration from persisted rows (src/vexic/storage/candidates.py) can surface
        # legacy datetime-shaped values, e.g. "2026-07-05T00:00:00Z". Truncate
        # to the date part instead of nulling it out: truncation only reduces
        # precision, it never invents components (Memory Invariant 11).
        if len(text) > 10 and text[10] in "T " and _is_real_date(text[:10]):
            return text[:10]
        return None


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
