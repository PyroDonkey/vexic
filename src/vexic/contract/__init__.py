"""Vexic memory service contract (v0.1.0).

The versioned public surface shared by every Vexic memory implementation:
the :class:`MemoryService` protocol, the request/result models for each
operation, and the supporting scope/capability/redaction types. Pure
pydantic models and enums -- importing this module pulls in no storage or
model-runtime dependencies.

Key types: :class:`MemoryService` (the operation protocol),
:class:`MemoryScope` (per-request authorization context), and
:class:`MemoryCapability` (the permission each operation requires).
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "0.1.0"

# Marker prefix stamped on every recorder-injected priming block (SessionStart
# recap/search context). Recorders and ingest paths use this as a substring
# guard so injected priming text can never re-enter Tier 1 transcript storage
# and, downstream, Light extraction (WI-6).
PRIME_CONTEXT_HEADER = "Vexic memory priming:"


class ContractVersion(StrEnum):
    """Supported contract versions; currently only v0.1."""
    V0_1 = CONTRACT_VERSION


class MemoryCategory(StrEnum):
    """Kind of durable memory a promoted fact belongs to."""
    PREFERENCE = "preference"
    FACT = "fact"
    GOAL = "goal"
    EVENT = "event"
    RELATIONSHIP = "relationship"
    SKILL = "skill"
    CONSTRAINT = "constraint"
    CONTEXT = "context"


class PrincipalType(StrEnum):
    """Who (or what) is acting: human, agent, service, operator, or system."""
    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"
    OPERATOR = "operator"
    SYSTEM = "system"


class TrustBoundary(StrEnum):
    """Where a caller sits: local trusted process or networked client."""
    LOCAL_TRUSTED = "local_trusted"
    NETWORKED = "networked"


class MemoryCapability(StrEnum):
    """Fine-grained permission strings gating each memory operation."""
    READ = "memory:read"
    WRITE = "memory:write"
    SEARCH = "memory:search"
    EXPAND_HISTORY = "memory:expand"
    FRESH_CONTEXT = "memory:fresh-context"
    EXPORT = "memory:export"
    REPLAY = "memory:replay"
    ADMIN_REBUILD = "memory:admin:rebuild"
    ADMIN_LIFECYCLE = "memory:admin:lifecycle"
    DREAM_TRIGGER = "memory:dream:trigger"


class EgressKind(StrEnum):
    """Classification of content leaving the store (audit/egress telemetry)."""
    EXPAND_HISTORY = "expand_history"
    EXPORT = "export"
    REPLAY = "replay"
    REBUILD_ARTIFACT = "rebuild_artifact"


class DreamPhase(StrEnum):
    """Background consolidation phases: light, rem, deep, summarize."""
    LIGHT = "light"
    REM = "rem"
    DEEP = "deep"
    SUMMARIZE = "summarize"


class LifecycleAction(StrEnum):
    """Scope lifecycle steps: retire, tombstone, deferred purge, purge."""
    RETIRE = "retire"
    TOMBSTONE_SCOPE = "tombstone_scope"
    PURGE_DEFERRED = "purge_deferred"
    PURGE = "purge"


class MemoryContractModel(BaseModel):
    """Base model for all contract types: extra fields forbidden, enums kept as enums."""
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Principal(MemoryContractModel):
    """The acting identity attached to a scope: an id plus its type."""
    principal_id: str = Field(min_length=1)
    principal_type: PrincipalType

    @field_validator("principal_id")
    @classmethod
    def _principal_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("principal_id must not be blank.")
        return value


def _scope_identifier_must_not_be_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("scope identifiers must not be blank.")
    return value


class MemoryScope(MemoryContractModel):
    """Authorization context for a request: tenant (required), optional
    project/user/session/agent ids, the acting principal, trust boundary,
    and the capability set the caller holds.
    """
    tenant_id: str = Field(min_length=1)
    project_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    principal: Principal
    trust_boundary: TrustBoundary
    capabilities: set[MemoryCapability] = Field(default_factory=set)
    correlation_id: str | None = None

    @field_validator(
        "tenant_id",
        "project_id",
        "user_id",
        "session_id",
        "agent_id",
        "correlation_id",
    )
    @classmethod
    def _ids_must_not_be_blank(cls, value: str | None) -> str | None:
        return _scope_identifier_must_not_be_blank(value)


class MemoryScopeSelector(MemoryContractModel):
    """Scope pattern used to target rows for lifecycle operations; ``None``
    fields are wildcards within the tenant.
    """
    tenant_id: str = Field(min_length=1)
    project_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None

    @field_validator(
        "tenant_id",
        "project_id",
        "user_id",
        "session_id",
        "agent_id",
    )
    @classmethod
    def _ids_must_not_be_blank(cls, value: str | None) -> str | None:
        return _scope_identifier_must_not_be_blank(value)


class RedactionContext(MemoryContractModel):
    """Secret values that must never appear in stored or egressed content."""
    forbidden_values: tuple[str, ...]


def require_capability(scope: MemoryScope, capability: MemoryCapability) -> None:
    if capability not in scope.capabilities:
        raise PermissionError(f"Memory capability required: {capability.value}")


class MemoryRequest(MemoryContractModel):
    """Base request: pins the contract version and carries the caller's scope."""
    contract_version: Literal["0.1.0"] = CONTRACT_VERSION
    scope: MemoryScope


class SessionScopedRequest(MemoryRequest):
    """Request whose scope must include a ``session_id``."""
    @model_validator(mode="after")
    def _scope_must_include_session_id(self) -> Self:
        if self.scope.session_id is None:
            raise ValueError("scope.session_id is required for this operation.")
        return self


class RedactionRequiredRequest(MemoryRequest):
    """Request that must carry a ``RedactionContext``."""
    redaction: RedactionContext


class SessionScopedRedactionRequiredRequest(RedactionRequiredRequest):
    """Request requiring both a ``session_id`` and a ``RedactionContext``."""
    @model_validator(mode="after")
    def _scope_must_include_session_id(self) -> Self:
        if self.scope.session_id is None:
            raise ValueError("scope.session_id is required for this operation.")
        return self


class MemoryResult(MemoryContractModel):
    """Base result: echoes the contract version."""
    contract_version: Literal["0.1.0"] = CONTRACT_VERSION


class TranscriptHit(MemoryContractModel):
    """One transcript message matched by search or replay."""
    message_id: int
    session_id: str
    timestamp: str | None = None
    body: str


class LongTermFact(MemoryContractModel):
    """A promoted durable fact, traceable to its source message ids."""
    fact_id: int
    fact_text: str
    subject: str
    category: MemoryCategory
    importance: int
    confidence: float
    source_message_ids: list[int]
    editable: bool
    created_at: str
    retrieved_count: int = 0
    used_count: int = 0
    # Event time (partial-precision ISO) for category="event" facts; None otherwise.
    occurred_at: str | None = None


class CandidateNote(MemoryContractModel):
    """A staged (not yet promoted) memory candidate."""
    candidate_id: int
    fact_text: str
    category: MemoryCategory
    source_message_ids: list[int]
    created_at: str


class RetrievalEvent(MemoryContractModel):
    """Record of one fact retrieval and its eventual usefulness verdict."""
    event_id: int
    referent_id: int
    session_id: str
    query: str
    retrieved_at: str
    used: bool | None = None
    judged_at: str | None = None


class SummaryNode(MemoryContractModel):
    """A compacted summary covering a contiguous transcript span."""
    summary_id: int
    session_id: str
    first_message_id: int
    last_message_id: int
    summary_text: str
    token_estimate: int
    created_at: str


class AppendTranscriptRequest(SessionScopedRedactionRequiredRequest):
    """Append serialized messages to a session transcript (requires WRITE)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    messages_json: list[str]


class AppendTranscriptResult(MemoryResult):
    """Ids assigned to the appended messages."""
    message_ids: list[int]


class SourceTranscriptMessage(MemoryContractModel):
    """One externally-recorded message with its source coordinates for dedup."""
    source_host: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    message_json: str

    @field_validator("source_host", "source_session_id", "source_message_id")
    @classmethod
    def _source_ids_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source identifiers must not be blank.")
        return value


class SourceTranscriptIngestItemResult(MemoryContractModel):
    """Per-message ingest outcome: inserted, skipped, or rejected."""
    source_host: str
    source_session_id: str
    source_message_id: str
    status: Literal["inserted", "skipped", "rejected"]
    message_id: int | None = None
    reason: str | None = None
    warning: str | None = None


class IngestSourceTranscriptRequest(SessionScopedRedactionRequiredRequest):
    """Idempotently ingest recorder-captured messages (requires WRITE)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    messages: list[SourceTranscriptMessage]


class IngestSourceTranscriptResult(MemoryResult):
    """Per-item outcomes for an ingest batch."""
    items: list[SourceTranscriptIngestItemResult]


class SearchTranscriptRequest(SessionScopedRequest):
    """Full-text search within one session's transcript (requires SEARCH)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.SEARCH
    query: str
    limit: int = 5


class SearchTranscriptResult(MemoryResult):
    """Transcript messages matching the query."""
    hits: list[TranscriptHit]


class ExpandHistoryRequest(SessionScopedRedactionRequiredRequest):
    """Fetch a verbatim message-id range from the transcript (requires EXPAND_HISTORY)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.EXPAND_HISTORY
    first_message_id: int
    last_message_id: int


class ExpandHistoryResult(MemoryResult):
    """Rendered transcript text for the requested range."""
    egress_kind: EgressKind = EgressKind.EXPAND_HISTORY
    text: str
    truncated: bool = False


class FreshContextRequest(SessionScopedRedactionRequiredRequest):
    """Build session-start priming context within a token budget (requires FRESH_CONTEXT)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.FRESH_CONTEXT
    token_budget: int = 6_000


class FreshContextResult(MemoryResult):
    """Summaries plus recent messages rendered as priming text."""
    summaries: list[SummaryNode] = Field(default_factory=list)
    recent: list[TranscriptHit] = Field(default_factory=list)
    text: str
    truncated: bool = False


class SearchLongTermRequest(MemoryRequest):
    """Semantic search over durable facts and candidates (requires SEARCH)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.SEARCH
    query: str
    limit: int = 5
    as_of: str | None = None
    event_after: str | None = None
    event_before: str | None = None


class SearchLongTermResult(MemoryResult):
    """Matching facts, with candidate notes as fallback."""
    facts: list[LongTermFact] = Field(default_factory=list)
    candidate_notes: list[CandidateNote] = Field(default_factory=list)


class RecordRetrievalEventRequest(RedactionRequiredRequest):
    """Persist a retrieval event or usefulness verdict (requires WRITE)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    event: RetrievalEvent


class RecordRetrievalEventResult(MemoryResult):
    """Id of the recorded retrieval event."""
    event_id: int


class RetireFactRequest(MemoryRequest):
    """Soft-retire a fact, optionally naming its successor (requires WRITE)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    fact_id: int
    superseded_by_fact_id: int | None = None
    redaction: None = None


class RetireFactResult(MemoryResult):
    """Whether the fact was retired."""
    retired: bool


class RunDreamPhaseRequest(RedactionRequiredRequest):
    """Execute one consolidation phase directly (requires ADMIN_REBUILD)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.ADMIN_REBUILD
    phase: DreamPhase


class RunDreamPhaseResult(MemoryResult):
    """Phase outcome: ok, error, or partial."""
    phase: DreamPhase
    status: Literal["ok", "error", "partial"]


class TriggerDreamPhaseRequest(MemoryRequest):
    """Boundary request for ``POST /v1/trigger_dream_phase`` (ADR 0025).

    Deliberately thin: it carries its own capability (``DREAM_TRIGGER``, not
    ``ADMIN_REBUILD``) so trigger-only keys (e.g. the recorder/cron caller)
    never need the heavier admin capability. The hosted service authenticates
    and binds this request exactly once at the trigger boundary, then
    internally mints a fully-scoped ``RunDreamPhaseRequest`` (server-side
    ``ADMIN_REBUILD``) to execute the phase directly -- see
    ``HostedMemoryService.trigger_dream_phase``.

    ``scope.session_id`` is intentionally not required: v1 summarize sweeps
    all compactable sessions tenant(+agent)-wide (see plan D1's honest-scope
    note), not a single session.
    """

    required_capability: ClassVar[MemoryCapability] = MemoryCapability.DREAM_TRIGGER
    phase: DreamPhase

    @model_validator(mode="after")
    def _v1_restricts_to_summarize(self) -> Self:
        if self.phase is not DreamPhase.SUMMARIZE:
            raise ValueError(
                "trigger_dream_phase only supports phase='summarize' in v1."
            )
        return self


class TriggerDreamPhaseResult(MemoryResult):
    status: Literal["scheduled", "skipped"]
    reason: str | None = None


class ExportScopeRequest(RedactionRequiredRequest):
    """Export all scope content to an artifact (requires EXPORT)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.EXPORT
    egress_kind: EgressKind = EgressKind.EXPORT


class ExportScopeResult(MemoryResult):
    """Reference to the written export artifact."""
    artifact_ref: str


class ReplayScopeRequest(RedactionRequiredRequest):
    """Return the scope's transcript verbatim (requires REPLAY)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.REPLAY
    egress_kind: EgressKind = EgressKind.REPLAY


class ReplayScopeResult(MemoryResult):
    """The replayed transcript messages."""
    messages: list[TranscriptHit]


class RebuildRequest(RedactionRequiredRequest):
    """Rebuild derived projections from canonical rows (requires ADMIN_REBUILD)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.ADMIN_REBUILD
    return_artifacts: bool = False


class RebuildResult(MemoryResult):
    """Optional artifact reference from the rebuild."""
    artifact_ref: str | None = None


class DeleteScopeRequest(RedactionRequiredRequest):
    """Tombstone a scope so it is blocked from retrieval/egress (requires ADMIN_LIFECYCLE)."""
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.ADMIN_LIFECYCLE
    target_scope: MemoryScopeSelector
    reason: str

    @model_validator(mode="after")
    def _target_scope_must_match_actor_tenant(self) -> Self:
        if self.target_scope.tenant_id != self.scope.tenant_id:
            raise ValueError("target_scope.tenant_id must match scope.tenant_id.")
        return self


class TombstoneRecord(MemoryContractModel):
    """A scope tombstone: what is blocked, by whom, and why."""
    tombstone_id: str
    target_scope: MemoryScopeSelector
    created_by: Principal
    reason: str
    retrieval_blocked: bool
    export_blocked: bool
    replay_blocked: bool
    rebuild_blocked: bool
    physical_purge_deferred: bool


class DeleteScopeResult(MemoryResult):
    """The tombstone created for the deleted scope."""
    tombstone: TombstoneRecord


class PurgeScopeRequest(RedactionRequiredRequest):
    """Physically erase a previously tombstoned scope (ADR 0022).

    Purge is the second deliberate step after ``delete_scope``: it requires an
    existing tombstone for exactly this target and irreversibly deletes the
    scope's canonical rows, projections, and content-bearing telemetry from
    the primary database. Provider backups persist until their own retention
    expires.
    """

    required_capability: ClassVar[MemoryCapability] = MemoryCapability.ADMIN_LIFECYCLE
    target_scope: MemoryScopeSelector
    reason: str
    dry_run: bool = False

    @model_validator(mode="after")
    def _target_scope_must_match_actor_tenant(self) -> Self:
        if self.target_scope.tenant_id != self.scope.tenant_id:
            raise ValueError("target_scope.tenant_id must match scope.tenant_id.")
        return self


class PurgeScopeResult(MemoryResult):
    """Row counts physically purged (or that would be, on dry run)."""
    tombstone_id: str
    purged: dict[str, int]
    dry_run: bool
    purged_at: str | None = None


@runtime_checkable
class MemoryService(Protocol):
    """The Vexic memory service contract.

    Structural (``Protocol``) and runtime-checkable: any object exposing
    these async operations satisfies it. ``LocalMemoryService`` is the
    SQLite reference implementation; hosted adapters implement the same
    surface. Every operation takes a versioned request model carrying a
    ``MemoryScope`` and returns the matching result model.
    """
    async def append_transcript(
        self,
        request: AppendTranscriptRequest,
    ) -> AppendTranscriptResult: ...

    async def ingest_source_transcript(
        self,
        request: IngestSourceTranscriptRequest,
    ) -> IngestSourceTranscriptResult: ...

    async def search_transcript(
        self,
        request: SearchTranscriptRequest,
    ) -> SearchTranscriptResult: ...

    async def expand_history(
        self,
        request: ExpandHistoryRequest,
    ) -> ExpandHistoryResult: ...

    async def fresh_context(
        self,
        request: FreshContextRequest,
    ) -> FreshContextResult: ...

    async def search_long_term(
        self,
        request: SearchLongTermRequest,
    ) -> SearchLongTermResult: ...

    async def record_retrieval_event(
        self,
        request: RecordRetrievalEventRequest,
    ) -> RecordRetrievalEventResult: ...

    async def retire_fact(
        self,
        request: RetireFactRequest,
    ) -> RetireFactResult: ...

    async def run_dream_phase(
        self,
        request: RunDreamPhaseRequest,
    ) -> RunDreamPhaseResult: ...

    async def export_scope(
        self,
        request: ExportScopeRequest,
    ) -> ExportScopeResult: ...

    async def replay_scope(
        self,
        request: ReplayScopeRequest,
    ) -> ReplayScopeResult: ...

    async def rebuild(
        self,
        request: RebuildRequest,
    ) -> RebuildResult: ...

    async def delete_scope(
        self,
        request: DeleteScopeRequest,
    ) -> DeleteScopeResult: ...

    async def purge_scope(
        self,
        request: PurgeScopeRequest,
    ) -> PurgeScopeResult: ...
