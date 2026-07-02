import pytest
from vexic.storage.connection import StorageTarget, connect

def test_repr_redacts_token():
    t = StorageTarget("libsql://db.turso.io", auth_token="SECRET-JWT")
    assert "SECRET-JWT" not in repr(t)
    assert "SECRET-JWT" not in str(t)
    assert "***" in repr(t)

def test_equality_and_hash_ignore_token():
    a = StorageTarget("libsql://db", auth_token="x")
    b = StorageTarget("libsql://db", auth_token="y")
    assert a == b and hash(a) == hash(b)

def test_as_connect_args():
    assert StorageTarget("p.db").as_connect_args() == ("p.db", None)
    assert StorageTarget("libsql://db", "tok").as_connect_args() == ("libsql://db", "tok")

def test_connect_accepts_storage_target_local(tmp_path):
    tgt = StorageTarget(str(tmp_path / "s.db"))
    conn = connect(tgt)
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()

def test_connect_rejects_double_token():
    with pytest.raises(ValueError):
        connect(StorageTarget("libsql://db", "a"), auth_token="b")

def test_init_db_runs_ddl_once(tmp_path, monkeypatch):
    import vexic.storage.schema as schema
    schema._reset_init_memo()  # test hook
    calls = {"n": 0}
    real_connect = schema.connect
    def counting_connect(target, **kw):
        calls["n"] += 1
        return real_connect(target, **kw)
    monkeypatch.setattr(schema, "connect", counting_connect)
    p = str(tmp_path / "m.db")
    schema.init_db(p); first = calls["n"]
    schema.init_db(p); schema.init_db(p)
    assert first >= 1 and calls["n"] == first  # no reconnect/DDL after first
    schema.init_db(p, force=True)
    assert calls["n"] == first + 1
