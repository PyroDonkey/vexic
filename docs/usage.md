# Vexic usage notes

This page keeps setup and integration examples out of the root README. For the
short project overview, see `../README.md`.
Environment variables are catalogued in [`configuration.md`](configuration.md).

Vexic stores cleaned conversation history, stages possible memories for review,
and promotes durable facts with provenance. Agents can recall prior work without
replaying raw logs or guessing at stale context.

Reliable agent memory matters because recall needs to be auditable, scoped, and
reversible. Vexic treats transcript rows as the source of truth, keeps derived
search indexes rebuildable, and records where each long-term fact came from so
memory behavior can be tested, migrated, and debugged.

Vexic is for engineers building agent products, internal automation, or
research systems that need local-first memory primitives today and a path to
hosted integrations later. The current package is a Python core with a SQLite
reference service, public contract models, retrieval primitives, and
conformance tests.

## Running the Project

Install and test the Python memory core with `uv`:

```powershell
uv run pytest
```

Vexic Console and the marketing website source live in the private
`PyroDonkey/vexic-website` repository, not this one (open-core boundary; see
ADR 0012's addendum). This repository's root remains `uv`-managed with no
Node package surface.

## Local MCP MVP

Run the read-only stdio MCP server against a local Vexic database:

```powershell
uv run vexic mcp-stdio --db-path .\memory.db --tenant-id local --session-id default
```

`vexic mcp-stdio` is the packaged launcher and ships with the `vexic` package
entry point. `scripts\vexic-mcp-stdio.py` remains available for hosted
recorder-proxy mode (`--recorder-config`).
Pass `--agent-id <id>` to bind the server to one agent-specific memory scope;
omit it to bind the server to the explicit shared agent scope.

By default, the MVP exposes the `recall_conversation_history` and
`recall_user_memory` MCP tools only. Transcript writes, export, delete,
rebuild, and admin tools are intentionally not registered. Long-term vector
search uses a host-supplied embedding adapter when one is configured,
otherwise it uses the optional local embedding adapter from
`vexic[local-embed]`. Without that extra, `recall_user_memory` returns an
actionable configuration error.

Privileged verbatim history egress is disabled by default. For a local,
session-bound agent that explicitly needs it, pass `--enable-expand-history` to
register `expand_history`. That tool requires `MemoryCapability.EXPAND_HISTORY`,
uses the configured scope only, applies forbidden-value redaction before
egress, and caps both returned messages and returned text. The local stdio MVP
does not yet have a dedicated audit hook for this privileged egress path.

Codex-style MCP config:

```toml
[mcp_servers.vexic]
command = "uv"
args = [
  "run",
  "vexic",
  "mcp-stdio",
  "--db-path",
  ".\\memory.db",
  "--tenant-id",
  "local",
  "--session-id",
  "default",
  # Optional agent-specific memory scope:
  # "--agent-id",
  # "agent-a",
  # Optional privileged egress:
  # "--enable-expand-history",
]
cwd = "<absolute-path-to-vexic-repo>"
```

Claude Code local MCP config:

```powershell
claude mcp add --scope local vexic -- uv run vexic mcp-stdio --db-path .\memory.db --tenant-id local --session-id default
```

The stdio tool schemas cap `query` at 1000 characters, `limit` at 1-20 results,
and privileged `expand_history` responses at 100 returned messages and 20000
characters.

### Native Agent Memory

[ADR 0004](adr/0004-native-agent-memory-is-host-integration-policy.md)
treats runtime-native memory suppression as host integration policy. When Claude
Code, Codex, or another local agent is connected to Vexic, disable that
runtime's own durable memory where the runtime exposes a supported switch. Vexic
core cannot prevent local runtime memory writes and must not grow Claude-,
Codex-, or provider-specific suppression code.

For Claude Code, disable auto memory in the settings layer used to launch the
Vexic-connected agent:

```json
{
  "autoMemoryEnabled": false
}
```

Alternatively, launch Claude Code with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
`CLAUDE.md` remains useful project instruction context, but it is prompt
context rather than storage enforcement.

For Codex/local agents, keep Codex memories disabled for the Vexic profile. If a
profile would otherwise enable memories, pin the Vexic profile off:

```toml
[features]
memories = false

[memories]
generate_memories = false
use_memories = false
disable_on_external_context = true
```

If a runtime cannot disable native memory, Vexic is authoritative only for
memory ingested through its recorder or importer path. Runtime-local memory
remains outside Vexic replay, export, redaction, and deletion semantics.
For the host transcript recorder flow, see
[Claude Code Transcript Import](#claude-code-transcript-import) and
[ADR 0002](adr/0002-host-recorders-ingest-complete-cleaned-transcripts.md).

## Claude Code Transcript Import

For hosted Claude Code recording, install the user-local hook and recorder
config:

```powershell
uv run --with-editable . python -m vexic.cli setup claude-code --base-url https://api.vexic.dev --api-key <raw-key> --project-id project-a --session-id session-a
```

The setup command updates the user's Claude Code hook config and writes a Vexic
recorder config outside the repository. It installs a Stop hook for writes and a
SessionStart hook for best-effort read priming on `startup` and `clear`;
`resume` is skipped to avoid duplicate context dumps. No `.mcp.json` is written.
The Stop hook is installed async: a transient hosted 5xx, 429/408 (a 429
honors an integer-seconds `Retry-After`), or connectivity failure is retried
with jittered backoff under an end-to-end ingest deadline (`--deadline-seconds`,
default 100s, inside the hook's 120s kill), and then reported as a non-blocking
warning in the recorder status file instead of derailing the conversation; the
next run re-posts from the start and the hosted ledger dedups (any other hosted
4xx auth/config fault still surfaces loudly). Installs made before this
behavior must re-run `vexic setup claude-code` to upgrade the Stop hook to
async.

Read-only memory search is opt-in (ADR 0027). Instead of writing any client
config, setup *prints* a `claude mcp add vexic -- ...` command and leaves it to
you to run. That command names the local stdio launcher plus the *path* to the
recorder config, so it never contains the raw API key; the credentials are read
fresh from the owner-only recorder config each run. Memory search stays off
until you run the printed command, which is the deliberate enable step.

Setup also works from a plain `pip install vexic` (no source checkout and no
`uv` required): run `python -m vexic.cli setup claude-code ...` and the hooks
invoke the installing interpreter directly, while the printed connect command
launches `python -m vexic.mcp_stdio_main --recorder-config ...`. Long-term
semantic search through the local MCP server needs the embedding extra, so
install with `pip install 'vexic[local-embed]'` if you want `search_long_term`
available.

On Claude Code stop events, the recorder reads the JSONL transcript, keeps
visible user/assistant text, maps source keys as
`claude-code`/`sessionId`/`uuid`, and posts cleaned rows to the hosted
`/v1/ingest_source_transcript` route. On eligible SessionStart events, the
primer reads the same recorder config, calls hosted read endpoints with Bearer
auth plus `X-Vexic-*` scope headers, and emits capped Claude Code
`additionalContext`. The enabled MCP entry reads the same recorder config and
proxies targeted read-only MCP requests to hosted `/mcp`.

To replay a missed hosted hook manually, point the recorder at the setup config
and a hook payload containing `session_id` and `transcript_path`:

```powershell
uv run --with-editable . python -m vexic.cli recorder ingest --config "$env:USERPROFILE\.vexic\claude-code-recorder.json" --hook-input .\claude-hook-replay.json
```

To replay SessionStart priming manually, provide a hook payload with
`{"source":"startup"}` or `{"source":"clear"}`:

```powershell
uv run --with-editable . python -m vexic.cli recorder prime --config "$env:USERPROFILE\.vexic\claude-code-recorder.json" --hook-input .\claude-session-start.json
```

For local recovery/import, import cleaned Claude Code JSONL transcript rows into
a local Vexic database:

```powershell
uv run python scripts\import-claude-code-jsonl.py --db-path .\memory.db --tenant-id local --session-id default <path-to-session.jsonl>
```

The importer is a repo-local host transcript recorder. It reads Claude Code
JSONL, keeps visible user/assistant text, maps source keys as
`claude-code`/`sessionId`/`uuid`, and delegates writes to
`LocalMemoryService.ingest_source_transcript`. It does not expose MCP writes.

## Connect Codex

Codex gets the read-only MCP connect leg only; the transcript recorder (write
path) stays Claude-Code-only for now. Setup writes an owner-only credential file
and *prints* Codex's own `codex mcp add` command; running that command is the
deliberate, opt-in enable step (ADR 0027).

```powershell
uv run --with-editable . python -m vexic.cli setup codex --base-url https://api.vexic.dev --api-key <raw-key> --project-id project-a --session-id session-a
```

Pass a single-use console setup token with `--token <token>` instead of the
manual `--api-key`/`--project-id`/`--session-id` credentials; the two are
mutually exclusive. Setup writes `~/.vexic/codex-mcp.json` (owner-only, holding
the same `base_url`/`api_key`/`project_id`/`session_id`/`agent_id?` shape the
stdio proxy reads) and prints a `codex mcp add vexic -- ...` command that names
only the local stdio launcher plus the *path* to that credential file, so the
raw key never appears in the command or in any client config. No hooks,
`settings.json`, or `.mcp.json` are written. Memory search stays off until you
run the printed command.

To disconnect, delete the credential file and remove the MCP entry:

```powershell
uv run --with-editable . python -m vexic.cli recorder uninstall-codex
```

That prints `codex mcp remove vexic` for you to run.

## Connect a generic MCP client

Clients without a dedicated installer use the generic path. It writes
`~/.vexic/<name>-mcp.json` (owner-only, same shape) and prints the local stdio
launcher command plus instructions to register it as an MCP server named
`vexic` in whatever config that client uses:

```powershell
uv run --with-editable . python -m vexic.cli setup mcp-client myagent --base-url https://api.vexic.dev --api-key <raw-key> --project-id project-a --session-id session-a
```

`<name>` must be a safe filename component (letters, digits, `.`, `_`, `-`; no
path separators). `--token` exchange works here too, mutually exclusive with the
manual credentials. As with Codex, no raw key appears in the printed command,
and no client config is mutated. Remove the credential file with:

```powershell
uv run --with-editable . python -m vexic.cli recorder uninstall-mcp-client myagent
```

<!-- memory-reliability-gate -->

The memory reliability gate is:

```powershell
uv run pytest tests/test_memory_reliability.py
```

<!-- memory-reliability-live-smoke -->

The opt-in live provider retrieval smoke is:

```powershell
uv run --with-editable . python -m vexic.live_retrieval_baseline `
  --allow-live `
  --fixture .\tests\fixtures\longmemeval_s_smoke.jsonl `
  --adapter .\adapters\openrouter_live_adapter.py `
  --provider openrouter `
  --model-group retrieval-smoke `
  --output-dir .\artifacts\live-retrieval `
  --max-rows 1 `
  --max-provider-calls 5 `
  --timeout-seconds 120
```

Without `--allow-live`, the command exits 0 before importing the adapter or
calling providers. The host-owned OpenRouter adapter reads `OPENROUTER_API_KEY`
from the process environment and supplies `build_extraction_agent`,
`build_contradiction_agent`, and `embed_texts`; Vexic core does not read
provider secrets. Every adapter request pins the OpenRouter provider
preference `data_collection: "deny"`, restricting routing to model providers
that neither retain nor train on prompts (transcript and fact text travel in
these requests; see the ADR 0009 telemetry boundary). Pair it with
account-level ZDR in the OpenRouter dashboard for defense in depth; the pin
can reduce provider availability for exotic models. REM is a local heuristic
and makes no provider calls (ADR 0020), which is why a
`--max-provider-calls 5` budget covers the default single-row run. The adapter lives under repo-local `adapters/` by design
because it is host-owned provider wiring, not package core. Embedding can
alternatively use the optional local `vexic[local-embed]` adapter.

Fixture rows are JSONL objects with `id`, `transcript`, `question`, and
`expected_fact`. `transcript` may be a list of strings or `{ "role": "user" |
"assistant", "content": "..." }` objects mapped from a host-supplied
LongMemEval_S artifact. Do not vendor the benchmark artifact into this repo.

`tests/fixtures/extraction_task_transcript_smoke.jsonl` is a committed
synthetic fixture for Light extraction: one assistant-heavy working-session
row (the transcript shape that once extracted zero candidates) and one
stated-preference row as a regression guard. Run it through the same command
with `--max-messages-per-row 15`; the extraction guard is a nonzero Tier 2
candidate count on both rows.

The harness runs each row in a disposable SQLite database and writes
`retrieval_metrics.json` and `answer_synthesis_metrics.json` under
`--output-dir`. Retrieval metrics classify failures as extraction miss,
promotion miss, retrieval miss, candidate fallback, or provider/runtime failure;
answer synthesis is recorded separately as `not_run` with the reserved
`judge_synthesis_issue` taxonomy slot for this retrieval-only smoke.

## LongMemEval Memory Harness

`vexic.longmemeval` is the full LongMemEval benchmark harness (rehomed from the
private source host; see `docs/provenance.md`). Unlike `vexic.run_evals`
(Tier 1 FTS only) and the live retrieval baseline (single-row provider smoke),
it ingests each benchmark question into an isolated per-question SQLite
database, runs the Light -> REM -> Deep dream chain through host-supplied agent
factories, and measures Tier 3 retrieval quality with per-stage diagnostics.

```bash
# Local, provider-free transcript-FTS run:
uv run python -m vexic.longmemeval \
  --dataset /path/to/longmemeval_oracle.json --split oracle \
  --output-dir .eval-runs/longmemeval --skip-dream

# Provider-backed judged-recall run (env-driven adapter secrets). The
# recall judge refuses the adapter's implicit default model, so the judge
# model group must be resolvable: set VEXIC_LIVE_CLAUDE_MODEL (for the
# default --judge-model-group claude) or VEXIC_LIVE_MODEL explicitly.
OPENROUTER_API_KEY=... VEXIC_LIVE_CLAUDE_MODEL=anthropic/claude-sonnet-5 \
  uv run python -m vexic.longmemeval \
  --allow-live --adapter adapters/openrouter_live_adapter.py \
  --dataset /path/to/longmemeval_s_cleaned.json --split s \
  --output-dir .eval-runs/longmemeval --answer-mode judged-recall --limit 12
```

Answer modes: `retrieval-debug` (Tier 1 FTS), `tier2-debug` (Light+REM, no
Deep), `tier3-debug` (full dream then `retrieve_long_term_facts`), and
`judged-recall` (Tier 3 with candidate fallback, graded by an LLM recall judge
built from the adapter's `build_longmemeval_recall_judge_agent`). Each run
writes `predictions.jsonl` and `diagnostics.jsonl` (stage decomposition:
`answer_extracted_to_tier2`, `answer_promoted_to_tier3`,
`answer_retrieved_from_tier3`, `answer_candidate_rank`), plus per-question-type
judged-recall rates. `answer_candidate_rank` ranks the raw active-candidate
population; `answer_candidate_rank_filtered` ranks only the promotion-eligible
subset (excludes promoted, undated-event, and unembedded candidates) as a
filter-surviving approximation of the pool Deep actually scores, so the two
differ when an ineligible candidate outranks the answer. `--selection
stratified` round-robins across question types; repeatable `--type-weight
multi-session=3` takes N rows from that question type per round-robin pass
(others default to 1) for a diagnostic subset weighted toward specific types,
still fully deterministic. `--resume-from-run` skips rows already `ok` in a
prior run's diagnostics. `--max-transient-retries` (default 2) bounds in-run
retries for transient provider-shape faults (malformed JSON /
`finish_reason='error'`) at each provider call site, logged to stderr and
counted in the diagnostics `transient_retry_count`. Dream-phase runs require `--allow-live` and an `--adapter`; the
judge fails closed with `HostPortNotConfigured` when no judge port is supplied.
Do not vendor the LongMemEval benchmark corpus into this repo.

After a judged-recall run, classify every miss by failing stage and build
per-subject fact histograms with the read-only analysis module:

```bash
uv run python -m vexic.longmemeval_analysis \
  --run-dir .eval-runs/longmemeval/<run-id> --dataset /path/to/longmemeval_s_cleaned.json
```

It buckets each miss into class 1 (no live Tier 3 fact contains the gold
answer: extraction vs promotion miss), class 2 (a gold fact exists but ranked
out of the returned top-k; the full RRF rank is recomputed offline from the
persisted per-retriever arrays), or class 3 (join/derivation candidates
flagged `needs_manual_review`, including answers that appear verbatim nowhere
in the transcript). Every `memory.db` is opened read-only; the output is
`analysis_report.json` in the run directory plus a stdout summary.

### Extraction-Prompt Ablation

`scripts/ablate_extraction_prompts.py` is a window-faithful ablation over the
Light extraction instructions. It reconstructs the exact persisted Light windows
from prior LongMemEval run databases (slicing the shared-scope history at the
`dream_runs` watermarks each Light cycle recorded), binds a fixed set of target
cases to their
answer-bearing windows through declarative locator substrings, and runs a
four-condition factorial over additions appended to the adapter's
`EXTRACTION_INSTRUCTIONS`: control (shipped instructions), a granularity/table
addition, an update-scanning addition, and both combined. Each target is scored
against an explicit CNF keyword rubric as a binary HIT or miss per repeat, and
the run reports per-condition recall with variance (mean and sample standard
deviation), candidate volume as a Tier 2 cost proxy, and token-usage deltas.

```bash
uv run python scripts/ablate_extraction_prompts.py \
  --db .eval-runs/<run>/<timestamp>/<case-id>/memory.db \
  --allow-live --repeats 5 --out .eval-runs/extraction-ablation
```

`--db` is repeatable and points at machine-local `.eval-runs/` databases (they
are gitignored, not vendored). The run is gated behind `--allow-live` with a
provider-call budget cap (`--max-provider-calls`, default 140); without
`--allow-live` it prints a skip notice and exits. The `--out` directory receives
`ablation_metrics.json` and `ablation_audit.jsonl`. Pass `--bind-only` to print
the target-to-window binding table and exit before any provider call, which
validates binding without `--allow-live` or budget.

### Light Time-Context Ablation

`scripts/ablate_light_time_context.py` is the evidence harness for ADR 0038. It
reconstructs the Light windows of one or more LongMemEval databases by
re-slicing their history at the default batch size and replays them through two
extraction variants -- `baseline` (transcript rendered without
`observed=` labels, prior temporal paragraph) and `treated` (the shipped
`render_transcript` plus the current `EXTRACTION_INSTRUCTIONS`) -- and reports
five deterministic metrics per repeat, aggregated mean/min/max across repeats.
`fabricated_year_rate` is scored both pre-guard and post-guard.

```bash
uv run python scripts/ablate_light_time_context.py \
  --db .eval-runs/<run>/<question-id>/memory.db \
  --allow-live --repeats 5 --max-windows 8 \
  --out .eval-runs/light-time-context-ablation
```

`--db` is repeatable and points at machine-local `.eval-runs/` databases (they
are gitignored, not vendored). Two `--db` values naming the same physical
database are a config error (exit 2): identity is device plus inode, so a
repeated path, a symlink, and a hard link are all rejected, since measuring one
corpus twice would double its weight in the aggregate metrics and re-spend
budget on it. The run is gated behind `--allow-live` with a
provider-call budget cap (`--max-provider-calls`, default 120); without
`--allow-live` it prints a skip notice and exits. The `--out` directory receives
`ablation_metrics.json` and `ablation_audit.jsonl`. Each
`window_transcript_hash` audit record carries both the `--db` spelling that was
supplied (`db`) and the path it resolved to at collection time (`db_resolved`).

Those windows match what Light actually saw only when that database's history
was consumed at the default batch size, under the default shared agent scope,
in full batches; a run that used a different batch size, an agent-scoped
history, or stopped mid-batch reconstructs different windows. The script's
module docstring carries the full evidence caveats.

Repeats are scheduled atomically over the whole window panel: a repeat runs
every window's every variant or is not scheduled at all, so a truncated run's
repeats all cover the identical panel and no window is ever scored by one
variant alone. A budget below one full panel (the windows actually collected,
which may be fewer than `max_windows`, times the variants) scores nothing. A
transient provider failure is recorded as a `call_error` audit record and voids
that repeat for every variant, leaving the rest of the run intact;
`provider_errors` and per-variant `calls_failed` appear in the metrics document,
voided candidates are marked `voided` in the audit, and a run whose every call
failed writes no artifacts at all. Input databases are opened read-only. To run against non-fixture data, set forbidden values in the module's
`REDACTION` constant; transcripts are checked before any provider call and the
whole artifact payload before anything is written.

### Class-3 Gap Simulation And Probe

Two provider-free harnesses re-measure the class-3 miss analysis against
current code. Both are read-only over LongMemEval run artifacts and take a gap
fixture: a machine-local JSON file naming, per question, the missing
constituents behind that miss (Tier-2 candidate id, Tier-3 fact id, or neither
for transcript-only gaps) plus the match tokens that identify them. Like the
oracle fixture, it is a run-local artifact attached to the issue, not committed.

`scripts/simulate_mentioned_at_promotion.py` answers what the ADR 0037
`mentioned_at` backfill does to promotion eligibility. It copies each frozen
question database to a temporary directory, lets schema init heal the copy, and
reports per gap candidate whether `mentioned_at` derives, whether Deep
promotion eligibility flips, and where the candidate ranks inside the eligible
pool. A flip lands one of three verdicts (with matching summary counters): it
flips within `--deep-top-n` (`flips-eligible-and-ranked`), flips but ranks
outside it (`flips-eligible-outside-top-n`), or flips into a pool no larger than
the top-n slice (`flips-eligible-degenerate-pool`) -- where the top-n trivially
covers the whole pool, so "within top-n" says nothing about ranking. Eligibility
and ranking are not re-derived: it reuses `_deep_eligible`
and `_rank_diagnostic_candidates` from `vexic.longmemeval`.

```bash
uv run python scripts/simulate_mentioned_at_promotion.py \
  --gaps <gap-fixture>.json --out .eval-runs/<out-dir>
```

Artifacts: `promotion_simulation_metrics.json` and
`promotion_simulation_table.md`. The frozen inputs are never opened: the
harness copies each question database to a temporary directory first and reads
and heals only the copy, so no read-only handle can leave a WAL sidecar next to
the source. The result
bounds the undated-event bucket rather than confirming it: it is a post-run
snapshot of the final candidate pool, not a replay of the per-cycle Deep pool,
it does not model Deep's model-backed contradiction check, and it says nothing
about later extraction-prompt changes.

`scripts/probe_class3_gaps.py` classifies each gap against a run's tiers as
`covered` (a live Tier-3 fact matches every token), `tier2-only` (extracted but
never promoted), `tier3-undated` (the fact exists but carries no date, for gaps
that are about datedness), or `absent`. Point it at the frozen runs for the
baseline, or at a fresh run with `--run-dir` to measure the same gaps on current
code; questions with no database under an overridden `--run-dir` are skipped and
listed in the artifact.

```bash
uv run python scripts/probe_class3_gaps.py \
  --gaps <gap-fixture>.json --run-dir .eval-runs/<run>/<timestamp> \
  --out .eval-runs/<out-dir>
```

Artifacts: `class3_gap_probe.json` and `class3_gap_probe.md`. Matching is
deterministic case-folded substring containment over the curated tokens, so
`covered` means the constituent text is present in Tier 3 -- not that the run
answered the question. A gap with an empty token list or a blank token is a
fixture error (exit 2, `gap fixture error`) checked before any question is
probed, since neither can match honestly and would silently misclassify. The
check covers the questions selected for the run -- all of them by default,
only the `--question-id` subset when that flag is passed. Unlike the
simulation, the probe copies nothing and opens the run databases directly:
a read-only open of a WAL-mode run database may create or update `-wal`/`-shm`
sidecars next to it, so byte-frozen provenance is the simulation harness's
guarantee (it heals a copy), not the probe's.

### Deep Backlog Replay

`scripts/replay_deep_backlog.py` measures whether the promotion-eligible Tier-2
backlog drains across successive Deep cycles or starves. It is read-only and
provider-free over frozen LongMemEval run databases and takes the same gap
fixture as the class-3 harnesses. It reconstructs each historical Deep cycle's
candidate pool from the persisted `dream_runs`, `long_term_memory`, and
`memory_dedup_events` tables -- there is no stored phase column, so the
Light/REM/Deep timeline is rebuilt from each row's counter signature -- and then
forward-simulates a quiescent drain of the healed final backlog to tell a
transient end-of-run backlog apart from structural starvation during ingestion.
Each question lands a drain verdict (`drained-during-run`,
`backlog-at-run-end-transient`, `structural-starvation-during-ingestion`,
`no-deep-cycles`, or `unreliable-attribution`) and each tracked gap candidate
lands its own (`promoted-historically`, `promoted-unattributed`,
`promotes-under-quiescence`, `undrained-at-round-cap`, `never-eligible`, or
`unreliable-attribution`). When the promotion attribution join is inconsistent
the reconstruction is untrustworthy, so both the drain verdict and every tracked
verdict are gated to `unreliable-attribution` rather than emitting an
authoritative classification.

```bash
uv run python scripts/replay_deep_backlog.py \
  --gaps <gap-fixture>.json --out .eval-runs/<out-dir>
```

Artifacts: `deep_backlog_replay_metrics.json` and `deep_backlog_replay_table.md`.
Like the promotion simulation, it copies each frozen database to a temporary
directory and reads and heals only the copy, so the byte-frozen inputs are never
opened. The reconstruction is bounded, not exact. The final-state
`retired`/`stale`/`needs_review` flags approximate each historical cycle's pool.
`rem_boost` history is unrecoverable, so every cycle is scored twice, bracketing
each prediction between the final `rem_boost` and zero. Per-cycle `hit_count` is
exact only through the `memory_dedup_events` merge decisions -- a database with no
merge events reconstructs it trivially exactly. The candidate `importance`,
`occurred_at`, and `source_message_ids` used for each historical cycle are the
final-state values: merges mutate them in place and no per-field history is kept,
so when merges exist those cycle inputs are approximations. The per-question
`state_reconstruction_exact` flag is true only for a zero-merge database whose
stored `hit_count` reconciles against its merge log, telling readers when this
matters. The forward simulation freezes the final `rem_boost` on every survivor,
while production reruns REM centrality each cycle over the shrinking pool -- the
quiescent drain outcome is invariant to this, but the round numbers are
approximate. Attribution spans each Deep cycle's start to the next cycle's start
(the last cycle unbounded above) because `commit_deep_cycle` persists a promoted
fact after the run's `finished_at` is recorded, so a delayed write still
attributes to the cycle that produced it. Deep's model-backed contradiction
judge is not modeled. And the phase classification is signature-based, with a
positional REM-before-Deep fallback for otherwise-ambiguous all-zero rows.

## Hosted MVP Shell

The dependency-free hosted shell in `vexic.hosted` binds authenticated tenant
scope before delegation and can route sanitized request/job usage events to an
adapter-owned telemetry sink. Concrete tenant provisioning, API-key storage,
and the internal-alpha HTTP transport live in adapters outside the memory core.
The Railway alpha at `https://api.vexic.dev` is for throwaway internal testing,
not a public product service. See `docs/hosted-mvp.md`. External
customer-memory readiness is still gated by hosted security, privacy, backup,
and abuse controls.

Hosted transcript writes use the same project/session/agent headers as hosted
MCP reads. The write body does not include `scope` or `tenant_id`; the tenant is
bound from the Agent API key.

Console-created projects expose `tenantId`; Agent API Key create/list responses
include a `scopeTemplate` with the correct `tenant_id`, `project_id`,
`principal`, `trust_boundary`, and key capabilities. Use that template for
direct `/v1/search_*` calls instead of guessing a tenant id.

Claude Code hosted auto-recording is installed with `vexic setup claude-code`.
It writes cleaned transcript rows through `/v1/ingest_source_transcript` and
installs a SessionStart priming hook that injects capped hosted memory context
on new/cleared sessions. It writes no client MCP config: read-only memory search
is opt-in (ADR 0027), so setup only *prints* a `claude mcp add vexic -- ...`
command that you run yourself to connect the local stdio launcher.

The SessionStart primer now leads that injected context with a recap built
from `POST /v1/fresh_context`: a bounded assembly of the session's summary
frontier (produced by the `summarize` dream phase) plus a token-budgeted raw
tail. That call requires the priming key to carry the `memory:fresh-context`
capability; without it, priming falls back to the existing search-only
context. See `docs/hosted-mvp.md` and `docs/adr/0024-hosted-fresh-conversation-context.md`.

```powershell
curl.exe -s https://api.vexic.dev/v1/append_transcript `
  -H "Authorization: Bearer <raw-key>" `
  -H "X-Vexic-Project-Id: project-a" `
  -H "X-Vexic-Session-Id: session-a" `
  -H "Content-Type: application/json" `
  -d "{\"messages_json\":[\"<clean-model-message-json>\"],\"redaction\":{\"forbidden_values\":[]}}"
```

Search the hosted memory API with the copied `scopeTemplate`. Add `session_id`
for session-scoped transcript search:

```powershell
curl.exe -s https://api.vexic.dev/v1/search_transcript `
  -H "Authorization: Bearer <raw-key>" `
  -H "Content-Type: application/json" `
  -d "{\"scope\":{\"tenant_id\":\"tenant_from_console\",\"project_id\":\"project-a\",\"session_id\":\"session-a\",\"agent_id\":\"agent-a\",\"principal\":{\"principal_id\":\"agent-a\",\"principal_type\":\"agent\"},\"trust_boundary\":\"networked\",\"capabilities\":[\"memory:search\"]},\"query\":\"cedar\",\"limit\":5}"

curl.exe -s https://api.vexic.dev/v1/search_long_term `
  -H "Authorization: Bearer <raw-key>" `
  -H "Content-Type: application/json" `
  -d "{\"scope\":{\"tenant_id\":\"tenant_from_console\",\"project_id\":\"project-a\",\"agent_id\":\"agent-a\",\"principal\":{\"principal_id\":\"agent-a\",\"principal_type\":\"agent\"},\"trust_boundary\":\"networked\",\"capabilities\":[\"memory:search\"]},\"query\":\"cedar\",\"limit\":5}"
```

## Hosted Quickstart

End-to-end path from a fresh Console account to an agent that reads Vexic
memory. This consolidates pieces otherwise spread across `README.md`,
`docs/hosted-mvp.md`, and ADR 0010.

1. **Create a Project in the Vexic Console.** The Console returns the project's
   `tenantId`. Everything below is scoped to that tenant; you never guess or
   type a tenant id by hand.

2. **Create an Agent API Key** for that project in the Console. Copy the raw
   key once -- it is not shown again. The create/list response also includes a
   `scopeTemplate` carrying the correct `tenant_id`, `project_id`, `principal`,
   `trust_boundary`, and capabilities; keep it for step 4.

3. **Point your MCP client at the hosted API** with that key. For Claude Code,
   the setup command installs the hook and recorder config (the raw key is
   stored in the owner-only recorder config), then prints the opt-in
   `claude mcp add vexic -- ...` command to enable read-only memory search:

   ```powershell
   uv run --with-editable . python -m vexic.cli setup claude-code --base-url https://api.vexic.dev --api-key <raw-key> --project-id project-a --session-id session-a
   ```

   No `.mcp.json` is written and no client config is auto-mutated. Memory search
   is off until you run the printed `claude mcp add` command -- running it is the
   deliberate, per-client opt-in (ADR 0027). The command references only the
   recorder-config *path*, never the raw key. See
   [Claude Code Transcript Import](#claude-code-transcript-import) for what the
   hook and recorder do. Copying the raw key into this command is the current
   interim path; the accepted target is a console-minted, single-use setup token
   exchanged by the CLI (ADR 0026), owned by follow-up issues.

4. **Make the first read.** Once you have run the printed connect command, the
   agent has two read-only MCP
   tools: `recall_conversation_history` (this and earlier conversations with the
   user) and `recall_user_memory` (durable facts, preferences, and decisions).
   Results come back as **prose the model presents in its own words**, not JSON
   (ADR 0021). To verify the wiring directly against `/mcp` before involving an
   agent, use the smoke request in [Native HTTP MCP](#native-http-mcp); to hit
   the underlying search endpoints (`/v1/search_transcript`,
   `/v1/search_long_term`, backing `src/vexic/hosted_http.py`) with the copied
   `scopeTemplate`, see the curl examples under
   [Hosted MVP Shell](#hosted-mvp-shell).

The MCP tool names (`recall_conversation_history`, `recall_user_memory`) are the
model-facing surface; the `/v1/search_*` routes are the underlying HTTP
endpoints. They name the same reads at different layers.

## Native HTTP MCP

The hosted FastAPI app also exposes `POST /mcp` as a stateless, read-only,
JSON-only Streamable HTTP MCP slice. It requires `Authorization: Bearer
<vexic-api-key>`, binds project/session/agent scope from `X-Vexic-*` headers,
and exposes only `recall_conversation_history` and `recall_user_memory`.

Minimum smoke request:

```powershell
curl.exe -s https://api.vexic.dev/mcp `
  -H "Authorization: Bearer <raw-key>" `
  -H "Accept: application/json, text/event-stream" `
  -H "X-Vexic-Project-Id: project-a" `
  -H "X-Vexic-Session-Id: session-a" `
  -H "Content-Type: application/json" `
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"
```
