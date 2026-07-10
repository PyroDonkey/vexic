"""Unit tests for the cross-backend storage-exception classifiers.

These exercise ``vexic.storage.errors`` with directly-constructed exceptions,
so they need no Turso credentials: real ``sqlite3.*`` typed exceptions cover the
local backend, and ``ValueError``s carrying the hosted libSQL/Hrana message form
cover the managed backend (ADR 0019). The Hrana strings below reproduce the
Rust-formatted payload the ``libsql`` driver raises for server-side SQL errors,
e.g.:

    Hrana: `stream error: `Error { message: "SQLite error: UNIQUE constraint
    failed: source_transcript_ledger.source_host, ...", code: "SQLITE_CONSTRAINT" }``
"""

from __future__ import annotations

import sqlite3

from vexic.storage.errors import (
    is_operational_error,
    is_retryable_operational_error,
    is_unique_violation,
)


def _hrana(message: str, code: str) -> ValueError:
    """Build a ``ValueError`` shaped like the libSQL/Hrana error payload."""
    return ValueError(
        'Hrana: `stream error: `Error { message: "SQLite error: '
        f'{message}", code: "{code}" }}`'
    )


# --- is_unique_violation -------------------------------------------------


def test_unique_violation_sqlite_integrity_error() -> None:
    exc = sqlite3.IntegrityError("UNIQUE constraint failed: messages.id")
    assert is_unique_violation(exc) is True


def test_unique_violation_libsql_constraint_code() -> None:
    exc = _hrana(
        "UNIQUE constraint failed: source_transcript_ledger.source_host",
        "SQLITE_CONSTRAINT",
    )
    assert is_unique_violation(exc) is True


def test_unique_violation_libsql_constraint_message_without_code() -> None:
    # Message marker alone should suffice even if the code fragment differs.
    exc = ValueError('SQLite error: UNIQUE constraint failed: candidates.uid')
    assert is_unique_violation(exc) is True


def test_unique_violation_ignores_unrelated_sqlite_operational_error() -> None:
    exc = sqlite3.OperationalError("no such table: messages")
    assert is_unique_violation(exc) is False


def test_unique_violation_ignores_unrelated_value_error() -> None:
    # A plain domain ValueError must NOT be classified as a unique violation,
    # so adopting sites re-raise it instead of swallowing it.
    exc = ValueError("token_budget must be greater than or equal to 0.")
    assert is_unique_violation(exc) is False


def test_unique_violation_ignores_operational_libsql_value_error() -> None:
    exc = _hrana("no such table: messages", "SQLITE_ERROR")
    assert is_unique_violation(exc) is False


# --- is_operational_error ------------------------------------------------


def test_operational_error_sqlite_operational_error_any_message() -> None:
    # The pre-existing sqlite3 catch swallowed EVERY sqlite3.OperationalError;
    # the classifier must preserve that (message-independent for the native type).
    assert is_operational_error(sqlite3.OperationalError("syntax error near X")) is True
    assert is_operational_error(sqlite3.OperationalError("database is locked")) is True
    assert is_operational_error(sqlite3.OperationalError("no such module: fts5")) is True


def test_operational_error_libsql_sqlite_error_code() -> None:
    exc = _hrana("no such column: bogus", "SQLITE_ERROR")
    assert is_operational_error(exc) is True


def test_operational_error_libsql_busy_and_locked() -> None:
    assert is_operational_error(_hrana("database is locked", "SQLITE_BUSY")) is True
    assert is_operational_error(_hrana("database is locked", "SQLITE_LOCKED")) is True


def test_operational_error_libsql_parse_error() -> None:
    exc = ValueError(
        'Hrana: `stream error: `Error { message: "SQL_PARSE_ERROR: '
        'near \\"MATCH\\": syntax error", code: "SQL_PARSE_ERROR" }`'
    )
    assert is_operational_error(exc) is True


def test_operational_error_libsql_bad_fts_match() -> None:
    # A malformed FTS MATCH is the concrete case search_messages swallows.
    exc = _hrana("no such column: nonexistent MATCH clause", "SQLITE_ERROR")
    assert is_operational_error(exc) is True


def test_operational_error_ignores_unrelated_value_error() -> None:
    exc = ValueError("limit must be at least 1.")
    assert is_operational_error(exc) is False


def test_operational_error_ignores_unique_constraint_only_value_error() -> None:
    # A pure constraint violation is not an operational error.
    exc = ValueError('SQLite error: UNIQUE constraint failed: x.y')
    assert is_operational_error(exc) is False


def test_operational_error_ignores_non_storage_exceptions() -> None:
    assert is_operational_error(RuntimeError("boom")) is False
    assert is_operational_error(KeyError("k")) is False


# --- is_retryable_operational_error --------------------------------------


def test_retryable_sqlite_locked_and_busy() -> None:
    assert is_retryable_operational_error(sqlite3.OperationalError("database is locked")) is True
    assert is_retryable_operational_error(sqlite3.OperationalError("database is busy")) is True
    assert (
        is_retryable_operational_error(sqlite3.OperationalError("disk I/O error")) is True
    )
    assert (
        is_retryable_operational_error(
            sqlite3.OperationalError("unable to open database file")
        )
        is True
    )


def test_retryable_sqlite_non_retryable_message() -> None:
    # Preserve the existing semantics: a syntax error is operational but NOT
    # retryable.
    assert (
        is_retryable_operational_error(sqlite3.OperationalError("syntax error near X"))
        is False
    )


def test_retryable_libsql_locked_and_busy() -> None:
    assert is_retryable_operational_error(_hrana("database is locked", "SQLITE_BUSY")) is True
    assert (
        is_retryable_operational_error(_hrana("database is locked", "SQLITE_LOCKED")) is True
    )


def test_retryable_libsql_non_retryable_operational() -> None:
    # An operational libSQL error that is not a lock/busy/io condition is not
    # retryable.
    assert (
        is_retryable_operational_error(_hrana("no such column: bogus", "SQLITE_ERROR"))
        is False
    )


def _hrana_stream_not_found() -> ValueError:
    """The Hrana ``api error`` payload raised when Turso reaps an idle stream
    before ``commit()`` (observed live 2026-07-10; the write is lost, so the
    caller must retry on a fresh connection)."""
    return ValueError(
        "Hrana: `api error: `status=404 Not Found, "
        'body={"error":"stream not found: 68426218:1738176"}``'
    )


def test_operational_error_libsql_stream_not_found() -> None:
    assert is_operational_error(_hrana_stream_not_found()) is True


def test_retryable_libsql_stream_not_found() -> None:
    assert is_retryable_operational_error(_hrana_stream_not_found()) is True


def test_retryable_ignores_unrelated_api_404_value_error() -> None:
    # A 404 api error without the stream-not-found marker (e.g. a deleted
    # database) must not classify as retryable.
    exc = ValueError(
        'Hrana: `api error: `status=404 Not Found, body={"error":"database not found"}``'
    )
    assert is_retryable_operational_error(exc) is False


def test_retryable_ignores_unrelated_value_error() -> None:
    assert is_retryable_operational_error(ValueError("nope")) is False


def test_retryable_ignores_non_storage_exceptions() -> None:
    assert is_retryable_operational_error(RuntimeError("database is locked")) is False
