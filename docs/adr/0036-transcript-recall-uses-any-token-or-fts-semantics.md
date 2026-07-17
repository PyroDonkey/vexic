# Transcript recall uses any-token OR FTS semantics

Status: accepted

## Context

`search_transcript` matched a query only when **every** query token appeared
in a single message body: `_fts_match_query` quoted each token and
space-joined them, which FTS5 treats as implicit AND. Transcript search was
the only caller using that default; the Tier 3 and Tier 2 keyword legs
already passed `any_token=True` (OR) into the same sanitizer.

ADR 0021 makes the MCP recall tools proactive and prose-first, so models send
sentence-length queries. The unicode61 tokenizer does no stemming and no
camelCase splitting, so a sentence query almost always contains at least one
token absent from the target message, and one dead token emptied the whole
result (COA-391). Live reproduction: a message containing "priming payload",
"startup", and "SessionStart" was missed by the query
`priming payload session start` (token `start` appears nowhere) but found by
`priming payload`. The downstream failure is the model telling the user "no
memory of that" -- a false negative from a recall surface. The eval runner
(`vexic.run_evals`) had already grown a client-side workaround, splitting the
question into per-keyword queries and unioning the hits.

## Decision

Every FTS keyword leg is recall-oriented. `_fts_match_query` always ORs the
sanitized tokens; the `any_token` flag is removed rather than defaulted,
because no caller wants AND semantics. Transcript search keeps its existing
`ORDER BY rank` bm25 ordering. bm25 is an IDF-weighted relevance score, not a
match-count sort: it generally favors messages matching more and rarer query
tokens, but a short message matching one rare token can outrank a long one
matching several common tokens. The result `limit` bounds the widened
candidate set, and a regression test pins that a rare-token target survives
the limit window against stopword-heavy filler.

`run_evals` drops the per-keyword split-and-union workaround and issues the
raw natural-language question as a single query -- the same shape a live MCP
caller sends.

The fuller treatment -- giving transcript recall the Tier 3 hybrid shape
(FTS keyword leg + vector KNN + RRF) -- is deliberately deferred: `messages`
has no embedding table, and embed-at-ingest is a separate cost and schema
decision. It is tracked as its own issue, not smuggled into this change.

## Consequences

- `search_transcript` is a contract-surface operation; this is a deliberate
  semantics change, not a silent one. Broad queries now return
  loosely-related messages where they previously returned nothing; ranked
  ordering plus the `limit` cap is the precision control. A false "no memory"
  is worse than a loosely relevant hit on a recall surface.
- Multi-token AND filtering is no longer expressible through the public
  search operations. No caller relied on it; the eval runner's workaround was
  evidence against it.
- The recorder-ledger dedup test that implicitly pinned AND behavior now
  asserts the OR-ranked hit list, which still proves dedup (no body appears
  twice).
- Eval pass rates from `vexic.run_evals` are not comparable across this
  change: the old runner unioned per-keyword result pages (up to
  keywords x limit bodies), the new runner scores at most `limit` bodies from
  one query. A pass-rate drop against a pre-0036 baseline is expected, not a
  retrieval regression.
