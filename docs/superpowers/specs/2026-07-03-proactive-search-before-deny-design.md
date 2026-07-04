# Proactive memory search: search-before-deny backstop

Status: approved (design)
Date: 2026-07-03

## Problem

Vexic already instructs the model to search memory proactively, in three
places:

- `RECALL_CONVERSATION_HISTORY_DESCRIPTION` / `RECALL_USER_MEMORY_DESCRIPTION`
  (tool descriptions)
- `server_instructions()` — the `WHEN TO SEARCH` block
- `build_prime_context()` — the session-start priming block

Despite this, the model still fails to search when a request is
retrieval-shaped but not phrased as a memory reference. Observed failure: asked
"Look up the secret response for the keyword ZEPHYR-TANGERINE-77," a fresh chat
answered "No secret keyword lookup tool exists... Made-up test string, likely"
and only searched after being told "Check vexic memory" — at which point it
found the stored value immediately.

Root cause: every existing trigger is scoped to *user-facts* — "preferences,
personal facts, goals, past decisions," "as I mentioned / last time," or
"before you say you don't know something *about the user*." A bare retrieval
request matches none of these lexically, so the model pattern-matches it as
noise and *confidently fabricates a negative* instead of checking.

## Decision

Add a high-precision **search-before-deny backstop**: before the model tells
the user that something doesn't exist, isn't in memory, is made-up, or that it
can't recall it, it must search first. This was chosen over broadening the
positive trigger list (or doing both) because it fires only right before a
confident negative — the moment a missed search is most damaging — and so
carries low over-search risk.

### Guardrails

1. **Domain scope.** The backstop fires only on denials about things memory
   could plausibly hold: what the user shared, asked the model to remember,
   values or names they mentioned, or prior discussion. It must NOT fire on
   world-facts or codebase-facts, or the model would search vexic before saying
   "Python has no such syntax" or "that function isn't in this file." The domain
   qualifier — "something they may have shared, asked you to remember, or
   discussed before" — is what enforces this.

2. **Anti-loop / honest negative.** "Search before denying" is not "never
   deny." A search that returns nothing explicitly licenses the honest "not
   found." Without this the model could second-guess indefinitely.

## Changes

Both edits are in `src/vexic/mcp_presentation.py`. No behavior code changes;
these are model-facing prompt strings.

### Change 1 — `server_instructions()`, WHEN TO SEARCH block

Append to the existing trigger sentence, after "...you don't know something
about the user":

> ...; or you are about to tell the user that something they may have shared,
> asked you to remember, or discussed before doesn't exist, isn't in memory, is
> made-up, or that you can't recall it — search first. A search that returns
> nothing lets you give that honest negative; search before denying, don't
> refuse to deny.

### Change 2 — `RECALL_USER_MEMORY_DESCRIPTION`, tail

Replace the ending "...and before saying you don't know something about them."
with:

> ...and before telling them you don't know or remember something, or that a
> value, name, or fact they ask you to recall doesn't exist or is made-up —
> search first.

### Deliberately unchanged

- `RECALL_CONVERSATION_HISTORY_DESCRIPTION` — its "references something said
  before" trigger already fires correctly.
- `build_prime_context()` priming block, `HOW TO PRESENT RESULTS`, and the
  result renderers — out of scope.

## Testing

- Extend the existing `mcp_presentation` unit tests:
  - assert the new backstop phrase appears in `server_instructions()` for both
    `enable_expand_history=False` and `True`.
  - assert the sharpened tail appears in `RECALL_USER_MEMORY_DESCRIPTION`.
- Run the full `pytest` suite to confirm no snapshot/contract test pins the old
  strings.

## Out of scope

- Cross-machine memory siloing (the two machines use different `project_id`s and
  therefore separate memory silos). This is a hosted-account configuration
  matter, not a prompt change, and is tracked separately.
- Broadening positive triggers or changing search frequency on non-denial turns.
