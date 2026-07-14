"""Search fallbacks distinguish malformed MATCH from storage unavailability."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

import vexic.storage.candidates as candidates
import vexic.storage.longterm as longterm
import vexic.storage.transcript as transcript
from vexic.storage.errors import QueryDeadlineExceeded


class _FaultConnection:
    def __init__(self, fault: BaseException) -> None:
        self._fault = fault

    def execute(self, *_args, **_kwargs):
        raise self._fault

    def close(self) -> None:
        return None


def _search_transcript(db_path: str) -> list[object]:
    return transcript.search_messages(db_path, "cedar")


def _search_long_term(db_path: str) -> list[object]:
    return longterm.keyword_long_term_fact_ids(db_path, "cedar", k=5)


def _search_candidates(db_path: str) -> list[object]:
    return candidates.keyword_candidate_ids(db_path, "cedar", k=5)


_SEARCH_PATHS: tuple[tuple[ModuleType, Callable[[str], list[object]]], ...] = (
    (transcript, _search_transcript),
    (longterm, _search_long_term),
    (candidates, _search_candidates),
)


@pytest.mark.parametrize(("module", "search"), _SEARCH_PATHS)
def test_search_propagates_remote_query_deadline(
    module: ModuleType,
    search: Callable[[str], list[object]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "memory.db")
    fault = QueryDeadlineExceeded("remote query deadline")
    monkeypatch.setattr(module, "connect", lambda _path: _FaultConnection(fault))

    with pytest.raises(QueryDeadlineExceeded) as excinfo:
        search(db_path)
    assert excinfo.value is fault


@pytest.mark.parametrize(("module", "search"), _SEARCH_PATHS)
def test_search_degrades_only_malformed_match_to_no_hits(
    module: ModuleType,
    search: Callable[[str], list[object]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "memory.db")
    fault = sqlite3.OperationalError('fts5: syntax error near "."')
    monkeypatch.setattr(module, "connect", lambda _path: _FaultConnection(fault))

    assert search(db_path) == []


@pytest.mark.parametrize(("module", "search"), _SEARCH_PATHS)
def test_search_propagates_non_match_operational_fault(
    module: ModuleType,
    search: Callable[[str], list[object]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "memory.db")
    fault = sqlite3.OperationalError("no such table: messages_fts")
    monkeypatch.setattr(module, "connect", lambda _path: _FaultConnection(fault))

    with pytest.raises(sqlite3.OperationalError) as excinfo:
        search(db_path)
    assert excinfo.value is fault
