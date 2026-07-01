import pytest
from vexic.storage.connection import StorageTarget

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
