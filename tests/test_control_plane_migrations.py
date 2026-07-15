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

    The first time the victim reads ``PRAGMA table_info(<watch_table>)``, the
    pre-rival rows are captured, the rival initializer runs to completion on its
    own connections, and the stale rows are handed back -- exactly the
    interleaving where both containers observed the column missing.
    """

    def __init__(self, conn, watch_table: str, rival, state: dict) -> None:
        self._conn = conn
        self._watch_table = watch_table
        self._rival = rival
        self._state = state

    def execute(self, sql: str, *args):
        cursor = self._conn.execute(sql, *args)
        if (
            not self._state["fired"]
            and f"table_info({self._watch_table})" in sql
        ):
            rows = cursor.fetchall()
            self._state["fired"] = True
            self._rival()
            return _StubCursor(rows)
        return cursor

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _patch_victim_connections(monkeypatch, cls, watch_table: str, rival) -> None:
    state = {"fired": False}
    real_connect = cls._connect_control

    def connect_with_rival(self):
        conn = real_connect(self)
        if state["fired"]:
            return conn
        return _RivalOnCheckConnection(conn, watch_table, rival, state)

    monkeypatch.setattr(cls, "_connect_control", connect_with_rival)


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
    victim's column check and its own ALTER.
    """
    _patch_victim_connections(
        monkeypatch,
        HostedTenantCatalog,
        "hosted_usage_events",
        lambda: HostedTenantCatalog(tmp_path),
    )

    HostedTenantCatalog(tmp_path)

    columns = _column_names(tmp_path / "control-plane.db", "hosted_usage_events")
    assert columns.count("key_id") == 1


def test_key_store_init_survives_rival_migrating_between_check_and_alter(
    tmp_path, monkeypatch
) -> None:
    """Same race through HostedApiKeyStore's own migration body (last_used_at)."""
    _patch_victim_connections(
        monkeypatch,
        HostedApiKeyStore,
        "hosted_api_keys",
        lambda: HostedApiKeyStore(tmp_path),
    )

    HostedApiKeyStore(tmp_path)

    columns = _column_names(tmp_path / "control-plane.db", "hosted_api_keys")
    assert columns.count("last_used_at") == 1
