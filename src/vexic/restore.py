"""Verify-gated, generation-stamped restore-drill orchestration.

`run_restore_drill` is PURE ORCHESTRATION over injected callables: it reads
NO secrets, imports NO adapter, and does no I/O of its own. Everything that
touches Turso, the filesystem, or the hosted catalog is supplied by the
caller as a callable (see `scripts/turso_restore_drill.py` for the real
wiring). This keeps the decision logic -- provision, import, verify,
activate-or-destroy -- unit-testable with plain fakes and reviewable without
cross-referencing adapter code.

Sequence:
    1. `replacement = provision_replacement()`
    2. `import_canonical(replacement)`
    3. `ok = verify(replacement)`
    4. if `ok`: `activate(replacement)` (repoints the catalog + bumps
       `generation`, quarantining the pre-repoint handle) -> `activated=True`.
    5. else: `destroy(replacement)`; the ORIGINAL stays active, untouched ->
       `activated=False`.

If `import_canonical` or `verify` raises before activation, this makes a
best-effort `destroy(replacement)` call (swallowing any destroy failure, so
a broken teardown never masks the original error) and then re-raises the
original exception. `activate` is never called on that path, so the
original database is never deactivated and the replacement is never leaked
un-destroyed (best-effort).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

ReplacementT = TypeVar("ReplacementT")


@dataclass(frozen=True)
class RestoreDrillResult(Generic[ReplacementT]):
    """Outcome of a `run_restore_drill` call.

    `activated` is True only when `verify` passed and `activate` ran
    (repointing the catalog and bumping `generation`); False when `verify`
    failed and the replacement was destroyed instead, leaving the original
    active. `replacement` is always the handle `provision_replacement()`
    returned, even on the `activated=False` path (useful for logging what
    was provisioned and destroyed).
    """

    activated: bool
    replacement: ReplacementT


def run_restore_drill(
    *,
    provision_replacement: Callable[[], ReplacementT],
    import_canonical: Callable[[ReplacementT], None],
    verify: Callable[[ReplacementT], bool],
    activate: Callable[[ReplacementT], None],
    destroy: Callable[[ReplacementT], None],
) -> RestoreDrillResult[ReplacementT]:
    """Run the restore drill: provision -> import -> verify -> activate-or-destroy.

    All five parameters are callables injected by the caller (see module
    docstring); this function performs no I/O and reads no secrets itself.
    """
    replacement = provision_replacement()
    try:
        import_canonical(replacement)
        ok = verify(replacement)
    except BaseException:
        _best_effort_destroy(destroy, replacement)
        raise

    if ok:
        activate(replacement)
        return RestoreDrillResult(activated=True, replacement=replacement)

    destroy(replacement)
    return RestoreDrillResult(activated=False, replacement=replacement)


def _best_effort_destroy(
    destroy: Callable[[ReplacementT], None], replacement: ReplacementT
) -> None:
    try:
        destroy(replacement)
    except BaseException:
        # Swallow: a broken compensating teardown must never mask the
        # original import/verify exception that triggered it. The
        # replacement may be left behind in this case -- that is an
        # accepted, documented limitation of "best-effort" cleanup.
        pass
