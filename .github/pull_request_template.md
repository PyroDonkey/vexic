<!--
Feature PRs target `dev` (squash merge). Release PRs are `dev` -> `main`
(merge commit) and must fast-forward `dev` immediately after merging.
See docs/branch-sync.md for the command sequences.
-->

## Summary

<!-- What changed and why, in a sentence or two. -->

## Linear

<!-- Link the issue so Linear auto-closes it on merge, e.g. "Fixes COA-123".
     Delete this section if there is no issue (chore/* branches). -->

Fixes COA-

## Checklist

- [ ] `uv run pytest` passes fresh (after the final edit)
- [ ] ADR touched? `docs/adr/README.md` index updated
- [ ] `LocalMemoryService` surface changed? "v0.1 Local Service Surface" in `docs/ai/AGENTS.md` updated
- [ ] Reconciliation triggers checked (see "Docs Are Downstream Of Code" in `docs/ai/AGENTS.md`)
