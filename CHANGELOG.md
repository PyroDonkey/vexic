# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
