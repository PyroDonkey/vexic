from __future__ import annotations

import sqlite3
from contextlib import closing

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
    IngestSourceTranscriptRequest,
    IngestSourceTranscriptResult,
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
    SourceTranscriptIngestItemResult,
    TranscriptHit,
    LongTermFact as ContractLongTermFact,
    require_capability,
)
from vexic.ports import EmbedTexts
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import (
    TranscriptRangeTooLarge,
    SourceTranscriptInput,
    ingest_source_messages,
    init_db,
    load_messages_in_id_range,
    message_search_text,
    save_messages,
    search_messages,
    single_message_adapter,
)
from vexic.subagents.retrieval import retrieve_candidate_fallback, retrieve_long_term_facts

EXPAND_HISTORY_MAX_ROWS = 2_000
_TOMBSTONE_FLAG_COLUMNS = {
    "retrieval": "retrieval_blocked",
    "export": "export_blocked",
    "replay": "replay_blocked",
    "rebuild": "rebuild_blocked",
}


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

    def _with_default_session(self, scope: MemoryScope) -> MemoryScope:
        if scope.session_id is not None:
            return scope
        return scope.model_copy(update={"session_id": "default"})

    def _scope_matches_tombstone(self, scope: MemoryScope, row: sqlite3.Row) -> bool:
        for field_name, column_name in (
            ("project_id", "target_project_id"),
            ("user_id", "target_user_id"),
            ("session_id", "target_session_id"),
        ):
            target_value = row[column_name]
            scope_value = getattr(scope, field_name)
            if (
                target_value is not None
                and scope_value is not None
                and target_value != scope_value
            ):
                return False
        return row["target_agent_id"] == scope.agent_id

    def _assert_not_tombstoned(self, scope: MemoryScope, operation: str) -> None:
        column_name = _TOMBSTONE_FLAG_COLUMNS[operation]
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT target_project_id, target_user_id, target_session_id, target_agent_id
                FROM scope_tombstones
                WHERE target_tenant_id = ? AND {column_name} = 1
                """,
                (scope.tenant_id,),
            ).fetchall()
        if any(self._scope_matches_tombstone(scope, row) for row in rows):
            raise PermissionError(f"Memory scope is tombstoned for {operation}.")

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
            agent_id=request.scope.agent_id,
            forbidden_secret_values=self._redaction_values(request.redaction),
        )
        return AppendTranscriptResult(message_ids=message_ids)

    async def ingest_source_transcript(
        self,
        request: IngestSourceTranscriptRequest,
    ) -> IngestSourceTranscriptResult:
        self._authorize(request.scope, request.required_capability)
        results = ingest_source_messages(
            self.db_path,
            [
                SourceTranscriptInput(
                    source_host=item.source_host,
                    source_session_id=item.source_session_id,
                    source_message_id=item.source_message_id,
                    message_json=item.message_json,
                )
                for item in request.messages
            ],
            session_id=request.scope.session_id or "default",
            agent_id=request.scope.agent_id,
            forbidden_secret_values=self._redaction_values(request.redaction),
        )
        return IngestSourceTranscriptResult(
            items=[
                SourceTranscriptIngestItemResult(
                    source_host=item.source_host,
                    source_session_id=item.source_session_id,
                    source_message_id=item.source_message_id,
                    status=item.status,
                    message_id=item.message_id,
                    reason=item.reason,
                    warning=item.warning,
                )
                for item in results
            ]
        )

    async def search_transcript(
        self,
        request: SearchTranscriptRequest,
    ) -> SearchTranscriptResult:
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(request.scope, "retrieval")
        hits = search_messages(
            self.db_path,
            request.query,
            session_id=request.scope.session_id or "default",
            agent_id=request.scope.agent_id,
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
        *,
        max_rows: int | None = None,
    ) -> ExpandHistoryResult:
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(request.scope, "replay")
        row_cap = max_rows if max_rows is not None else EXPAND_HISTORY_MAX_ROWS
        if row_cap < 1:
            raise ValueError("max_rows must be at least 1.")
        try:
            rows = load_messages_in_id_range(
                self.db_path,
                request.first_message_id,
                request.last_message_id,
                session_id=request.scope.session_id or "default",
                agent_id=request.scope.agent_id,
                max_rows=row_cap,
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
        self._assert_not_tombstoned(
            self._with_default_session(request.scope),
            "retrieval",
        )
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
        raise NotImplementedError

    async def retire_fact(
        self,
        request: RetireFactRequest,
    ) -> RetireFactResult:
        raise NotImplementedError

    async def run_dream_phase(
        self,
        request: RunDreamPhaseRequest,
    ) -> RunDreamPhaseResult:
        raise NotImplementedError

    async def export_scope(
        self,
        request: ExportScopeRequest,
    ) -> ExportScopeResult:
        raise NotImplementedError

    async def replay_scope(
        self,
        request: ReplayScopeRequest,
    ) -> ReplayScopeResult:
        raise NotImplementedError

    async def rebuild(
        self,
        request: RebuildRequest,
    ) -> RebuildResult:
        raise NotImplementedError

    async def delete_scope(
        self,
        request: DeleteScopeRequest,
    ) -> DeleteScopeResult:
        raise NotImplementedError
