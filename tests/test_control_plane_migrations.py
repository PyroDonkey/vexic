"""Concurrent-startup safety of the control-plane schema migrations (COA-386).

Two containers booting against the same control-plane database (Railway rolling
deploy) race the check-then-ALTER migration idiom in
``_init_control_plane_schema``: both read ``PRAGMA table_info`` before either
ALTER commits, and the loser raises ``duplicate column name`` instead of
converging. These tests recreate that interleaving deterministically -- a
wrapper connection runs a full rival initialization at the exact moment between
the victim's column check and its ALTER -- so no thread timing is involved.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


class _StubCursor:
    """Replays rows captured before the rival ran, keeping the victim's view stale."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _RivalOnCheckConnection:
    """Delegate to a real control connection, injecting a rival at the race window.

    When the victim reads ``PRAGMA table_info(<watch_table>)`` for the
    ``fire_on_occurrence``-th time -- the read guarding the racing column --
    the pre-rival rows are captured, the rival initializer runs to completion
    on its own connections, and the stale rows are handed back: exactly the
    interleaving where both containers observed the column missing. Executed
    SQL is recorded in ``state["sqls"]`` so tests can assert the victim really
    attempted the losing ALTER.
    """

    def __init__(
        self, conn, watch_table: str, rival, state: dict, fire_on_occurrence: int
    ) -> None:
        self._conn = conn
        self._watch_table = watch_table
        self._rival = rival
        self._state = state
        self._fire_on_occurrence = fire_on_occurrence

    def execute(self, sql: str, *args):
        self._state["sqls"].append(sql)
        cursor = self._conn.execute(sql, *args)
        if not self._state["fired"] and f"table_info({self._watch_table})" in sql:
            self._state["checks"] += 1
            if self._state["checks"] == self._fire_on_occurrence:
                rows = cursor.fetchall()
                self._state["fired"] = True
                self._rival()
                return _StubCursor(rows)
        return cursor

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _patch_victim_connections(
    monkeypatch, cls, watch_table: str, rival, fire_on_occurrence: int = 1
) -> dict:
    state: dict = {"fired": False, "checks": 0, "sqls": []}
    real_connect = cls._connect_control

    def connect_with_rival(self):
        conn = real_connect(self)
        if state["fired"]:
            return conn
        return _RivalOnCheckConnection(
            conn, watch_table, rival, state, fire_on_occurrence
        )

    monkeypatch.setattr(cls, "_connect_control", connect_with_rival)
    return state


def _column_names(control_db: Path, table: str) -> list[str]:
    with closing(sqlite3.connect(control_db)) as conn:
        return [
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]


def test_catalog_init_survives_rival_migrating_between_check_and_alter(
    tmp_path, monkeypatch
) -> None:
    """A catalog boot converges when a rival container migrates first.

    ``hosted_usage_events`` is created without ``key_id``, so the additive
    ALTER fires on every fresh database -- the rival adds it between the
    victim's column check and its own ALTER. The ``key_id`` check is the
    second ``table_info(hosted_usage_events)`` read (``project_id`` is
    checked first).
    """
    state = _patch_victim_connections(
        monkeypatch,
        HostedTenantCatalog,
        "hosted_usage_events",
        lambda: HostedTenantCatalog(tmp_path),
        fire_on_occurrence=2,
    )

    HostedTenantCatalog(tmp_path)

    # The victim must have lost the race for real: it attempted the ALTER a
    # rival had already applied, and converged by swallowing the duplicate.
    assert any(
        "ALTER TABLE hosted_usage_events ADD COLUMN key_id" in sql
        for sql in state["sqls"]
    )
    columns = _column_names(tmp_path / "control-plane.db", "hosted_usage_events")
    assert columns.count("key_id") == 1


class _PauseOnCheckConnection:
    """Delegate to a real control connection, pausing at the race window.

    The first ``PRAGMA table_info(<watch_table>)`` read signals ``reached`` and
    blocks until ``release`` is set, holding the victim initializer open so a
    rival container's initializer can run (or contend) meanwhile.
    """

    def __init__(self, conn, watch_table: str, reached, release, state: dict) -> None:
        self._conn = conn
        self._watch_table = watch_table
        self._reached = reached
        self._release = release
        self._state = state

    def execute(self, sql: str, *args):
        cursor = self._conn.execute(sql, *args)
        if (
            not self._state["fired"]
            and f"table_info({self._watch_table})" in sql
        ):
            rows = cursor.fetchall()
            self._state["fired"] = True
            self._reached.set()
            assert self._release.wait(timeout=30)
            return _StubCursor(rows)
        return cursor

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_dream_sweep_state_drop_recreate_is_serialized_against_rival(
    tmp_path, monkeypatch
) -> None:
    """A racing boot must not drop the sweep-state table a rival just rebuilt.

    Old-shape ``dream_sweep_state`` (pre-``agent_id``) triggers the
    DROP+recreate migration. The victim pauses between its column check and the
    DROP while a rival initializer runs to completion and records sweep state;
    the victim's resumed DROP must not destroy the rival's table or its row.
    """
    control_db = tmp_path / "control-plane.db"
    with closing(sqlite3.connect(control_db)) as conn:
        conn.execute(
            """
            CREATE TABLE dream_sweep_state (
                tenant_id TEXT PRIMARY KEY,
                last_summarize_watermark INTEGER NOT NULL DEFAULT 0,
                last_dream_completed_at TEXT
            )
            """
        )
        conn.commit()

    reached = threading.Event()
    release = threading.Event()
    state = {"fired": False, "wrapped": False}
    real_connect = HostedTenantCatalog._connect_control

    def connect_with_pause(self):
        conn = real_connect(self)
        if state["wrapped"]:
            return conn
        state["wrapped"] = True
        return _PauseOnCheckConnection(
            conn, "dream_sweep_state", reached, release, state
        )

    monkeypatch.setattr(HostedTenantCatalog, "_connect_control", connect_with_pause)

    errors: list[BaseException] = []

    def run_catalog_init() -> None:
        try:
            HostedTenantCatalog(tmp_path)
        except BaseException as exc:  # noqa: BLE001 - surfaced via assertion
            errors.append(exc)

    victim = threading.Thread(target=run_catalog_init)
    victim.start()
    assert reached.wait(timeout=30)

    def run_rival_then_record() -> None:
        try:
            HostedTenantCatalog(tmp_path)
            with closing(sqlite3.connect(control_db, timeout=30)) as conn:
                conn.execute(
                    "INSERT INTO dream_sweep_state "
                    "(tenant_id, agent_id, last_summarize_watermark) VALUES (?, ?, ?)",
                    ("tenant-x", "", 7),
                )
                conn.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced via assertion
            errors.append(exc)

    rival = threading.Thread(target=run_rival_then_record)
    rival.start()
    # Give the rival time to finish (pre-fix) or block on the write lock
    # (post-fix) before the victim resumes its DROP decision.
    rival.join(timeout=1.0)
    release.set()
    victim.join(timeout=30)
    rival.join(timeout=30)
    assert not victim.is_alive() and not rival.is_alive()

    assert errors == []
    columns = _column_names(control_db, "dream_sweep_state")
    assert "agent_id" in columns
    assert "last_dream_failed_at" in columns
    with closing(sqlite3.connect(control_db)) as conn:
        rows = conn.execute(
            "SELECT tenant_id, last_summarize_watermark FROM dream_sweep_state"
        ).fetchall()
    assert rows == [("tenant-x", 7)]


def test_concurrent_catalog_and_key_store_inits_all_converge(tmp_path) -> None:
    """Barrier-released initializers against one control DB all boot cleanly.

    The rolling-deploy regression net: several containers (catalog and key
    store alike) start simultaneously against the same pre-existing old-shape
    database and every one must converge without raising.
    """
    control_db = tmp_path / "control-plane.db"
    with closing(sqlite3.connect(control_db)) as conn:
        conn.execute(
            """
            CREATE TABLE dream_sweep_state (
                tenant_id TEXT PRIMARY KEY,
                last_summarize_watermark INTEGER NOT NULL DEFAULT 0,
                last_dream_completed_at TEXT
            )
            """
        )
        conn.commit()

    factories = [
        lambda: HostedTenantCatalog(tmp_path),
        lambda: HostedTenantCatalog(tmp_path),
        lambda: HostedApiKeyStore(tmp_path),
        lambda: HostedApiKeyStore(tmp_path),
    ]
    barrier = threading.Barrier(len(factories))
    errors: list[BaseException] = []

    def run(factory) -> None:
        barrier.wait(timeout=30)
        try:
            factory()
        except BaseException as exc:  # noqa: BLE001 - surfaced via assertion
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(factory,)) for factory in factories]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads)

    assert errors == []
    assert "last_dream_failed_at" in _column_names(control_db, "dream_sweep_state")
    assert _column_names(control_db, "hosted_usage_events").count("key_id") == 1
    assert _column_names(control_db, "hosted_api_keys").count("last_used_at") == 1


@pytest.mark.parametrize("cls", [HostedTenantCatalog, HostedApiKeyStore])
def test_init_retries_once_when_rival_holds_the_write_lock(
    tmp_path, monkeypatch, cls
) -> None:
    """A boot that loses the write lock re-runs its init instead of dying.

    On Turso the lock's loser surfaces ``database is locked`` instead of
    waiting on the local 30s busy timeout; one retry re-checks the (by then
    migrated) schema on a fresh connection.
    """
    real_connect = cls._connect_control
    attempts = {"count": 0}

    def locked_once(self):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(self)

    monkeypatch.setattr(cls, "_connect_control", locked_once)

    cls(tmp_path)

    assert attempts["count"] == 2


def test_key_store_init_survives_rival_migrating_between_check_and_alter(
    tmp_path, monkeypatch
) -> None:
    """Same race through HostedApiKeyStore's own migration body (last_used_at)."""
    state = _patch_victim_connections(
        monkeypatch,
        HostedApiKeyStore,
        "hosted_api_keys",
        lambda: HostedApiKeyStore(tmp_path),
    )

    HostedApiKeyStore(tmp_path)

    assert any(
        "ALTER TABLE hosted_api_keys ADD COLUMN last_used_at" in sql
        for sql in state["sqls"]
    )
    columns = _column_names(tmp_path / "control-plane.db", "hosted_api_keys")
    assert columns.count("last_used_at") == 1
