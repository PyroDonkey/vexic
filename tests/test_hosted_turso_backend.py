from vexic.hosted import resolve_storage_backend  # new helper


def test_default_is_local():
    assert resolve_storage_backend({}) == "local"


def test_turso_flag_selected():
    assert resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "turso"}) == "turso"


def test_unknown_flag_rejected():
    import pytest
    with pytest.raises(ValueError):
        resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "postgres"})
