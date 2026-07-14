"""Cross-backend storage-exception classifiers (ADR 0019).

The local reference backend is ``sqlite3``, which raises typed exceptions
(:class:`sqlite3.IntegrityError`, :class:`sqlite3.OperationalError`) for SQL
errors. The hosted libSQL/Turso driver (used via
:func:`vexic.storage.connection.connect`) does NOT: for every server-side SQL
error it raises a bare :class:`ValueError` whose message carries a
Rust-formatted Hrana payload, for example::

    Hrana: `stream error: `Error { message: "SQLite error: UNIQUE constraint
    failed: source_transcript_ledger.source_host", code: "SQLITE_CONSTRAINT" }``

So every ``except sqlite3.IntegrityError``/``except sqlite3.OperationalError``
site silently fails to catch the libSQL equivalent once it runs against Turso.
These classifiers recognize BOTH forms, so an adopting site can keep a broad
catch (``except (sqlite3.<Type>, ValueError) as exc:``) and re-raise when the
classifier returns ``False`` -- unrelated ``ValueError``s (and unrelated sqlite
errors) still propagate, never silently swallowed.
"""

from __future__ import annotations

import sqlite3

# libSQL/Hrana ``code:`` fragments and message substrings that mark an
# operational (as opposed to constraint) SQL error. ``SQLITE_ERROR`` covers the
# generic "SQL logic error" bucket (bad column, bad FTS MATCH, missing table);
# busy/locked cover contention; the parse markers cover malformed SQL. These
# mirror what the sqlite3 ``OperationalError`` type spans, so no more is
# swallowed than the native catch already swallowed.
_OPERATIONAL_MARKERS = (
    "SQLITE_ERROR",
    "SQLITE_BUSY",
    "SQLITE_LOCKED",
    "SQLITE_IOERR",
    "SQLITE_CANTOPEN",
    "SQL_PARSE_ERROR",
    "SQL_PARSE",
    "no such table",
    "no such column",
    "no such module",
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
    "unable to open database file",
    "disk i/o error",
    "syntax error",
    "malformed match",
    "fts5:",
)

# Lock/busy/IO conditions that a retry might clear. Mirrors the intent of the
# former ``_RETRYABLE_SQLITE_OPERATIONAL_ERRORS`` tuple in
# ``hosted_control_plane_http.py``, extended to the libSQL message form.
_RETRYABLE_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
    "unable to open database file",
    "disk i/o error",
    "sqlite_busy",
    "sqlite_locked",
    "sqlite_ioerr",
    "sqlite_cantopen",
)

# Errors produced specifically by parsing an FTS5 MATCH expression. Search
# callers intentionally degrade these to no hits because the query cannot be
# evaluated. Other operational faults (missing tables, connectivity, busy
# databases, deadlines) must propagate so an availability failure never looks
# like an authoritative empty result.
_MALFORMED_FTS_MARKERS = (
    "malformed match",
    "fts5: syntax error",
    "unterminated string",
    "unterminated quote",
)


class QueryDeadlineExceeded(ValueError):
    """A remote storage call could not complete within its availability bound.

    This covers a remote libSQL query deadline and bounded prerequisite work
    such as resolving its short-lived database token (ADR 0019 Addendum 7).
    Subclasses :class:`ValueError` so hosted HTTP boundaries route it through
    the retryable 503 ``storage_unavailable`` path. The classifiers recognize
    it by type, not message. The message must never embed SQL text, parameters,
    credentials, or transport details.
    """


class MutationOutcomeUnknown(ValueError):
    """A timed-out remote mutation may have committed after the deadline.

    Unlike :class:`QueryDeadlineExceeded`, this fault is deliberately not
    retryable: the driver exposes no safe cancellation primitive, so blindly
    retrying could duplicate an append-only transcript row or another durable
    observation. The message never includes SQL or parameters.
    """


def _is_reaped_stream_error(message: str) -> bool:
    """True for the Hrana ``api error`` 404 raised when Turso reaps an idle
    stream (~10s) before the next round-trip -- typically ``commit()``. The
    write on that stream is LOST (verified live 2026-07-10), so a retry must
    re-execute on a fresh connection, not just re-commit. Requires the Hrana
    payload context so a domain ``ValueError`` that merely contains the phrase
    is not reclassified as a storage fault. ``message`` is lowercased.
    """
    return "hrana" in message and "stream not found" in message


def _is_remote_connect_error(message: str) -> bool:
    """True for the Hrana ``http error`` raised when the driver cannot reach
    the remote at all (DNS failure, refused, or black-holed TCP connect --
    observed live 2026-07-13). The remote being unreachable is transient from
    the caller's viewpoint, so it classifies as retryable. Requires the Hrana
    payload context so a domain ``ValueError`` that merely mentions connecting
    is not reclassified. ``message`` is lowercased.
    """
    return "hrana" in message and "error trying to connect" in message


def _is_upstream_connect_error(message: str) -> bool:
    """True for the Hrana ``api error`` raised when the Turso edge cannot
    reach the database primary (``connect to upstream failed`` -- observed
    live 2026-07-13 as ``status=502 Bad Gateway``). The edge losing its
    upstream is transient from the caller's viewpoint, so it classifies as
    retryable. Requires the Hrana ``api error`` envelope -- not just any
    Hrana message -- so a payload that merely echoes the phrase is not
    reclassified. The status code is deliberately not pinned: a 503/504
    variant of the same edge fault is equally transient. ``message`` is
    lowercased.
    """
    return (
        "hrana" in message
        and "api error" in message
        and "connect to upstream failed" in message
    )


def _message(exc: BaseException) -> str:
    """Best-effort message text for both ``sqlite3.*`` and the libSQL ``ValueError``.

    Both drivers put the useful text in ``str(exc)``: sqlite3 the plain SQLite
    message, libSQL the Hrana/``code:`` payload. Callers lower-case the result
    when matching case-insensitively.
    """
    return str(exc)


def is_unique_violation(exc: BaseException) -> bool:
    """True when ``exc`` is a UNIQUE/PRIMARY KEY (constraint) violation.

    ``sqlite3`` raises :class:`sqlite3.IntegrityError`; the hosted libSQL driver
    raises a bare :class:`ValueError` whose message contains ``SQLITE_CONSTRAINT``
    or ``UNIQUE constraint failed``. An unrelated ``ValueError`` returns ``False``
    so adopting sites re-raise it.
    """
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    if isinstance(exc, ValueError):
        message = _message(exc)
        return "SQLITE_CONSTRAINT" in message or "UNIQUE constraint failed" in message
    return False


def is_operational_error(exc: BaseException) -> bool:
    """True when ``exc`` is an operational SQL error (bad SQL, contention, ...).

    Preserves the exact reach of the native ``except sqlite3.OperationalError``
    it replaces: ANY :class:`sqlite3.OperationalError` classifies True regardless
    of message. For the hosted libSQL backend it additionally recognizes the bare
    :class:`ValueError` whose Hrana payload carries an operational marker (e.g.
    ``SQLITE_ERROR``/``SQLITE_BUSY``/``SQL_PARSE`` or "no such"/"database is
    locked"/malformed-MATCH text). A pure constraint ``ValueError`` and unrelated
    ``ValueError``s return ``False`` so adopting sites re-raise them.
    """
    if isinstance(exc, sqlite3.OperationalError):
        return True
    if isinstance(exc, (MutationOutcomeUnknown, QueryDeadlineExceeded)):
        return True
    if isinstance(exc, ValueError):
        message = _message(exc).lower()
        return (
            any(marker.lower() in message for marker in _OPERATIONAL_MARKERS)
            or _is_reaped_stream_error(message)
            or _is_remote_connect_error(message)
            or _is_upstream_connect_error(message)
        )
    return False


def is_retryable_operational_error(exc: BaseException) -> bool:
    """True when ``exc`` is an operational error a retry might clear.

    Mirrors the semantics of the former
    ``_is_retryable_sqlite_operational_error`` in ``hosted_control_plane_http.py``
    -- locked/busy/IO/open conditions -- extended to the libSQL ``ValueError``
    form. A non-retryable operational error (e.g. a syntax error) returns
    ``False``; non-storage exceptions return ``False``.
    """
    if isinstance(exc, QueryDeadlineExceeded):
        return True
    if isinstance(exc, MutationOutcomeUnknown):
        return False
    if isinstance(exc, sqlite3.OperationalError) or (
        isinstance(exc, ValueError) and is_operational_error(exc)
    ):
        message = _message(exc).lower()
        return (
            any(marker in message for marker in _RETRYABLE_MARKERS)
            or _is_reaped_stream_error(message)
            or _is_remote_connect_error(message)
            or _is_upstream_connect_error(message)
        )
    return False


def is_malformed_fts_query_error(exc: BaseException) -> bool:
    """True only for an invalid FTS5 MATCH expression.

    SQLite reports these as :class:`sqlite3.OperationalError`; hosted libSQL
    reports the same messages inside a bare Hrana :class:`ValueError`. The
    narrow message check is intentional: callers may treat malformed free-text
    search as no hits, but must not swallow an unavailable or corrupt backend.
    """
    if not isinstance(exc, (sqlite3.OperationalError, ValueError)):
        return False
    if isinstance(exc, (MutationOutcomeUnknown, QueryDeadlineExceeded)):
        return False
    message = _message(exc).lower()
    if isinstance(exc, ValueError) and not message.lstrip().startswith("hrana: `"):
        # A bare domain ValueError can contain parser-like wording too. The
        # hosted driver form is recognizable by its Hrana envelope; without
        # that context it must propagate rather than masquerade as no hits.
        return False
    return any(marker in message for marker in _MALFORMED_FTS_MARKERS) or (
        "no such column:" in message and "match clause" in message
    )
