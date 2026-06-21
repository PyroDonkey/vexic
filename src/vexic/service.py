from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

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
    TombstoneRecord,
    TranscriptHit,
    LongTermFact as ContractLongTermFact,
    require_capability,
)
from vexic.ports import EmbedTexts, missing_host_port
from vexic.redaction import assert_no_forbidden_secret_values
from vexic.storage import (
    TranscriptRangeTooLarge,
    SourceTranscriptInput,
    ingest_source_messages,
    init_db,
    load_messages_in_id_range,
    message_search_text,
    record_fact_use_verdict,
    record_long_term_retrieval,
    create_memory_rebuild_copy,
    repair_memory_projections,
    save_messages,
    search_messages,
    single_message_adapter,
)
from vexic.storage.longterm import retire_long_term_fact
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
            if target_value is not None and target_value != getattr(scope, field_name):
                return False
        return True

    def _assert_not_tombstoned(self, scope: MemoryScope, operation: str) -> None:
        column_name = _TOMBSTONE_FLAG_COLUMNS[operation]
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT target_project_id, target_user_id, target_session_id
                FROM scope_tombstones
                WHERE target_tenant_id = ? AND {column_name} = 1
                """,
                (scope.tenant_id,),
            ).fetchall()
        if any(self._scope_matches_tombstone(scope, row) for row in rows):
            raise PermissionError(f"Memory scope is tombstoned for {operation}.")

    def _load_replay_hits(self, scope: MemoryScope) -> list[TranscriptHit]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, timestamp, message_json
                FROM messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (scope.session_id or "default",),
            ).fetchall()

        hits: list[TranscriptHit] = []
        for row in rows:
            body = message_search_text(single_message_adapter.validate_json(row[3]))
            if body:
                hits.append(
                    TranscriptHit(
                        message_id=int(row[0]),
                        session_id=str(row[1]),
                        timestamp=None if row[2] is None else str(row[2]),
                        body=body,
                    )
                )
        return hits

    def _export_payload(self, scope: MemoryScope) -> dict[str, object]:
        messages = self._load_replay_hits(scope)
        exported_message_ids = {hit.message_id for hit in messages}
        with closing(sqlite3.connect(self.db_path)) as conn:
            fact_rows = conn.execute(
                """
                SELECT id, fact_text, subject, category, importance, confidence,
                       source_message_ids, promoted_from_candidate_id,
                       retrieved_count, used_count, retired, retired_at,
                       retired_by_fact_id, editable, created_at
                FROM long_term_memory
                ORDER BY id ASC
                """
            ).fetchall()

        facts: list[dict[str, object]] = []
        for row in fact_rows:
            source_message_ids = [int(value) for value in json.loads(row[6])]
            if exported_message_ids and not exported_message_ids.intersection(source_message_ids):
                continue
            facts.append(
                {
                    "fact_id": int(row[0]),
                    "fact_text": str(row[1]),
                    "subject": str(row[2]),
                    "category": str(row[3]),
                    "importance": int(row[4]),
                    "confidence": float(row[5]),
                    "source_message_ids": source_message_ids,
                    "promoted_from_candidate_id": int(row[7]),
                    "retrieved_count": int(row[8]),
                    "used_count": int(row[9]),
                    "retired": bool(row[10]),
                    "retired_at": None if row[11] is None else str(row[11]),
                    "retired_by_fact_id": None if row[12] is None else int(row[12]),
                    "editable": bool(row[13]),
                    "created_at": str(row[14]),
                }
            )
        return {
            "scope": scope.model_dump(mode="json"),
            "messages": [hit.model_dump(mode="json") for hit in messages],
            "long_term_memory": facts,
        }

    def _write_export_artifact(
        self,
        payload: dict[str, object],
        redaction: RedactionContext,
    ) -> Path:
        target = Path(self.db_path).with_name(
            f"{Path(self.db_path).stem}-export-{uuid4().hex}.json"
        )
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        assert_no_forbidden_secret_values(
            self._redaction_values(redaction),
            rendered,
        )
        temp_path = target.with_name(f".{target.name}.tmp")
        try:
            temp_path.write_text(rendered, encoding="utf-8")
            temp_path.replace(target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return target

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
        self._authorize(request.scope, request.required_capability)
        event_scope = request.scope.model_copy(
            update={"session_id": request.event.session_id}
        )
        self._assert_not_tombstoned(event_scope, "retrieval")
        event_ids = record_long_term_retrieval(
            self.db_path,
            [request.event.referent_id],
            session_id=request.event.session_id,
            query=request.event.query,
            forbidden_secret_values=self._redaction_values(request.redaction),
        )
        event_id = event_ids[0]
        if request.event.used is True:
            record_fact_use_verdict(
                self.db_path,
                used_event_ids=[event_id],
                unused_event_ids=[],
            )
        elif request.event.used is False:
            record_fact_use_verdict(
                self.db_path,
                used_event_ids=[],
                unused_event_ids=[event_id],
            )
        return RecordRetrievalEventResult(event_id=event_id)

    async def retire_fact(
        self,
        request: RetireFactRequest,
    ) -> RetireFactResult:
        self._authorize(request.scope, request.required_capability)
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                retired = retire_long_term_fact(
                    conn,
                    fact_id=request.fact_id,
                    superseded_by_fact_id=request.superseded_by_fact_id,
                )
        return RetireFactResult(retired=retired)

    async def run_dream_phase(
        self,
        request: RunDreamPhaseRequest,
    ) -> RunDreamPhaseResult:
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(request.scope, "rebuild")
        raise missing_host_port("Dream phase execution")

    async def export_scope(
        self,
        request: ExportScopeRequest,
    ) -> ExportScopeResult:
        self._authorize(request.scope, request.required_capability)
        scope = self._with_default_session(request.scope)
        self._assert_not_tombstoned(scope, "export")
        artifact = self._write_export_artifact(
            self._export_payload(scope),
            request.redaction,
        )
        return ExportScopeResult(artifact_ref=str(artifact))

    async def replay_scope(
        self,
        request: ReplayScopeRequest,
    ) -> ReplayScopeResult:
        self._authorize(request.scope, request.required_capability)
        scope = self._with_default_session(request.scope)
        self._assert_not_tombstoned(scope, "replay")
        messages = self._load_replay_hits(scope)
        assert_no_forbidden_secret_values(
            self._redaction_values(request.redaction),
            *(hit.body for hit in messages),
        )
        return ReplayScopeResult(messages=messages)

    async def rebuild(
        self,
        request: RebuildRequest,
    ) -> RebuildResult:
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(request.scope, "rebuild")
        forbidden_values = self._redaction_values(request.redaction)
        if request.return_artifacts:
            target = Path(self.db_path).with_name(
                f"{Path(self.db_path).stem}-rebuild-{uuid4().hex}.db"
            )
            report = create_memory_rebuild_copy(
                self.db_path,
                target,
                forbidden_secret_values=forbidden_values,
            )
            return RebuildResult(artifact_ref=str(report.output_path))

        repair_memory_projections(
            self.db_path,
            forbidden_secret_values=forbidden_values,
        )
        return RebuildResult()

    async def delete_scope(
        self,
        request: DeleteScopeRequest,
    ) -> DeleteScopeResult:
        self._authorize(request.scope, request.required_capability)
        assert_no_forbidden_secret_values(
            self._redaction_values(request.redaction),
            request.reason,
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO scope_tombstones
                        (target_tenant_id, target_project_id, target_user_id,
                         target_session_id, created_by_principal_id,
                         created_by_principal_type, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.target_scope.tenant_id,
                        request.target_scope.project_id,
                        request.target_scope.user_id,
                        request.target_scope.session_id,
                        request.scope.principal.principal_id,
                        request.scope.principal.principal_type.value,
                        request.reason,
                    ),
                )
                tombstone_id = str(cursor.lastrowid)

        return DeleteScopeResult(
            tombstone=TombstoneRecord(
                tombstone_id=tombstone_id,
                target_scope=request.target_scope,
                created_by=request.scope.principal,
                reason=request.reason,
                retrieval_blocked=True,
                export_blocked=True,
                replay_blocked=True,
                rebuild_blocked=True,
                physical_purge_deferred=True,
            )
        )
