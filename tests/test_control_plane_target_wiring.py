"""The `VEXIC_CONTROL_PLANE_TARGET` flag and its wiring through
`create_service_from_env` onto the catalog + API-key store, and through the
operator CLI (`issue-key` / `revoke-key` / `run-dream-phase`) so runbook
commands operate on the same control plane the serving app authenticates
against."""

import json

import pytest

from tests.fakes.libsql import FakeLibsqlConn
from vexic.contract import MemoryCapability
from vexic.hosted import resolve_control_plane_target
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
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


class _FullFakeProvisioning:
    """Seam double covering both the control-plane target and the
    customer-memory (``VEXIC_STORAGE_BACKEND=turso``) wiring."""

    def __init__(self, target: StorageTarget) -> None:
        self.target = target
        self.resolver_calls = 0

    def build_control_plane_target(self, env):
        return self.target

    def build_port(self, env):
        class _FakePort:
            def create_database(self, name):
                return f"libsql://{name}.fake"

        return _FakePort()

    def build_token_cache(self, port):
        return object()

    def build_resolver(self, token_cache, *, org, env=None):
        self.resolver_calls += 1
        return lambda tenant: None


@pytest.fixture
def turso_control_plane(monkeypatch):
    """Route every StorageTarget connection at one shared in-memory fake."""
    import vexic.hosted_local as hosted_local

    fake = FakeLibsqlConn()

    def _fake_connect(target, *, auth_token=None, **kwargs):
        assert isinstance(target, StorageTarget)
        return _SharedFakeConn(fake)

    monkeypatch.setattr(hosted_local, "connect", _fake_connect)
    monkeypatch.setenv("VEXIC_CONTROL_PLANE_TARGET", "turso")
    monkeypatch.delenv("VEXIC_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("VEXIC_HOSTED_ROOT", raising=False)
    return StorageTarget("libsql://fake-cp", auth_token="s3cr3t-token")


def test_cli_issue_key_honors_turso_control_plane_target(
    turso_control_plane, tmp_path, capsys
):
    """`issue-key` must write to the flag-selected control plane, not the
    local `control-plane.db` under `--root`."""
    from vexic.hosted_http import main

    provisioning = _ControlTargetProvisioning(turso_control_plane)
    rc = main(
        [
            "issue-key",
            "--root",
            str(tmp_path),
            "--tenant-id",
            "t1",
            "--principal-id",
            "p1",
        ],
        turso_provisioning=provisioning,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    # The key authenticates against the Turso control plane...
    verifier = HostedApiKeyStore(control_target=turso_control_plane)
    auth = verifier.authenticate(payload["raw_key"])
    assert auth.tenant_id == "t1"
    # ...and nothing was written to the dead local control plane.
    assert not (tmp_path / "control-plane.db").exists()


def test_cli_revoke_key_honors_turso_control_plane_target(
    turso_control_plane, tmp_path, capsys
):
    """Security pin: a key revoked per the runbook must actually stop
    authenticating against the flag-selected control plane."""
    from vexic.hosted_http import main

    seed_catalog = HostedTenantCatalog(tmp_path, control_target=turso_control_plane)
    seed_catalog.provision_tenant("t1", project_ids=set())
    seed_keys = HostedApiKeyStore(control_target=turso_control_plane)
    api_key = seed_keys.create_key(
        tenant_id="t1",
        principal_id="p1",
        capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
        project_ids=set(),
        agent_ids=set(),
    )

    provisioning = _ControlTargetProvisioning(turso_control_plane)
    rc = main(
        ["revoke-key", "--root", str(tmp_path), "--key-id", api_key.key_id],
        turso_provisioning=provisioning,
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "key_id": api_key.key_id,
        "revoked": True,
    }

    verifier = HostedApiKeyStore(control_target=turso_control_plane)
    with pytest.raises(PermissionError):
        verifier.authenticate(api_key.raw_key)


def test_cli_revoke_key_needs_no_customer_provisioning_vars(
    turso_control_plane, tmp_path, monkeypatch, capsys
):
    """Revocation must never be blocked by customer-memory provisioning
    config: with `VEXIC_STORAGE_BACKEND=turso` but no `TURSO_ORG` /
    platform-API variables, `revoke-key` still revokes against the
    flag-selected control plane."""
    from vexic.hosted_http import main

    seed_catalog = HostedTenantCatalog(tmp_path, control_target=turso_control_plane)
    seed_catalog.provision_tenant("t1", project_ids=set())
    seed_keys = HostedApiKeyStore(control_target=turso_control_plane)
    api_key = seed_keys.create_key(
        tenant_id="t1",
        principal_id="p1",
        capabilities={MemoryCapability.WRITE},
        project_ids=set(),
        agent_ids=set(),
    )

    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "turso")
    monkeypatch.delenv("TURSO_ORG", raising=False)
    provisioning = _ControlTargetProvisioning(turso_control_plane)
    rc = main(
        ["revoke-key", "--root", str(tmp_path), "--key-id", api_key.key_id],
        turso_provisioning=provisioning,
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["revoked"] is True

    verifier = HostedApiKeyStore(control_target=turso_control_plane)
    with pytest.raises(PermissionError):
        verifier.authenticate(api_key.raw_key)


def test_cli_and_factory_resolve_same_control_plane_target(
    turso_control_plane, tmp_path, monkeypatch, capsys
):
    """A key issued through the CLI must authenticate against the service
    `create_service_from_env` builds from the same environment."""
    from vexic.hosted_http import create_service_from_env, main

    provisioning = _ControlTargetProvisioning(turso_control_plane)
    rc = main(
        [
            "issue-key",
            "--root",
            str(tmp_path),
            "--tenant-id",
            "t1",
            "--principal-id",
            "p1",
        ],
        turso_provisioning=provisioning,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))
    service = create_service_from_env(turso_provisioning=provisioning)
    auth = service.api_keys.authenticate(payload["raw_key"])
    assert auth.tenant_id == "t1"


def test_cli_run_dream_phase_honors_control_plane_and_storage_backend(
    turso_control_plane, tmp_path, monkeypatch, capsys
):
    """`run-dream-phase` must authenticate against the flag-selected control
    plane and wire the Turso customer-target resolver when
    `VEXIC_STORAGE_BACKEND=turso`. With no dream-phase adapter configured the
    command fails closed AFTER auth, so a `HostPortNotConfigured` message (not
    an auth failure) proves the control plane was reached."""
    from vexic.hosted_http import main

    seed_catalog = HostedTenantCatalog(tmp_path, control_target=turso_control_plane)
    seed_catalog.provision_tenant("t1", project_ids=set())
    seed_keys = HostedApiKeyStore(control_target=turso_control_plane)
    api_key = seed_keys.create_key(
        tenant_id="t1",
        principal_id="p1",
        capabilities={MemoryCapability.ADMIN_REBUILD},
        project_ids=set(),
        agent_ids=set(),
    )

    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_ORG", "fake-org")
    monkeypatch.setenv("VEXIC_API_KEY", api_key.raw_key)
    monkeypatch.delenv("VEXIC_DREAM_PHASE_ADAPTER", raising=False)
    provisioning = _FullFakeProvisioning(turso_control_plane)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "run-dream-phase",
                "--root",
                str(tmp_path),
                "--tenant-id",
                "t1",
                "--phase",
                "rem",
            ],
            turso_provisioning=provisioning,
        )
    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "host-supplied model port" in stderr
    assert provisioning.resolver_calls == 1
