"""SQLite reference implementation of the Vexic memory service contract.

``LocalMemoryService`` implements the full :class:`vexic.contract.MemoryService`
protocol against a local SQLite database (or a libSQL ``StorageTarget``). It
is the conformance baseline: hosted adapters wrap or mirror this behavior.
"""

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
    FreshContextRequest,
    FreshContextResult,
    LoadActiveContextRequest,
    LoadActiveContextResult,
    IngestSourceTranscriptRequest,
    IngestSourceTranscriptResult,
    MemoryCapability,
    MemoryCategory,
    MemoryScope,
    MemoryService,
    PurgeScopeRequest,
    PurgeScopeResult,
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
    SummaryNode,
    TranscriptHit,
    LongTermFact as ContractLongTermFact,
    TombstoneRecord,
    require_capability,
)
from vexic.ports import ContentCodec, DreamPhasePorts, EmbedTexts, missing_host_port
from vexic.redaction import (
    assert_no_forbidden_secret_values,
    assert_no_forbidden_secret_values_in_payload,
)
from vexic.storage import (
    TranscriptRangeTooLarge,
    SourceTranscriptInput,
    ingest_source_messages,
    count_session_messages,
    init_db,
    load_active_context_messages,
    load_fresh_context_rows,
    load_messages_in_id_range,
    render_session_recap,
    message_search_text,
    render_recap_blocks,
    save_messages,
    search_messages,
    single_message_adapter,
)
from vexic.storage.longterm import record_fact_use_verdict, record_long_term_retrieval
from vexic.storage.operators import repair_memory_projections
from vexic.storage.purge import purge_scope_rows
from vexic.timeutil import utc_now_iso
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
    """Local, single-tenant implementation of ``MemoryService`` over SQLite.

    Constructor arguments (keyword-only):

    - ``db_path``: SQLite file path or a ``StorageTarget`` (libSQL DSN).
    - ``tenant_id``: the tenant this database serves; requests whose scope
      names another tenant are rejected.
    - ``forbidden_secret_values``: service-level secrets merged into every
      request's ``RedactionContext`` for the egress/storage guards.
    - ``embed``: optional embedding adapter; defaults to the local embedder.
    - ``dream_phase_ports``: host-supplied agents for the dream phases;
      ``run_dream_phase`` fails without them.
    - ``content_codec``: optional encrypt/decrypt codec for stored
      transcript content (ADR 0023); ``None`` stores plaintext.
    - ``artifact_dir``: directory for export/replay/rebuild artifacts;
      defaults to the OS temp dir.

    Call :meth:`init_schema` once after construction (and after any
    upgrade) before invoking operations; it creates or migrates the schema.
    """

    def __init__(
        self,
        *,
        db_path: str | StorageTarget,
        tenant_id: str,
        forbidden_secret_values: tuple[str, ...] = (),
        embed: EmbedTexts | None = None,
        dream_phase_ports: DreamPhasePorts | None = None,
        content_codec: ContentCodec | None = None,
        artifact_dir: str | Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.tenant_id = tenant_id
        self.forbidden_secret_values = forbidden_secret_values
        self.embed = embed
        self.dream_phase_ports = dream_phase_ports
        # ADR 0023: canonical transcript content is encoded through this
        # codec before storage and decoded after reads. None = plaintext
        # (the local default); hosted adapters supply an encrypting codec.
        self.content_codec = content_codec
        # Export/replay/rebuild artifacts hold full memory content. The
        # default stays the OS temp dir for compatibility; hosts should point
        # this at a managed, owner-only location and schedule prune_artifacts.
        self.artifact_dir = None if artifact_dir is None else Path(artifact_dir)
        self._artifact_dir_prepared = False

    def _decode_content(self, stored: str) -> str:
        if self.content_codec is None:
            return stored
        return self.content_codec.decode(stored)

    def init_schema(self) -> None:
        """Create or migrate the database schema; required before first use."""
        # Thread the codec so a first-init FTS rebuild decodes encoded rows;
        # every service entrypoint routes through here (ADR 0023).
        init_db(self.db_path, content_codec=self.content_codec)

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

    def _scope_matches_tombstone_erase_key(
        self, scope: MemoryScope, row: Mapping[str, object]
    ) -> bool:
        """Match the (session, agent) erase key the physical purge uses.

        ``purge_scope_rows`` deletes by ``(? IS NULL OR session_id = ?) AND
        agent_id IS ?`` -- project/user-blind, because the physical tables
        carry no project/user columns (ADR 0007). The write gate must mirror
        that exactly: a tombstone whose project or user differs from the
        writer's scope still erases the writer's rows, so those fields must
        not exempt a write.
        """
        target_session = row["target_session_id"]
        if target_session is not None and target_session != scope.session_id:
            return False
        return row["target_agent_id"] == scope.agent_id

    def _tombstone_blocks_write(self, scope: MemoryScope) -> bool:
        """Whether any tombstone blocks writes into this scope.

        Writes are blocked by ANY matching tombstone, regardless of which
        lifecycle flags it carries: every tombstone marks the scope for
        erasure (pending or completed purge, ADR 0022), so new content
        written into it would be either erased by the purge or orphaned
        behind the audit record. Matching uses the physical erase key; reads
        gate logical access and honor the tombstone's full scope pattern
        instead.
        """
        with closing(connect(self.db_path)) as conn:
            rows = rows_as_dicts(conn.execute(
                """
                SELECT target_session_id, target_agent_id
                FROM scope_tombstones
                WHERE target_tenant_id = ?
                """,
                (scope.tenant_id,),
            ))
        return any(
            self._scope_matches_tombstone_erase_key(scope, row) for row in rows
        )

    def _assert_not_tombstoned(self, scope: MemoryScope, operation: str) -> None:
        if operation == "write":
            if self._tombstone_blocks_write(scope):
                raise PermissionError("Memory scope is tombstoned for write.")
            return
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

    def _assert_no_pending_purge_for_agent(self, scope: MemoryScope) -> None:
        """Block agent-wide sweeps while a physical purge is pending.

        Dream phases read source rows by agent across ALL sessions (Light
        reads messages agent-wide, REM/Deep operate on candidates agent-wide,
        Summarize keys its rows to the source sessions), so a per-session
        gate cannot protect them: a run under any session scope could
        consolidate a doomed session's still-present rows into outputs the
        purge then erases by source intersection. While any tombstone for
        this exact agent has its physical purge pending
        (``physical_purge_deferred = 1``; the purge transaction flips it to 0
        on completion, and a dry run rolls the flip back), the whole agent is
        blocked. After the purge completes the doomed sources are gone, so
        only the per-session erase-key write gate remains -- an agent-wide
        block would otherwise outlive the purge forever, because tombstones
        survive as audit records.
        """
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM scope_tombstones
                WHERE target_tenant_id = ?
                    AND target_agent_id IS ?
                    AND physical_purge_deferred = 1
                LIMIT 1
                """,
                (scope.tenant_id, scope.agent_id),
            ).fetchone()
        if row is not None:
            raise PermissionError(
                "Agent scope has a physical purge pending; dream phases are "
                "blocked until purge_scope completes."
            )

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
            message = single_message_adapter.validate_python(
                json.loads(self._decode_content(row[2]))
            )
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
        # Messages persist under scope.session_id or "default", so the
        # tombstone check runs against that exact session (ADR 0022 follow-up:
        # writes into a tombstoned scope fail closed instead of being erased
        # or orphaned by the deferred purge).
        self._assert_not_tombstoned(self._with_default_session(request.scope), "write")
        messages = [single_message_adapter.validate_json(raw) for raw in request.messages_json]
        message_ids = save_messages(
            self.db_path,
            messages,
            session_id=request.scope.session_id or "default",
            agent_id=request.scope.agent_id,
            forbidden_secret_values=self._redaction_values(request.redaction),
            content_codec=self.content_codec,
        )
        return AppendTranscriptResult(message_ids=message_ids)

    async def ingest_source_transcript(
        self,
        request: IngestSourceTranscriptRequest,
    ) -> IngestSourceTranscriptResult:
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(self._with_default_session(request.scope), "write")
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
            content_codec=self.content_codec,
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
                content_codec=self.content_codec,
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

    async def fresh_context(
        self,
        request: FreshContextRequest,
    ) -> FreshContextResult:
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(request.scope, "retrieval")
        session_id = request.scope.session_id or "default"
        # After trimming, the surviving frontier may start mid-session; the
        # covered-prefix then collapses to zero and the tail re-walks from the
        # session start, which can duplicate trimmed ranges as raw text --
        # acceptable because the token ceiling still bounds the total.
        frontier, tail_hits = load_fresh_context_rows(
            self.db_path,
            token_budget=request.token_budget,
            session_id=session_id,
            agent_id=request.scope.agent_id,
            content_codec=self.content_codec,
        )

        summaries = [
            SummaryNode(
                summary_id=summary.id,
                session_id=summary.session_id,
                first_message_id=summary.first_message_id,
                last_message_id=summary.last_message_id,
                summary_text=summary.summary_text,
                token_estimate=summary.token_estimate,
                created_at=summary.created_at,
            )
            for summary in frontier
        ]
        recent = [
            TranscriptHit(
                message_id=hit.message_id,
                session_id=session_id,
                timestamp=hit.timestamp,
                body=hit.body,
            )
            for hit in tail_hits
        ]

        redaction_values = self._redaction_values(request.redaction)
        recap_blocks = render_recap_blocks(
            frontier,
            forbidden_secret_values=redaction_values,
        )
        recent_block = "\n\n".join(
            f"[message {hit.message_id} @ {hit.timestamp}]\n{hit.body}"
            if hit.timestamp
            else f"[message {hit.message_id}]\n{hit.body}"
            for hit in recent
        )
        sections = [block for block in (*recap_blocks, recent_block) if block]
        text = "\n\n".join(sections)
        assert_no_forbidden_secret_values(redaction_values, text)
        return FreshContextResult(summaries=summaries, recent=recent, text=text)

    async def load_active_context(
        self,
        request: LoadActiveContextRequest,
    ) -> LoadActiveContextResult:
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(request.scope, "retrieval")
        session_id = request.scope.session_id or "default"
        messages = load_active_context_messages(
            self.db_path,
            token_budget=request.token_budget,
            session_id=session_id,
            agent_id=request.scope.agent_id,
            timezone_name=request.timezone_name,
            content_codec=self.content_codec,
        )
        redaction_values = self._redaction_values(request.redaction)
        messages_json: list[str] = []
        for message in messages:
            # Guard the structured form, not the serialized string: JSON
            # escaping (newline -> \n, non-ASCII -> \uXXXX) can hide a
            # forbidden value from a substring check that the client would
            # reconstruct on parse.
            payload = single_message_adapter.dump_python(message, mode="json")
            assert_no_forbidden_secret_values_in_payload(redaction_values, payload)
            messages_json.append(json.dumps(payload, ensure_ascii=False))
        recap = render_session_recap(
            self.db_path,
            session_id=session_id,
            agent_id=request.scope.agent_id,
            forbidden_secret_values=redaction_values,
            content_codec=self.content_codec,
        )
        total = count_session_messages(
            self.db_path,
            session_id=session_id,
            agent_id=request.scope.agent_id,
        )
        return LoadActiveContextResult(
            messages_json=messages_json,
            recap_text=recap or None,
            truncated=len(messages) < total,
        )

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
            as_of=request.as_of,
            event_after=request.event_after,
            event_before=request.event_before,
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
                        occurred_at=fact.occurred_at,
                        mentioned_at=fact.mentioned_at,
                    )
                    for fact in facts
                ]
            )

        if self._tombstone_blocks_write(self._with_default_session(request.scope)):
            # The candidate fallback records retrieval telemetry (an INSERT
            # into candidate_retrieval_events plus counter updates) and serves
            # tentative notes; both are wrong for a scope marked for erasure.
            # The search itself is a read governed by the retrieval flag
            # above, so skip the fallback rather than failing the read.
            return SearchLongTermResult(candidate_notes=[])

        notes = await retrieve_candidate_fallback(
            self.db_path,
            request.query,
            session_id=request.scope.session_id or "default",
            agent_id=request.scope.agent_id,
            return_k=request.limit,
            embed=self.embed,
            as_of=request.as_of,
            event_after=request.event_after,
            event_before=request.event_before,
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
        self.init_schema()
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
        # Recording the event also writes a row, so the flag-independent write
        # gate applies on top of the retrieval flag gate.
        self._assert_not_tombstoned(
            request.scope.model_copy(update={"session_id": request.event.session_id}),
            "write",
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
        self.init_schema()
        self._authorize(request.scope, request.required_capability)
        self._assert_not_tombstoned(
            self._with_default_session(request.scope),
            "rebuild",
        )
        # Retiring a fact mutates a row, so the flag-independent write gate
        # applies on top of the rebuild flag gate.
        self._assert_not_tombstoned(
            self._with_default_session(request.scope),
            "write",
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
        self.init_schema()
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
        self.init_schema()
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
        self.init_schema()
        self._authorize(request.scope, request.required_capability)
        scoped = self._with_default_session(request.scope)
        self._assert_not_tombstoned(scoped, "rebuild")
        report = repair_memory_projections(
            self.db_path,
            forbidden_secret_values=self._redaction_values(request.redaction),
            content_codec=self.content_codec,
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
        self.init_schema()
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

    async def purge_scope(
        self,
        request: PurgeScopeRequest,
    ) -> PurgeScopeResult:
        init_db(self.db_path)
        self._authorize(request.scope, request.required_capability)
        if request.target_scope.tenant_id != self.tenant_id:
            raise PermissionError("target_scope tenant_id does not match opened database.")
        if request.target_scope.session_id is None and not request.confirm_whole_scope:
            # Guard the mass-delete surface: a null session_id erases every
            # session for the target agent scope. Fail before any deletion and
            # regardless of dry_run so even a preview requires opting in.
            raise ValueError(
                "Whole-scope purge (null target_scope.session_id) erases every "
                "session for the target agent scope; set confirm_whole_scope=True "
                "to proceed."
            )
        assert_no_forbidden_secret_values(
            self._redaction_values(request.redaction),
            request.reason,
        )
        with closing(connect(self.db_path)) as conn:
            tombstone_rows = conn.execute(
                """
                SELECT id FROM scope_tombstones
                WHERE target_tenant_id = ?
                    AND target_project_id IS ?
                    AND target_user_id IS ?
                    AND target_session_id IS ?
                    AND target_agent_id IS ?
                """,
                (
                    request.target_scope.tenant_id,
                    request.target_scope.project_id,
                    request.target_scope.user_id,
                    request.target_scope.session_id,
                    request.target_scope.agent_id,
                ),
            ).fetchall()
        tombstone_ids = [int(row[0]) for row in tombstone_rows]
        if not tombstone_ids:
            raise ValueError(
                "No tombstone matches the target scope; run delete_scope first. "
                "Purge is the second deliberate step of erasure."
            )
        purged_at = utc_now_iso()
        counts = purge_scope_rows(
            self.db_path,
            target_session_id=request.target_scope.session_id,
            target_agent_id=request.target_scope.agent_id,
            tombstone_ids=tombstone_ids,
            purged_at=purged_at,
            dry_run=request.dry_run,
        )
        return PurgeScopeResult(
            tombstone_id=str(max(tombstone_ids)),
            purged=counts,
            dry_run=request.dry_run,
            purged_at=None if request.dry_run else purged_at,
        )


async def _run_dream_phase_with_usage(
    service: LocalMemoryService,
    request: RunDreamPhaseRequest,
) -> tuple[RunDreamPhaseResult, UsageSummary]:
    service.init_schema()
    service._authorize(request.scope, request.required_capability)
    # Dream phases write candidate and fact rows, so they are gated as writes:
    # any tombstone matching the erase key blocks them even when its lifecycle
    # flags (including rebuild_blocked) are zero. The write gate subsumes the
    # rebuild flag gate here, since the normalized scope carries a concrete
    # session and the erase-key match is at least as broad.
    service._assert_not_tombstoned(
        service._with_default_session(request.scope),
        "write",
    )
    # And because the sweeps read sources agent-wide across sessions, the
    # whole agent is additionally blocked while any purge is pending for it
    # (see _assert_no_pending_purge_for_agent).
    service._assert_no_pending_purge_for_agent(request.scope)
    ports = service.dream_phase_ports
    if ports is None:
        raise missing_host_port("Dream phase")

    if request.phase is DreamPhase.LIGHT:
        from vexic.pipeline import run_light_phase

        outcome = await run_light_phase(
            service.db_path,
            ports.model_group,
            agent_id=request.scope.agent_id,
            secrets=ports.secrets,
            extraction_agent_factory=ports.extraction_agent_factory,
            embed=ports.embed,
            forbidden_secret_values=service._redaction_values(request.redaction),
            content_codec=service.content_codec,
        )
        # ADR 0031 amendment: a run that extracted candidates and kept none
        # (all dropped for bad provenance) is partial, not a silent ok.
        status = (
            "partial"
            if outcome.candidates_dropped and not outcome.candidates_kept
            else "ok"
        )
        return RunDreamPhaseResult(phase=request.phase, status=status), outcome.usage
    elif request.phase is DreamPhase.REM:
        from vexic.rem import run_rem_phase

        usage = await run_rem_phase(
            service.db_path,
            agent_id=request.scope.agent_id,
            forbidden_secret_values=service._redaction_values(request.redaction),
        )
    elif request.phase is DreamPhase.DEEP:
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
    elif request.phase is DreamPhase.SUMMARIZE:
        from vexic.summarize import run_summarize_phase

        outcome = await run_summarize_phase(
            service.db_path,
            ports.model_group,
            agent_id=request.scope.agent_id,
            secrets=ports.secrets,
            summary_agent_factory=ports.summary_agent_factory,
            forbidden_secret_values=service._redaction_values(request.redaction),
            content_codec=service.content_codec,
            daily_span_budget=ports.daily_span_budget,
        )
        # Per-session isolation swallows individual session failures so the
        # rest of the sweep proceeds; the phase outcome must still say so.
        if outcome.sessions_failed == 0:
            status = "ok"
        elif outcome.sessions_failed < outcome.sessions_considered:
            status = "partial"
        else:
            status = "error"
        return RunDreamPhaseResult(phase=request.phase, status=status), outcome.usage
    else:
        raise ValueError(f"Unsupported dream phase: {request.phase!r}")

    return RunDreamPhaseResult(phase=request.phase, status="ok"), usage
