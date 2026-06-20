# Host-triggered, Vexic-committed promotion

Status: accepted

Promotion from Tier 1 transcript into Tier 2 candidates and Tier 3 durable facts
is host-triggered and Vexic-committed. Hosts decide when to ask for promotion,
which sessions or message ranges are in scope, and which host-supplied model
ports are available. Vexic owns the write semantics: category validation,
redaction, provenance, `source_message_ids`, candidate/fact lifecycle, and
non-destructive supersession.

Vexic v0.1 should not run an automatic background promotion daemon or infer a
global promotion schedule. A host may choose to request promotion after every
completed run, on a timer, on user action, or through its own review workflow,
but those policies stay outside the core package.

Promotion tools and adapters must not let agents write directly to the
database. Agent-facing surfaces may request promotion through the public memory
contract or a thin adapter, but Vexic remains the gatekeeper for Tier 2 and Tier
3 persistence. Candidate fallback remains tentative retrieval material until a
promotion path commits a durable fact with transcript provenance.
