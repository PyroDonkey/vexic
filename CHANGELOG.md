# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.7] - 2026-07-18

Recorder deadline, retry, and error-reporting reliability for the async Stop
and SessionStart hooks. No public memory contract change; `CONTRACT_VERSION`
stays `0.1.0`.

### Fixed

- Session-start prime reads fan out across parallel daemon workers under a
  single end-to-end deadline (default 20s), so a stalled hosted read is
  abandoned rather than waited into the SessionStart hook kill window and
  can no longer eat the whole injected block (#250).
- Recorder ingest bounds retries end to end: transport failures, 408, and
  429 responses retry with a capped, jittered backoff and honor a bounded
  `Retry-After`, and the per-attempt socket timeout is capped to the
  remaining budget so a dripping response cannot stretch a body read far
  past the deadline. The default socket timeout composes with the ingest
  deadline to stay inside the Stop hook kill even when an un-preempted
  in-flight read overshoots the deadline (#252, #253).
- MCP `tools/call` error path sanitizes pydantic `ValidationError` messages
  through the client-safe builder instead of echoing raw input values
  (#251).

### Changed

- `fetch_prime_context` returns a `PrimeFetchResult` (context plus per-leg
  timing/outcome) instead of a bare `str`; `recorder prime` writes a
  `phase: "started"` attempt marker and a `phase: "finished"` record with
  per-leg durations into a sibling `-prime.json` status file so overlapping
  Stop-hook ingests and prime cannot erase each other's records (#250).
- Product glossary adds the dreaming and recorder terms; the hosted-mvp
  sweeper section notes the bounded prelude retry (ADR 0030 amendment).

[0.1.7]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.7

## [0.1.6] - 2026-07-17

Session-start priming robustness plus recorder and hosted-layer reliability
fixes. No public memory contract change; `CONTRACT_VERSION` stays `0.1.0`.

### Fixed

- Session-start prime usage guidance survives truncation: a fixed framing
  block leads the injected snapshot and footer space is reserved before
  capping, so the instructions can no longer be the first thing truncated
  away (#246).
- Recorder transcript-cursor writes are monotonic, so overlapping async Stop
  ingests can no longer regress the cursor and re-post already-ledgered
  rows; equal-offset content corrections still write through so same-length
  transcript rewrites self-heal (#248).
- Hosted write-route preflight 400 responses sanitize pydantic
  `ValidationError` messages through the same client-safe builder as the
  HTTP adapter, instead of echoing raw input values (#249).
- Dream-phase prelude retries retryable operational faults with a bounded
  backoff (#243).
- Recorder Stop hook fails open on transient hosted errors instead of
  derailing the conversation with a blocking exit (#244).
- Hosted 400 responses in the HTTP adapter sanitize pydantic
  `ValidationError` messages (#245).

### Changed

- Prime snapshot sections carry per-item caps (transcript hits and the
  recap body) with an explicit truncation marker at both fetch and render
  time, so one long item cannot starve the other sections; the full text
  stays reachable through the recall tools (#246).
- MCP `server_instructions` now describe session-start priming: visible
  snapshot facts need no re-search (explicitly subordinate to the
  search-before-denying rule) and truncated snapshot items are flagged as
  incomplete (#246).

[0.1.6]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.6

## [0.1.5] - 2026-07-16

Reliability patch plus evaluation tooling. No public memory contract change;
`CONTRACT_VERSION` stays `0.1.0`.

### Added

- LongMemEval weighted sampling (`--type-weight TYPE=N`) and the
  `vexic.longmemeval_analysis` miss-classification module for diagnosing
  retrieval misses in eval runs (#237).

### Fixed

- Transcript ingest strips harness-injected `<task-notification>` blocks the
  same way as system-reminder blocks, keeping subagent reports and tool
  returns out of searchable transcript text (ADR 0034, #235).
- Transcript recall uses any-token OR FTS semantics with bm25 ranking, so a
  query only partially matching stored text still recalls it (ADR 0036,
  #236).
- Writes to a tombstoned scope fail closed both before and after physical
  purge (ADR 0022, #240).
- Four hosted-layer alpha staging shortcuts are hardened in the in-process
  hosted shell (#239).

### Changed

- GitHub Actions workflows moved off deprecated Node 20 runtimes, are
  SHA-pinned, and Dependabot now watches the actions ecosystem (#238, #241).

[0.1.5]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.5

## [0.1.4] - 2026-07-16

Reliability patch for the internal-alpha hosted stack. No public memory
contract change; `CONTRACT_VERSION` stays `0.1.0`.

### Fixed

- SessionStart priming no longer discards all memory context when a hosted
  read times out mid-response: read-phase transport and decode failures are
  normalized into the degradation path, so prime emits a partial context
  instead of nothing (#231).
- Canonical migration artifacts tolerate additive schema columns and the
  import row loop is one atomic transaction whose rollback failures preserve
  the original import error (#230); a raw libsql DSN string import target
  fails closed on host-owned extension tables instead of skipping the
  pre-import guard (#233).
- Control-plane schema migrations are concurrency-safe (#226), and
  control-plane retirement cuts live access (#227).
- Dream-phase failures are durably recorded and gate the retry clock (#222),
  dream sweep state is stamped with job-completion time (#225), withheld-stamp
  dream chains back off briefly after failures (#224), and the summarize
  watermark holds when SUMMARIZE never ran (#223).
- Light extraction drop counts are durable telemetry, and all-dropped runs
  report partial instead of success (#229).
- Usage is captured under the pydantic-ai property form, and missing usage
  fails loud instead of silently reporting zero (#228).

[0.1.4]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.4

## [0.1.3] - 2026-07-14

Reliability and hardening patch for the internal-alpha hosted stack. No public
memory contract change; `CONTRACT_VERSION` stays `0.1.0`.

### Added

- Remote libSQL queries are bounded by a wall-clock deadline (#213).
- Recorder keeps a local transcript cursor, so the Stop hook no longer rereads
  the full transcript each turn (#208).
- Per-tenant Turso token cache is bounded with LRU eviction (#207).

### Changed

- Transient Turso connect faults are absorbed at the connection boundary (#219).

### Fixed

- Light extraction drops miscited candidates instead of failing the whole
  batch (#201), handles assistant-heavy task transcripts (#211), and filters
  Claude Code harness envelopes out of ingested transcript (#212).
- The dream in-flight lock is a durable control-plane lease; a cancelled dream
  job holds its lease instead of freeing a live scope (#203).
- Every reasoning agent gets output headroom rather than output-sized caps
  (#199).
- Operator CLI honors `VEXIC_CONTROL_PLANE_TARGET`, so `revoke-key` revokes on
  production (#206).
- Release paths from PR #214 hardened (#215).

[0.1.3]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.3

## [0.1.2] - 2026-07-10

Feature and hardening patch for the internal-alpha hosted stack. The public
memory contract gains one additive operation; `CONTRACT_VERSION` stays `0.1.0`.

### Added

- `load_active_context`, a structured sibling of `fresh_context` that returns
  serialized transcript messages a host can replay as model message history
  instead of rendered priming text.
- LongMemEval evaluation harness rehomed into `vexic.longmemeval` with a CLI,
  host-port judge wiring, and an `--allow-live` provider-call gate.
- Hosted control-plane catalog can route to Turso behind a flag, with a
  `migrate_control_plane` migration path.

### Changed

- Dream sweeper storage routes through the customer-target resolver, and raised
  dream-phase output token caps with surfaced sweeper logs.
- Hosted ingest storage `ValueError`s are classified as 5xx and surface only a
  stable error code.

### Fixed

- Dream sweeper state writes lost to reaped Turso Hrana streams are now retried
  (#194).

[0.1.2]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.2

## [0.1.1] - 2026-07-07

Hardening patch for the internal-alpha hosted control plane. The public memory
contract is unchanged; `CONTRACT_VERSION` stays `0.1.0`.

### Added

- `confirm_whole_scope` opt-in on the control-plane purge request. A null
  `target_scope.session_id` erases every session for an agent scope, so that
  whole-scope erasure now requires an explicit flag and can never happen by
  omission: the guard fails before any deletion, even under `dry_run`
  (ADR 0028).

### Changed

- Control-plane destructive operations record their audit atomically under a
  transition-guarded write, and the re-retire path returns a clearer error.

[0.1.1]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.1

## [0.1.0] - 2026-07-06

Initial public release of the Vexic memory core.

### Added

- Local-first, provenance-first memory core for long-running AI agents.
- Public contract models (`MemoryScope`, `MemoryCapability`, `MemoryService`)
  with a versioned contract surface.
- `LocalMemoryService`, a SQLite reference implementation with `sqlite-vec`
  vector search and hybrid retrieval.
- Three-tier memory model: append-only cleaned transcript, staged memory
  candidates, and durable long-term facts, each carrying source provenance.
- Read-only local MCP server exposing `recall_conversation_history` and
  `recall_user_memory`.
- Conformance and reliability test suite.

[0.1.0]: https://github.com/PyroDonkey/vexic/releases/tag/v0.1.0
