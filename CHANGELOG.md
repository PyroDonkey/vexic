# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-07-10

Feature and hardening patch for the internal-alpha hosted stack. The public
memory contract gains one additive operation; `CONTRACT_VERSION` stays `0.1.0`.

### Added

- `load_active_context`, a structured sibling of `fresh_context` that returns
  serialized transcript messages a host can replay as model message history
  instead of rendered priming text.
- LongMemEval evaluation harness rehomed into `vexic.longmemeval` with a CLI,
  host-port judge wiring, and an `--allow-live` provider-call gate (COA-342).
- Hosted control-plane catalog can route to Turso behind a flag, with a
  `migrate_control_plane` migration path (COA-360).

### Changed

- Dream sweeper storage routes through the customer-target resolver, and raised
  dream-phase output token caps with surfaced sweeper logs (COA-352, COA-355).
- Hosted ingest storage `ValueError`s are classified as 5xx and surface only a
  stable error code (COA-356).

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
