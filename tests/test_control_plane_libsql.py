from tests.fakes.libsql import FakeLibsqlConn


def test_fake_rejects_named_params_and_row_factory():
    c = FakeLibsqlConn()
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO t (v) VALUES (?)", ("x",)); c.commit()
    assert c.execute("SELECT v FROM t").fetchone() == ("x",)
    import pytest
    with pytest.raises(AttributeError):
        c.enable_load_extension(True)
