"""Vexic: local-first memory core for long-running AI agents.

Import surface:

- Contract symbols (``MemoryService``, ``MemoryScope``, request/result
  models, ...) are re-exported eagerly from :mod:`vexic.contract`. They are
  pydantic models and enums with no heavy dependencies.
- ``LocalMemoryService`` is exported lazily (PEP 562): ``from vexic import
  LocalMemoryService`` works, but ``import vexic`` alone does not load
  :mod:`vexic.service` or its storage/pipeline dependencies.

Quickstart::

    from vexic import LocalMemoryService, MemoryScope, MemoryCapability

    service = LocalMemoryService(db_path="memory.db", tenant_id="local")
    service.init_schema()
"""

from typing import Any

from vexic.contract import (
    CONTRACT_VERSION,
    AppendTranscriptRequest,
    AppendTranscriptResult,
    ExpandHistoryRequest,
    ExpandHistoryResult,
    FreshContextRequest,
    FreshContextResult,
    LoadActiveContextRequest,
    LoadActiveContextResult,
    IngestSourceTranscriptRequest,
    IngestSourceTranscriptResult,
    LongTermFact,
    MemoryCapability,
    MemoryCategory,
    MemoryScope,
    MemoryScopeSelector,
    MemoryService,
    Principal,
    PrincipalType,
    RedactionContext,
    SearchLongTermRequest,
    SearchLongTermResult,
    SearchTranscriptRequest,
    SearchTranscriptResult,
    SourceTranscriptMessage,
    TranscriptHit,
    TrustBoundary,
)

__version__ = "0.1.1"

__all__ = [
    "CONTRACT_VERSION",
    "AppendTranscriptRequest",
    "AppendTranscriptResult",
    "ExpandHistoryRequest",
    "ExpandHistoryResult",
    "FreshContextRequest",
    "FreshContextResult",
    "LoadActiveContextRequest",
    "LoadActiveContextResult",
    "IngestSourceTranscriptRequest",
    "IngestSourceTranscriptResult",
    "LocalMemoryService",
    "LongTermFact",
    "MemoryCapability",
    "MemoryCategory",
    "MemoryScope",
    "MemoryScopeSelector",
    "MemoryService",
    "Principal",
    "PrincipalType",
    "RedactionContext",
    "SearchLongTermRequest",
    "SearchLongTermResult",
    "SearchTranscriptRequest",
    "SearchTranscriptResult",
    "SourceTranscriptMessage",
    "TranscriptHit",
    "TrustBoundary",
]


def __getattr__(name: str) -> Any:
    # PEP 562 lazy export: vexic.service pulls in storage/pipeline (and,
    # transitively, pydantic_ai), so it must not load on `import vexic`.
    if name == "LocalMemoryService":
        from vexic.service import LocalMemoryService

        return LocalMemoryService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
