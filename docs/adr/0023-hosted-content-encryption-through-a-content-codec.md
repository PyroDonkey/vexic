# Hosted content encryption flows through a core ContentCodec port

Status: accepted

## Context

ADR 0008 chose provider-managed encryption at rest for hosted v1 and
deliberately deferred app-level encryption of searchable content. The 2026-07
privacy audit re-opened that call against the product requirement that user
chat logs must not be readable by anyone, including operators: provider disk
encryption does not stop a holder of a database token or the platform API
token from reading plaintext memory. The owner approved app-level envelope
encryption for hosted canonical content -- per-tenant data keys wrapped by a
KMS key, unwrapped at runtime in the hosted adapter, with production KMS
decrypt denied to developers and break-glass access audited.

A multi-model design review (Fuse deep pass) shaped the seam below; the
panel's disagreement over per-call AAD arguments was resolved in favor of the
repo's no-speculative-parameters rule: AAD binding is fixed at codec
construction, and adding an optional argument later is a non-breaking change.

## Decision

- `vexic.ports.ContentCodec` is the seam: `encode(plaintext) -> str` before
  storage, `decode(stored) -> str` after reads. No codec configured means
  plaintext passthrough -- the local default, where the database already sits
  in the user's own custody. Hosted adapters supply an encrypting codec;
  key material and KMS SDKs never enter `src/vexic` (policy-tested).
- Codecs own their envelope: encoded values carry a codec-specific version
  prefix (for example `vx1:`), and `decode` passes through values without the
  prefix, so plaintext legacy rows keep reading correctly during migration.
  The identity default writes no prefix at all.
- This ADR covers Tier 1 transcript content end-to-end:
  `messages.message_json` is encoded by `save_messages` and
  `ingest_source_messages` (after the plaintext forbidden-value guard and
  FTS body extraction) and decoded by every reader -- replay, export, history
  expansion, the token-budget and batch loaders, the FTS rebuild, the ingest
  duplicate-content comparison, and the Light-phase transcript loader.
- Search projections are a documented plaintext residue: `messages_fts.body`
  (and later the candidate/fact FTS and vector projections) hold searchable
  plaintext derived before encoding. They are rebuildable, live inside the
  same provider-encrypted database, and are the accepted trade for keeping
  server-side search. Do not claim full at-rest content encryption until a
  projection design addresses them.
- Privileged egress decodes: export, replay, and rebuild artifacts contain
  readable JSON, never codec envelopes, and the existing forbidden-value
  redaction runs on decoded plaintext.
- Idempotency and dedup comparisons operate on plaintext or source keys,
  never on encoded bytes (encrypting codecs are non-deterministic). The
  source-transcript ledger already keys on source identifiers; its
  duplicate-content warning decodes the stored row before comparing.

## Rollout

Phase 12a (this change) ships the seam, the identity default, and the
transcript column. Phase 12b supplies the KMS-backed codec in `adapters/`
(per-tenant DEK wrapped by a KMS CMK, wrapped-DEK storage in the control
plane, AAD bound at construction per column), extends encoding to the
remaining content columns (`memory_candidates.fact_text/subject`,
`long_term_memory.fact_text/subject`, `session_summaries.summary_text`,
`memory_dedup_events.incoming_fact_text`, `promotion_labels.fact_text`,
`retrieval_events.query/rewritten_query`), and runs the operator-run
re-encryption migration for existing hosted data (ADR 0011 pattern,
verify-gated per ADR 0019). Purge (ADR 0022) already deletes rows outright
and never calls `decode`; 12b adds wrapped-DEK deletion so a whole-tenant
purge is also a crypto-shred.

## Consequences

- Amends ADR 0008: "Vexic does not build app-level searchable-memory
  encryption" no longer holds; the provider-encryption baseline, TLS
  requirements, and backup posture stand.
- Local mode stays plaintext by default; an encrypted local mode (SQLCipher
  or a local codec) remains a deliberate deferral.
- KMS latency lands on the hosted read path in 12b; key loss makes tenant
  data unrecoverable -- both are hosted-adapter concerns to size there.
- Confidentiality claims must stay honest: with 12a, transcript canonical
  rows are codec-encoded on hosted deployments that configure a codec, while
  FTS projections and the not-yet-covered content columns remain plaintext
  inside the provider-encrypted database.
