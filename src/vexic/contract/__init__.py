from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "0.1.0"


class ContractVersion(StrEnum):
    V0_1 = CONTRACT_VERSION


class MemoryCategory(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    GOAL = "goal"
    EVENT = "event"
    RELATIONSHIP = "relationship"
    SKILL = "skill"
    CONSTRAINT = "constraint"
    CONTEXT = "context"


class PrincipalType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"
    OPERATOR = "operator"
    SYSTEM = "system"


class TrustBoundary(StrEnum):
    LOCAL_TRUSTED = "local_trusted"
    NETWORKED = "networked"


class MemoryCapability(StrEnum):
    READ = "memory:read"
    WRITE = "memory:write"
    SEARCH = "memory:search"
    EXPAND_HISTORY = "memory:expand"
    EXPORT = "memory:export"
    REPLAY = "memory:replay"
    ADMIN_REBUILD = "memory:admin:rebuild"
    ADMIN_LIFECYCLE = "memory:admin:lifecycle"


class EgressKind(StrEnum):
    EXPAND_HISTORY = "expand_history"
    EXPORT = "export"
    REPLAY = "replay"
    REBUILD_ARTIFACT = "rebuild_artifact"


class DreamPhase(StrEnum):
    LIGHT = "light"
    REM = "rem"
    DEEP = "deep"


class LifecycleAction(StrEnum):
    RETIRE = "retire"
    TOMBSTONE_SCOPE = "tombstone_scope"
    PURGE_DEFERRED = "purge_deferred"


class MemoryContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Principal(MemoryContractModel):
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
    forbidden_values: tuple[str, ...]


def require_capability(scope: MemoryScope, capability: MemoryCapability) -> None:
    if capability not in scope.capabilities:
        raise PermissionError(f"Memory capability required: {capability.value}")


class MemoryRequest(MemoryContractModel):
    contract_version: Literal["0.1.0"] = CONTRACT_VERSION
    scope: MemoryScope


class SessionScopedRequest(MemoryRequest):
    @model_validator(mode="after")
    def _scope_must_include_session_id(self) -> Self:
        if self.scope.session_id is None:
            raise ValueError("scope.session_id is required for this operation.")
        return self


class RedactionRequiredRequest(MemoryRequest):
    redaction: RedactionContext


class SessionScopedRedactionRequiredRequest(RedactionRequiredRequest):
    @model_validator(mode="after")
    def _scope_must_include_session_id(self) -> Self:
        if self.scope.session_id is None:
            raise ValueError("scope.session_id is required for this operation.")
        return self


class MemoryResult(MemoryContractModel):
    contract_version: Literal["0.1.0"] = CONTRACT_VERSION


class TranscriptHit(MemoryContractModel):
    message_id: int
    session_id: str
    timestamp: str | None = None
    body: str


class LongTermFact(MemoryContractModel):
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


class CandidateNote(MemoryContractModel):
    candidate_id: int
    fact_text: str
    category: MemoryCategory
    source_message_ids: list[int]
    created_at: str


class RetrievalEvent(MemoryContractModel):
    event_id: int
    referent_id: int
    session_id: str
    query: str
    retrieved_at: str
    used: bool | None = None
    judged_at: str | None = None


class SummaryNode(MemoryContractModel):
    summary_id: int
    session_id: str
    first_message_id: int
    last_message_id: int
    summary_text: str
    token_estimate: int
    created_at: str


class AppendTranscriptRequest(SessionScopedRedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    messages_json: list[str]


class AppendTranscriptResult(MemoryResult):
    message_ids: list[int]


class SourceTranscriptMessage(MemoryContractModel):
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
    source_host: str
    source_session_id: str
    source_message_id: str
    status: Literal["inserted", "skipped", "rejected"]
    message_id: int | None = None
    reason: str | None = None
    warning: str | None = None


class IngestSourceTranscriptRequest(SessionScopedRedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    messages: list[SourceTranscriptMessage]


class IngestSourceTranscriptResult(MemoryResult):
    items: list[SourceTranscriptIngestItemResult]


class SearchTranscriptRequest(SessionScopedRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.SEARCH
    query: str
    limit: int = 5


class SearchTranscriptResult(MemoryResult):
    hits: list[TranscriptHit]


class ExpandHistoryRequest(SessionScopedRedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.EXPAND_HISTORY
    first_message_id: int
    last_message_id: int


class ExpandHistoryResult(MemoryResult):
    egress_kind: EgressKind = EgressKind.EXPAND_HISTORY
    text: str
    truncated: bool = False


class SearchLongTermRequest(MemoryRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.SEARCH
    query: str
    limit: int = 5


class SearchLongTermResult(MemoryResult):
    facts: list[LongTermFact] = Field(default_factory=list)
    candidate_notes: list[CandidateNote] = Field(default_factory=list)


class RecordRetrievalEventRequest(RedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    event: RetrievalEvent


class RecordRetrievalEventResult(MemoryResult):
    event_id: int


class RetireFactRequest(MemoryRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.WRITE
    fact_id: int
    superseded_by_fact_id: int | None = None
    redaction: None = None


class RetireFactResult(MemoryResult):
    retired: bool


class RunDreamPhaseRequest(RedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.ADMIN_REBUILD
    phase: DreamPhase


class RunDreamPhaseResult(MemoryResult):
    phase: DreamPhase
    status: Literal["ok", "error", "partial"]


class ExportScopeRequest(RedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.EXPORT
    egress_kind: EgressKind = EgressKind.EXPORT


class ExportScopeResult(MemoryResult):
    artifact_ref: str


class ReplayScopeRequest(RedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.REPLAY
    egress_kind: EgressKind = EgressKind.REPLAY


class ReplayScopeResult(MemoryResult):
    messages: list[TranscriptHit]


class RebuildRequest(RedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.ADMIN_REBUILD
    return_artifacts: bool = False


class RebuildResult(MemoryResult):
    artifact_ref: str | None = None


class DeleteScopeRequest(RedactionRequiredRequest):
    required_capability: ClassVar[MemoryCapability] = MemoryCapability.ADMIN_LIFECYCLE
    target_scope: MemoryScopeSelector
    reason: str

    @model_validator(mode="after")
    def _target_scope_must_match_actor_tenant(self) -> Self:
        if self.target_scope.tenant_id != self.scope.tenant_id:
            raise ValueError("target_scope.tenant_id must match scope.tenant_id.")
        return self


class TombstoneRecord(MemoryContractModel):
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
    tombstone: TombstoneRecord


@runtime_checkable
class MemoryService(Protocol):
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
