# Vexic Extraction Provenance

Vexic was extracted from Coalescent under Linear issue COA-138.

- Extraction date: 2026-06-19
- Source repository: `C:\Users\Ryan\Documents\GitHub\Coalescent`
- Source commit: `6001d35` (`COA-172 sync memory docs references`)
- Target repository: `C:\Users\Ryan\Documents\GitHub\Vexic`
- History strategy: clean provenance snapshot, not git-filtered history

Original source areas:

- `engine/memory_contract/`
- `engine/storage/`
- `engine/pipeline.py`
- `engine/rem.py`
- `engine/deep.py`
- `engine/subagents/retrieval.py`
- `engine/session_context.py`
- `engine/memory_admin.py`
- memory-owned models from `engine/models.py`
- supporting memory utilities: redaction, embeddings, text utilities, time, usage

Coalescent remains the private AgentOS host and first-party consumer.
