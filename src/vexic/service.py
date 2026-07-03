from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from pathlib import Path

from vexic.fs_permissions import ensure_owner_only

from vexic.contract import (
    AppendTranscriptRequest,
    AppendTranscriptResult,
    CandidateNote as ContractCandidateNote,
    DeleteScopeRequest,
    DeleteScopeResult,
    DreamPhase,
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
    TombstoneRecord,
    require_capability,
)
from vexic.ports import DreamPhasePorts, EmbedTexts, missing_host_port
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
from vexic.storage.longterm import record_fact_use_verdict, record_long_term_retrieval
from vexic.storage.operators import repair_memory_projections
from vexic.subagents.retrieval import retrieve_candidate_fallback, retrieve_long_term_facts
from vexic.usage import UsageSummary
from vexic.storage.connection import StorageTarget, connect, rows_as_dicts

def _iter_payload_strings(value: object) -> Iterator[str]:
    """Yield every raw string (mapping keys and values) in a JSON-able payload.

    The redaction guard is a plain substring check, so it must run against the
    unescaped values. Serialized JSON can escape forbidden secrets (newlines to
    `\\n`, non-ASCII to `\\uXXXX`) and hide them from the substring match.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_payload_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _iter_payload_strings(item)


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
        db_path: str | StorageTarget,
        tenant_id: str,
        forbidden_secret_values: tuple[str, ...] = (),
        embed: EmbedTexts | None = None,
        dream_phase_ports: DreamPhasePorts | None = None,
        artifact_dir: str | Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.tenant_id = tenant_id
        self.forbidden_secret_values = forbidden_secret_values
        self.embed = embed
        self.dream_phase_ports = dream_phase_ports
        # Export/replay/rebuild artifacts hold full memory content. The
        # default stays the OS temp dir for compatibility; hosts should point
        # this at a managed, owner-only location and schedule prune_artifacts.
        self.artifact_dir = None if artifact_dir is None else Path(artifact_dir)
        self._artifact_dir_prepared = False

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

    def _scope_matches_tombstone(self, scope: MemoryScope, row: Mapping[str, object]) -> bool:
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
        with closing(connect(self.db_path)) as conn:
            rows = rows_as_dicts(conn.execute(
                f"""
                SELECT target_project_id, target_user_id, target_session_id, target_agent_id
                FROM scope_tombstones
                WHERE target_tenant_id = ? AND {column_name} = 1
                """,
                (scope.tenant_id,),
            ))
        if any(self._scope_matches_tombstone(scope, row) for row in rows):
            raise PermissionError(f"Memory scope is tombstoned for {operation}.")

    def _artifact_root(self) -> Path:
        if self.artifact_dir is None:
            return Path(tempfile.gettempdir())
        if not self._artifact_dir_prepared:
            self.artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                # mkdir's mode only applies at creation; a pre-existing
                # directory keeps its old bits, so tighten before verifying.
                # (NT enforcement rewrites the DACL inside ensure_owner_only.)
                self.artifact_dir.chmod(0o700)
            ensure_owner_only(self.artifact_dir, directory=True)
            self._artifact_dir_prepared = True
        return self.artifact_dir

    def _artifact_path(self, kind: str) -> Path:
        return self._artifact_root() / f"vexic-{kind}-{uuid.uuid4().hex}.json"

    def prune_artifacts(self, *, older_than_seconds: float) -> int:
        """Delete aged ``vexic-*.json`` artifacts from the artifact root.

        Artifacts are plaintext full-content snapshots; they are meant to be
        consumed and discarded, not to accumulate. Returns the removed count.
        """
        if older_than_seconds < 0:
            raise ValueError(
                "older_than_seconds must be >= 0; a negative window would "
                "delete artifacts written moments ago."
            )
        root = self._artifact_root()
        if not root.exists():
            return 0
        cutoff = time.time() - older_than_seconds
        removed = 0
        for artifact in root.glob("vexic-*.json"):
            try:
                aged = artifact.stat().st_mtime < cutoff
            except FileNotFoundError:
                # Deleted between glob and stat by a concurrent consumer;
                # already gone is the outcome pruning wanted.
                continue
            if aged:
                artifact.unlink(missing_ok=True)
                removed += 1
        return removed

    def _write_json_artifact(
        self,
        kind: str,
        payload: dict[str, object],
        forbidden_secret_values: tuple[str, ...],
    ) -> str:
        text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
        # Check the structured (unescaped) strings first: the substring guard
        # cannot see secrets hidden behind JSON escaping. The serialized text is
        # checked too as a belt-and-suspenders pass.
        assert_no_forbidden_secret_values(
            forbidden_secret_values,
            *_iter_payload_strings(payload),
            text,
        )
        path = self._artifact_path(kind)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as artifact:
            artifact.write(text)
        return str(path)

    def _replay_hits(self, scope: MemoryScope) -> list[TranscriptHit]:
        scoped = self._with_default_session(scope)
        with closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, message_json
                FROM messages
                WHERE session_id = ?
                    AND agent_id IS ?
                ORDER BY id ASC
                """,
                (scoped.session_id, scoped.agent_id),
            ).fetchall()
        hits: list[TranscriptHit] = []
        for row in rows:
            message = single_message_adapter.validate_python(json.loads(row[2]))
            body = message_search_text(message)
            if body:
                hits.append(
                    TranscriptHit(
                        message_id=int(row[0]),
                        session_id=scoped.session_id or "default",
                        timestamp=row[1],
                        body=body,
                    )
                )
        return hits

    def _rows(
        self,
        conn: sqlite3.Connection,
        query: str,
        params: tuple[object, ...],
    ) -> list[dict[str, object]]:
        return rows_as_dicts(conn.execute(query, params))

    def _export_payload(self, scope: MemoryScope) -> dict[str, object]:
        scoped = self._with_default_session(scope)
        with closing(connect(self.db_path)) as conn:
            return {
                "scope": {
                    "tenant_id": scoped.tenant_id,
                    "project_id": scoped.project_id,
                    "user_id": scoped.user_id,
                    "session_id": scoped.session_id,
                    "agent_id": scoped.agent_id,
                },
                "messages": [
                    hit.model_dump(mode="json") for hit in self._replay_hits(scoped)
                ],
                "memory_candidates": self._rows(
                    conn,
                    """
                    SELECT id, fact_text, subject, category, importance, confidence,
                           source_message_ids, hit_count, retrieved_count, used_count,
                           promoted, promoted_fact_id, retired, stale, needs_review
                    FROM memory_candidates
                    WHERE agent_id IS ?
                    ORDER BY id ASC
                    """,
                    (scoped.agent_id,),
                ),
                "long_term_memory": self._rows(
                    conn,
                    """
                    SELECT id, fact_text, subject, category, importance, confidence,
                           source_message_ids, promoted_from_candidate_id,
                           retrieved_count, used_count, retired, retired_by_fact_id
                    FROM long_term_memory
                    WHERE agent_id IS ?
                    ORDER BY id ASC
                    """,
                    (scoped.agent_id,),
                ),
                "retrieval_events": self._rows(
                    conn,
                    """
                    SELECT id, fact_id, session_id, agent_id, query, retrieved_at,
                           used, judged_at
                    FROM retrieval_events
                    WHERE session_id = ?
                        AND agent_id IS ?
                    ORDER BY id ASC
                    """,
                    (scoped.session_id, scoped.agent_id),
                ),
                "candidate_retrieval_events": self._rows(
                    conn,
                    """
                    SELECT id, candidate_id, session_id, agent_id, query,
                           retrieved_at, used, judged_at
                    FROM candidate_retrieval_events
                    WHERE session_id = ?
                        AND agent_id IS ?
                    ORDER BY id ASC
                    """,
                    (scoped.session_id, scoped.agent_id),
                ),
                "session_summaries": self._rows(
                    conn,
                    """
                    SELECT id, session_id, agent_id, kind, first_message_id,
                           last_message_id, summary_text, token_estimate,
                           replaces_summary_ids
                    FROM session_summaries
                    WHERE session_id = ?
                        AND agent_id IS ?
                    ORDER BY id ASC
                    """,
                    (scoped.session_id, scoped.agent_id),
                ),
            }

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
            agent_id=request.scope.agent_id,
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
            agent_id=request.scope.agent_id,
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
        init_db(self.db_path)
        self._authorize(request.scope, request.required_capability)
        if (
            request.scope.session_id is not None
            and request.scope.session_id != request.event.session_id
        ):
            raise PermissionError("Retrieval event session_id is outside memory scope.")
        # The event is persisted under request.event.session_id, so the tombstone
        # check must run against that exact session. Using the defaulted scope
        # session would let a session-specific tombstone be bypassed whenever the
        # caller omits scope.session_id.
        self._assert_not_tombstoned(
            request.scope.model_copy(update={"session_id": request.event.session_id}),
            "retrieval",
        )
        event_ids = record_long_term_retrieval(
            self.db_path,
            [request.event.referent_id],
            session_id=request.event.session_id,
            agent_id=request.scope.agent_id,
            query=request.event.query,
            forbidden_secret_values=self._redaction_values(request.redaction),
        )
        if not event_ids:
            raise PermissionError("Retrieval referent is outside memory scope.")
        if request.event.used is not None:
            record_fact_use_verdict(
                self.db_path,
                used_event_ids=event_ids if request.event.used else [],
                unused_event_ids=[] if request.event.used else event_ids,
            )
        return RecordRetrievalEventResult(event_id=event_ids[0])

    async def retire_fact(
        self,
        request: RetireFactRequest,
    ) -> RetireFactResult:
        init_db(self.db_path)
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(
            self._with_default_session(request.scope),
            "rebuild",
        )
        with closing(connect(self.db_path)) as conn:
            with conn:
                if request.superseded_by_fact_id is not None:
                    row = conn.execute(
                        """
                        SELECT 1
                        FROM long_term_memory
                        WHERE id = ?
                            AND agent_id IS ?
                        """,
                        (request.superseded_by_fact_id, request.scope.agent_id),
                    ).fetchone()
                    if row is None:
                        raise PermissionError("Superseding fact is outside memory scope.")
                retired = conn.execute(
                    """
                    UPDATE long_term_memory
                    SET retired = 1,
                        retired_at = CURRENT_TIMESTAMP,
                        retired_by_fact_id = ?
                    WHERE id = ?
                        AND agent_id IS ?
                        AND retired = 0
                    """,
                    (
                        request.superseded_by_fact_id,
                        request.fact_id,
                        request.scope.agent_id,
                    ),
                ).rowcount
        return RetireFactResult(retired=retired > 0)

    async def run_dream_phase(
        self,
        request: RunDreamPhaseRequest,
    ) -> RunDreamPhaseResult:
        result, _usage = await _run_dream_phase_with_usage(self, request)
        return result

    async def export_scope(
        self,
        request: ExportScopeRequest,
    ) -> ExportScopeResult:
        init_db(self.db_path)
        self._authorize(request.scope, request.required_capability)
        scoped = self._with_default_session(request.scope)
        self._assert_not_tombstoned(scoped, "export")
        artifact_ref = self._write_json_artifact(
            "export",
            self._export_payload(scoped),
            self._redaction_values(request.redaction),
        )
        return ExportScopeResult(artifact_ref=artifact_ref)

    async def replay_scope(
        self,
        request: ReplayScopeRequest,
    ) -> ReplayScopeResult:
        init_db(self.db_path)
        self._authorize(request.scope, request.required_capability)
        scoped = self._with_default_session(request.scope)
        self._assert_not_tombstoned(scoped, "replay")
        messages = self._replay_hits(scoped)
        assert_no_forbidden_secret_values(
            self._redaction_values(request.redaction),
            *(hit.body for hit in messages),
        )
        return ReplayScopeResult(messages=messages)

    async def rebuild(
        self,
        request: RebuildRequest,
    ) -> RebuildResult:
        init_db(self.db_path)
        self._authorize(request.scope, request.required_capability)
        scoped = self._with_default_session(request.scope)
        self._assert_not_tombstoned(scoped, "rebuild")
        report = repair_memory_projections(
            self.db_path,
            forbidden_secret_values=self._redaction_values(request.redaction),
        )
        if not request.return_artifacts:
            return RebuildResult()
        artifact_ref = self._write_json_artifact(
            "rebuild",
            {
                "scope": {
                    "tenant_id": scoped.tenant_id,
                    "session_id": scoped.session_id,
                    "agent_id": scoped.agent_id,
                },
                "repair_report_scope": "database",
                "repair_report": {
                    "messages_fts_rows": report.messages_fts_rows,
                    "candidate_fts_rows": report.candidate_fts_rows,
                    "long_term_fts_rows": report.long_term_fts_rows,
                    "candidate_counters_recomputed": report.candidate_counters_recomputed,
                    "long_term_counters_recomputed": report.long_term_counters_recomputed,
                    "candidate_embeddings_repaired": report.candidate_embeddings_repaired,
                    "long_term_embeddings_repaired": report.long_term_embeddings_repaired,
                },
            },
            self._redaction_values(request.redaction),
        )
        return RebuildResult(artifact_ref=artifact_ref)

    async def delete_scope(
        self,
        request: DeleteScopeRequest,
    ) -> DeleteScopeResult:
        init_db(self.db_path)
        self._authorize(request.scope, request.required_capability)
        if request.target_scope.tenant_id != self.tenant_id:
            raise PermissionError("target_scope tenant_id does not match opened database.")
        assert_no_forbidden_secret_values(
            self._redaction_values(request.redaction),
            request.reason,
        )
        with closing(connect(self.db_path)) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO scope_tombstones
                        (target_tenant_id, target_project_id, target_user_id,
                         target_session_id, target_agent_id,
                         created_by_principal_id, created_by_principal_type, reason,
                         retrieval_blocked, export_blocked, replay_blocked,
                         rebuild_blocked, physical_purge_deferred)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 1, 1)
                    """,
                    (
                        request.target_scope.tenant_id,
                        request.target_scope.project_id,
                        request.target_scope.user_id,
                        request.target_scope.session_id,
                        request.target_scope.agent_id,
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


async def _run_dream_phase_with_usage(
    service: LocalMemoryService,
    request: RunDreamPhaseRequest,
) -> tuple[RunDreamPhaseResult, UsageSummary]:
    init_db(service.db_path)
    service._authorize(request.scope, request.required_capability)
    service._assert_not_tombstoned(
        service._with_default_session(request.scope),
        "rebuild",
    )
    ports = service.dream_phase_ports
    if ports is None:
        raise missing_host_port("Dream phase")

    if request.phase is DreamPhase.LIGHT:
        from vexic.pipeline import run_light_phase

        usage = await run_light_phase(
            service.db_path,
            ports.model_group,
            agent_id=request.scope.agent_id,
            secrets=ports.secrets,
            extraction_agent_factory=ports.extraction_agent_factory,
            embed=ports.embed,
            forbidden_secret_values=service._redaction_values(request.redaction),
        )
    elif request.phase is DreamPhase.REM:
        from vexic.rem import run_rem_phase

        usage = await run_rem_phase(
            service.db_path,
            agent_id=request.scope.agent_id,
            forbidden_secret_values=service._redaction_values(request.redaction),
        )
    else:
        from vexic.deep import run_deep_phase

        usage = await run_deep_phase(
            service.db_path,
            ports.model_group,
            agent_id=request.scope.agent_id,
            secrets=ports.secrets,
            contradiction_agent_factory=ports.contradiction_agent_factory,
            defer_contradiction=ports.defer_contradiction,
            forbidden_secret_values=service._redaction_values(request.redaction),
        )

    return RunDreamPhaseResult(phase=request.phase, status="ok"), usage
