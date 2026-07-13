# Versioned docs do not record deployed state

Status: accepted

## Context

`docs/hosted-mvp.md` asserted that the deployed alpha had *not* cut over to
Turso and that `VEXIC_STORAGE_BACKEND` stayed unset. Production had run `turso`
for weeks. An investigator trusted the sentence, read the now-vestigial
`customer-*.db` files on the Railway volume, found them empty, and concluded
Tier 3 held no data and the service had no traffic. It held 898 messages in
Turso. The wrong conclusion was copied into two other issues before anyone
caught it (COA-353).

The audit that followed (COA-354) asked whether that was one typo or a class. It
was a class. Eleven separate sentences across `docs/hosted-mvp.md` asserted what
the live service currently ran. They had already drifted apart from *each
other*: one section said the control plane ran on managed Turso while the
readiness list two hundred lines below still called it "repo-local SQLite," and
both claimed to describe the same deployment. One sentence went further and
recorded a production model *value*.

The common failure is not carelessness. It is that these sentences cannot be
kept true by the process that keeps the rest of the repo true. A claim about
code is verified by reading code, and CI fails when it drifts. A claim about
what a running service currently has set is verified only by looking at the
running service, which no reviewer does and no test can. Such a sentence is
correct on the day it is written and rots silently from then on -- and it rots
*while looking authoritative*, which is what makes it more dangerous than no
documentation at all.

## Decision

**Deployment state is not a property of this repository, and versioned docs do
not record it.**

Concretely, docs under `docs/` may state:

- what the code does;
- what configuration the code reads, and what each setting selects
  (`docs/configuration.md` is the catalogue, enforced by
  `scripts/check_doc_drift.py`);
- the *recipe* a hosted deployment must follow -- "to run the hosted service on
  Turso, set these variables" -- because that is a property of the code's
  requirements.

They may not state:

- what the live service currently has set, is currently running, or currently
  holds on disk;
- any configuration **value** read from a live environment.

Operator truth about a running deployment lives where it can be observed: the
Railway service variables, the control-plane database, and the ops tracker. A
doc that needs to talk about live state points the reader at how to check it,
and does not cache the answer in prose.

The distinction is recipe versus report. "Set `VEXIC_CONTROL_PLANE_TARGET=turso`
to route the catalog to managed Turso" is a recipe: it stays true as long as the
code does. "The deployed alpha runs `VEXIC_CONTROL_PLANE_TARGET=turso`" is a
report, and it is stale the moment someone changes the variable.

## Consequences

- `docs/hosted-mvp.md` states the required config as a recipe and points at
  `railway variables` (names, never values) for what is actually set. Hazard
  notes that used to assert live state -- the vestigial `customer-*.db` files,
  the stale volume `control-plane.db` -- are phrased conditionally on the
  configuration that causes them.
- A reader who wants to know what production runs is told to look, rather than
  handed an answer that may be months old. This is more friction and it is the
  point: the friction is bounded, and the misdiagnosis it prevents was not.
- This does not add a prose lint. A banned-phrase check over docs is too
  false-positive prone to sit in CI, and a false failure blocks merges. The
  enforcement here is structural: with no live-state assertions left in the
  tree, there is nothing to drift. The bidirectional environment-variable check
  in `scripts/check_doc_drift.py` covers the mechanically checkable half --
  every variable the code reads is catalogued, and no catalogued name outlives
  the code that read it.
- The repo therefore cannot answer "what is deployed right now" from a
  checkout, by design. That question is answered by the deployment.
