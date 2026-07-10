"""The `VEXIC_CONTROL_PLANE_TARGET` flag and its wiring through
`create_service_from_env` onto the catalog + API-key store."""

import pytest

from tests.fakes.libsql import FakeLibsqlConn
from vexic.hosted import resolve_control_plane_target
from vexic.storage import StorageTarget


def test_resolve_control_plane_target_defaults_local():
    assert resolve_control_plane_target({}) == "local"
    assert resolve_control_plane_target({"VEXIC_CONTROL_PLANE_TARGET": "local"}) == "local"


def test_resolve_control_plane_target_turso():
    assert resolve_control_plane_target({"VEXIC_CONTROL_PLANE_TARGET": "turso"}) == "turso"
    # case/whitespace tolerant, mirroring resolve_storage_backend
    assert resolve_control_plane_target({"VEXIC_CONTROL_PLANE_TARGET": " Turso "}) == "turso"


def test_resolve_control_plane_target_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_control_plane_target({"VEXIC_CONTROL_PLANE_TARGET": "neon"})


class _SharedFakeConn:
    """Hands out one FakeLibsqlConn across every connect() call so schema init
    on the catalog and the key store share one in-memory db."""

    def __init__(self, fake: FakeLibsqlConn) -> None:
        self._fake = fake

    def execute(self, sql, parameters=(), /):
        return self._fake.execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self._fake.executemany(sql, parameters)

    def cursor(self):
        return self._fake.cursor()

    def commit(self):
        self._fake.commit()

    def rollback(self):
        self._fake.rollback()

    def close(self):
        pass

    def __enter__(self):
        self._fake.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fake.__exit__(*exc)


class _ControlTargetProvisioning:
    """Factory seam double that yields a StorageTarget for the control plane."""

    def __init__(self, target: StorageTarget) -> None:
        self.target = target
        self.calls = 0

    def build_control_plane_target(self, env):
        self.calls += 1
        return self.target


def test_factory_local_backend_ignores_control_plane_target(monkeypatch, tmp_path):
    """Default (flag unset) keeps the catalog on the local filesystem."""
    from vexic.hosted_http import create_service_from_env

    monkeypatch.delenv("VEXIC_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("VEXIC_CONTROL_PLANE_TARGET", raising=False)
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))

    target = StorageTarget("libsql://fake-cp", auth_token="s3cr3t-token")
    provisioning = _ControlTargetProvisioning(target)
    service = create_service_from_env(turso_provisioning=provisioning)

    assert provisioning.calls == 0
    assert service.catalog._control_target == tmp_path / "control-plane.db"
    assert service.api_keys._control_target == tmp_path / "control-plane.db"


def test_factory_wires_turso_control_plane_target(monkeypatch, tmp_path):
    """`VEXIC_CONTROL_PLANE_TARGET=turso` threads the StorageTarget onto both
    the catalog and the API-key store, independent of the customer-memory
    backend (kept `local` here so no Turso provisioning machinery runs)."""
    import vexic.hosted_local as hosted_local
    from vexic.hosted_http import create_service_from_env

    fake = FakeLibsqlConn()

    def _fake_connect(target, *, auth_token=None, **kwargs):
        assert isinstance(target, StorageTarget)
        return _SharedFakeConn(fake)

    monkeypatch.setattr(hosted_local, "connect", _fake_connect)
    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "local")
    monkeypatch.setenv("VEXIC_CONTROL_PLANE_TARGET", "turso")
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))

    target = StorageTarget("libsql://fake-cp", auth_token="s3cr3t-token")
    provisioning = _ControlTargetProvisioning(target)
    service = create_service_from_env(turso_provisioning=provisioning)

    assert provisioning.calls == 1
    assert service.catalog._control_target is target
    assert service.api_keys._control_target is target
