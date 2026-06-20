from __future__ import annotations

from vexic.contract import (
    AppendTranscriptRequest,
    AppendTranscriptResult,
    CandidateNote as ContractCandidateNote,
    DeleteScopeRequest,
    DeleteScopeResult,
    ExpandHistoryRequest,
    ExpandHistoryResult,
    ExportScopeRequest,
    ExportScopeResult,
    MemoryCapability,
    MemoryCategory,
    MemoryScope,
    MemoryService,
    RecordRetrievalEventRequest,
    RecordRetrievalEventResult,
    RedactionContext,
    ReplayScopeRequest,
    ReplayScopeResult,
    RebuildRequest,
    RebuildResult,
    RetireFactRequest,
    RetireFactResult,
    RunDreamPhaseRequest,
    RunDreamPhaseResult,
    SearchLongTermRequest,
    SearchLongTermResult,
    SearchTranscriptRequest,
    SearchTranscriptResult,
    TranscriptHit,
    LongTermFact as ContractLongTermFact,
    require_capability,
)
from vexic.ports import EmbedTexts
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import (
    TranscriptRangeTooLarge,
    init_db,
    load_messages_in_id_range,
    save_messages,
    search_messages,
    single_message_adapter,
)
from vexic.subagents.retrieval import retrieve_candidate_fallback, retrieve_long_term_facts

EXPAND_HISTORY_MAX_ROWS = 2_000


class LocalMemoryService(MemoryService):
    def __init__(
        self,
        *,
        db_path: str,
        tenant_id: str,
        forbidden_secret_values: tuple[str, ...] = (),
        embed: EmbedTexts | None = None,
    ) -> None:
        self.db_path = db_path
        self.tenant_id = tenant_id
        self.forbidden_secret_values = forbidden_secret_values
        self.embed = embed

    def init_schema(self) -> None:
        init_db(self.db_path)

    def _authorize(self, scope: MemoryScope, capability: MemoryCapability) -> None:
        if scope.tenant_id != self.tenant_id:
            raise PermissionError("Memory scope tenant_id does not match opened database.")
        require_capability(scope, capability)

    def _redaction_values(self, redaction: RedactionContext) -> tuple[str, ...]:
        return (*self.forbidden_secret_values, *redaction.forbidden_values)

    async def append_transcript(
        self,
        request: AppendTranscriptRequest,
    ) -> AppendTranscriptResult:
        self._authorize(request.scope, request.required_capability)
        messages = [single_message_adapter.validate_json(raw) for raw in request.messages_json]
        message_ids = save_messages(
            self.db_path,
            messages,
            session_id=request.scope.session_id or "default",
            forbidden_secret_values=self._redaction_values(request.redaction),
        )
        return AppendTranscriptResult(message_ids=message_ids)

    async def search_transcript(
        self,
        request: SearchTranscriptRequest,
    ) -> SearchTranscriptResult:
        self._authorize(request.scope, request.required_capability)
        hits = search_messages(
            self.db_path,
            request.query,
            session_id=request.scope.session_id or "default",
            limit=request.limit,
        )
        return SearchTranscriptResult(
            hits=[
                TranscriptHit(
                    message_id=hit.message_id,
                    session_id=request.scope.session_id or "default",
                    timestamp=hit.timestamp,
                    body=hit.body,
                )
                for hit in hits
            ]
        )

    async def expand_history(
        self,
        request: ExpandHistoryRequest,
    ) -> ExpandHistoryResult:
        self._authorize(request.scope, request.required_capability)
        try:
            rows = load_messages_in_id_range(
                self.db_path,
                request.first_message_id,
                request.last_message_id,
                session_id=request.scope.session_id or "default",
                max_rows=EXPAND_HISTORY_MAX_ROWS,
            )
        except TranscriptRangeTooLarge:
            return ExpandHistoryResult(text="", truncated=True)
        text = "\n\n".join(
            f"[message {hit.message_id} @ {hit.timestamp}]\n{hit.body}"
            if hit.timestamp
            else f"[message {hit.message_id}]\n{hit.body}"
            for hit in rows
        )
        assert_no_forbidden_secret_values(
            self._redaction_values(request.redaction), text
        )
        return ExpandHistoryResult(text=text)

    async def search_long_term(
        self,
        request: SearchLongTermRequest,
    ) -> SearchLongTermResult:
        self._authorize(request.scope, request.required_capability)
        facts = await retrieve_long_term_facts(
            self.db_path,
            request.query,
            session_id=request.scope.session_id or "default",
            return_k=request.limit,
            embed=self.embed,
        )
        if facts:
            return SearchLongTermResult(
                facts=[
                    ContractLongTermFact(
                        fact_id=fact.fact_id,
                        fact_text=fact.fact_text,
                        subject=fact.subject,
                        category=MemoryCategory(fact.category),
                        importance=fact.importance,
                        confidence=fact.confidence,
                        source_message_ids=fact.source_message_ids,
                        editable=fact.editable,
                        created_at=fact.created_at,
                        retrieved_count=fact.retrieved_count,
                        used_count=fact.used_count,
                    )
                    for fact in facts
                ]
            )

        notes = await retrieve_candidate_fallback(
            self.db_path,
            request.query,
            session_id=request.scope.session_id or "default",
            return_k=request.limit,
            embed=self.embed,
        )
        return SearchLongTermResult(
            candidate_notes=[
                ContractCandidateNote(
                    candidate_id=note.candidate_id,
                    fact_text=note.fact_text,
                    category=MemoryCategory(note.category),
                    source_message_ids=note.source_message_ids,
                    created_at=note.created_at,
                )
                for note in notes
            ]
        )

    async def record_retrieval_event(
        self,
        request: RecordRetrievalEventRequest,
    ) -> RecordRetrievalEventResult:
        self._authorize(request.scope, request.required_capability)
        raise NotImplementedError("record_retrieval_event is not a standalone v0.1 operation.")

    async def retire_fact(
        self,
        request: RetireFactRequest,
    ) -> RetireFactResult:
        self._authorize(request.scope, request.required_capability)
        raise NotImplementedError("retire_fact is wired in the lifecycle slice.")

    async def run_dream_phase(
        self,
        request: RunDreamPhaseRequest,
    ) -> RunDreamPhaseResult:
        self._authorize(request.scope, request.required_capability)
        raise NotImplementedError("run_dream_phase is wired in the dream slice.")

    async def export_scope(
        self,
        request: ExportScopeRequest,
    ) -> ExportScopeResult:
        self._authorize(request.scope, request.required_capability)
        raise NotImplementedError("export_scope is wired in the admin slice.")

    async def replay_scope(
        self,
        request: ReplayScopeRequest,
    ) -> ReplayScopeResult:
        self._authorize(request.scope, request.required_capability)
        raise NotImplementedError("replay_scope is wired in the admin slice.")

    async def rebuild(
        self,
        request: RebuildRequest,
    ) -> RebuildResult:
        self._authorize(request.scope, request.required_capability)
        raise NotImplementedError("rebuild is wired in the admin slice.")

    async def delete_scope(
        self,
        request: DeleteScopeRequest,
    ) -> DeleteScopeResult:
        self._authorize(request.scope, request.required_capability)
        raise NotImplementedError("delete_scope is deferred for v0.1 local core.")
