# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
