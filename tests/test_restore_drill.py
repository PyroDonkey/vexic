"""`run_restore_drill` decision-logic orchestration.

Creds-free: every dependency (`provision_replacement`, `import_canonical`,
`verify`, `activate`, `destroy`) is an injected fake callable that records
its own call count. No adapter, no secrets, no network -- this exercises
only the sequencing/rollback logic in `src/vexic/restore.py`.
"""

from __future__ import annotations

import pytest

from vexic.restore import run_restore_drill


class _Recorder:
    """Records calls made to it; args/kwargs -> return value via a lambda."""

    def __init__(self, fn=None):
        self.calls: list[object] = []
        self._fn = fn

    def __call__(self, *args):
        self.calls.append(args)
        if self._fn is not None:
            return self._fn(*args)
        return None

    @property
    def call_count(self) -> int:
        return len(self.calls)


def test_verify_passes_activates_and_does_not_destroy():
    replacement = object()
    provision_replacement = _Recorder(lambda: replacement)
    import_canonical = _Recorder()
    verify = _Recorder(lambda _replacement: True)
    activate = _Recorder()
    destroy = _Recorder()

    result = run_restore_drill(
        provision_replacement=provision_replacement,
        import_canonical=import_canonical,
        verify=verify,
        activate=activate,
        destroy=destroy,
    )

    assert result.activated is True
    assert result.replacement is replacement
    assert activate.call_count == 1
    assert activate.calls[0] == (replacement,)
    assert destroy.call_count == 0
    assert import_canonical.call_count == 1
    assert verify.call_count == 1


def test_verify_fails_destroys_and_does_not_activate():
    replacement = object()
    provision_replacement = _Recorder(lambda: replacement)
    import_canonical = _Recorder()
    verify = _Recorder(lambda _replacement: False)
    activate = _Recorder()
    destroy = _Recorder()

    result = run_restore_drill(
        provision_replacement=provision_replacement,
        import_canonical=import_canonical,
        verify=verify,
        activate=activate,
        destroy=destroy,
    )

    assert result.activated is False
    assert result.replacement is replacement
    assert destroy.call_count == 1
    assert destroy.calls[0] == (replacement,)
    assert activate.call_count == 0


def test_import_raises_best_effort_destroys_and_reraises():
    replacement = object()
    provision_replacement = _Recorder(lambda: replacement)

    def _boom(_replacement):
        raise RuntimeError("import blew up")

    import_canonical = _Recorder(_boom)
    verify = _Recorder(lambda _replacement: True)
    activate = _Recorder()
    destroy = _Recorder()

    with pytest.raises(RuntimeError, match="import blew up"):
        run_restore_drill(
            provision_replacement=provision_replacement,
            import_canonical=import_canonical,
            verify=verify,
            activate=activate,
            destroy=destroy,
        )

    assert destroy.call_count == 1
    assert destroy.calls[0] == (replacement,)
    assert activate.call_count == 0
    assert verify.call_count == 0


def test_verify_raises_best_effort_destroys_and_reraises():
    replacement = object()
    provision_replacement = _Recorder(lambda: replacement)
    import_canonical = _Recorder()

    def _boom(_replacement):
        raise RuntimeError("verify blew up")

    verify = _Recorder(_boom)
    activate = _Recorder()
    destroy = _Recorder()

    with pytest.raises(RuntimeError, match="verify blew up"):
        run_restore_drill(
            provision_replacement=provision_replacement,
            import_canonical=import_canonical,
            verify=verify,
            activate=activate,
            destroy=destroy,
        )

    assert destroy.call_count == 1
    assert destroy.calls[0] == (replacement,)
    assert activate.call_count == 0


def test_destroy_failure_after_verify_failure_does_not_mask_original_error():
    # Not applicable to verify-failure path (no exception to preserve), but
    # a destroy raising here should propagate on its own -- there is no
    # "original error" being masked since verify failing is not an exception.
    replacement = object()
    provision_replacement = _Recorder(lambda: replacement)
    import_canonical = _Recorder()
    verify = _Recorder(lambda _replacement: False)
    activate = _Recorder()

    def _destroy_boom(_replacement):
        raise RuntimeError("destroy blew up")

    destroy = _Recorder(_destroy_boom)

    with pytest.raises(RuntimeError, match="destroy blew up"):
        run_restore_drill(
            provision_replacement=provision_replacement,
            import_canonical=import_canonical,
            verify=verify,
            activate=activate,
            destroy=destroy,
        )

    assert activate.call_count == 0


def test_compensating_destroy_failure_does_not_mask_original_exception():
    replacement = object()
    provision_replacement = _Recorder(lambda: replacement)

    def _boom(_replacement):
        raise RuntimeError("import blew up")

    import_canonical = _Recorder(_boom)
    verify = _Recorder(lambda _replacement: True)
    activate = _Recorder()

    def _destroy_boom(_replacement):
        raise RuntimeError("destroy also blew up")

    destroy = _Recorder(_destroy_boom)

    with pytest.raises(RuntimeError, match="import blew up"):
        run_restore_drill(
            provision_replacement=provision_replacement,
            import_canonical=import_canonical,
            verify=verify,
            activate=activate,
            destroy=destroy,
        )

    assert destroy.call_count == 1
    assert activate.call_count == 0
