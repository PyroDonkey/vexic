import pytest
from adapters.turso_adapter import control_plane_target
from vexic.storage.connection import StorageTarget

def test_reads_env_into_redacted_target():
    env = {"TURSO_DATABASE_URL": "libsql://db.turso.io", "TURSO_AUTH_TOKEN": "JWT"}
    t = control_plane_target(env)
    assert isinstance(t, StorageTarget) and t.target == "libsql://db.turso.io"
    assert "JWT" not in repr(t)

def test_missing_env_raises():
    with pytest.raises(ValueError):
        control_plane_target({})
