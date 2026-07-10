# Vexic Extraction Provenance

Vexic was extracted from a private predecessor host under Linear issue COA-138.

- Extraction date: 2026-06-19
- Source repository: a private predecessor host repository
- Source commit: `6001d35` (`COA-172 sync memory docs references`)
- Target repository: Vexic
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

That private predecessor host remains the first-party consumer.

## LongMemEval harness rehome (COA-342)

The LongMemEval memory eval harness was rehomed to `src/vexic/longmemeval.py`
on 2026-07-09 under Linear issue COA-342, after the source-host copy was
deleted in the COA-341 cutover.

- Source commit: `5caf185^` in the private predecessor host repository
  (recover with `git show 5caf185^:engine/evals/longmemeval.py`)
- Source files: `engine/evals/longmemeval.py`,
  `tests/test_longmemeval_eval.py`, `scripts/eval_longmemeval_memory.py`
- Port changes: `engine.*` imports rewritten to `vexic.*`; the source host's
  model factory replaced with host-port agent factories (recall judge factory
  lives in `adapters/openrouter_live_adapter.py`); tenant-secret CLI loading
  replaced with the env-driven adapter pattern behind `--allow-live`; the CLI
  folded into the module as `python -m vexic.longmemeval`.
