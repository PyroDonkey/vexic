# Local embedding and deferrable contradiction lower the LLM floor

Status: accepted

## Context

Vexic already stores embedding metadata and vector projections in the local
core, but the default `embed_texts` implementation was only a missing host-port
stub. That made Tier 2/Tier 3 retrieval and Light candidate embedding depend on
a host embedding credential even though the storage model is host-neutral.

Deep promotion also scored candidates heuristically, but still required a
contradiction judge before promotion. That made contradiction checking part of
the hard model floor even when hosts wanted a first pass that only extracted,
embedded, and promoted memory.

## Decision

Vexic ships an optional local embedding adapter behind the `local-embed` extra.
The adapter lazily loads FastEmbed with `BAAI/bge-small-en-v1.5`, returns
384-dimensional L2-normalized vectors, and is used when no host embedding port
is supplied. Hosts may still pass their own embedding port, and the core package
does not require FastEmbed unless the extra is installed.

Deep contradiction is deferrable. When no contradiction agent is configured and
deferral is enabled, Deep promotes selected candidates without judging
candidate-vs-fact or candidate-vs-pending contradictions. When a contradiction
agent is supplied, the existing supersession and retirement behavior remains
the configured path.

## Consequences

- Minimal dream execution needs Light extraction plus either the optional local
  embedding extra or a host embedding port.
- Retrieval and Light embedding can run without provider secrets or host
  embedding credentials.
- Deferred Deep promotion may temporarily leave contradictory active facts in
  Tier 3 until a later audit retires losers.
- The storage embedding metadata contract stays unchanged:
  `BAAI/bge-small-en-v1.5`, 384 dimensions, L2 distance over unit vectors.
- FastEmbed model loading stays lazy so importing `vexic` does not import or
  initialize model libraries.

## Deferred

- A scheduled Deep audit that runs the contradiction judge over existing Tier 3
  facts and recent promotions.
- Additional local embedding backends or model-selection configuration.
