# Native agent memory is host integration policy

Status: accepted

When a host connects an agent runtime to Vexic, the host should disable or
suppress that runtime's native memory system where possible. Durable memory
should flow through Vexic so transcript provenance, Tier 2 candidates, Tier 3
facts, redaction, replay, and deletion semantics stay coherent.

This is not a Vexic core behavior. Vexic cannot reliably prevent Claude Code,
Codex, or another runtime from writing to its own local memory files. Host
integrations own that configuration through runtime settings, project
instructions, hooks, or import/recorder setup.

Vexic may document recommended host configuration and provide adapters that make
the Vexic path easy, but the memory core should not grow runtime-specific
suppression code.
