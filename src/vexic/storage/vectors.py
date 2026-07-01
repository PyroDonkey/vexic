from __future__ import annotations

import re
import sqlite3
from typing import Protocol

from vexic.embeddings import EMBEDDING_DIM
from vexic.storage.connection import StorageConnection
from vexic.storage.schema import _load_vec_extension, _similarity_from_distance

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _safe_identifier(name: str) -> str:
    # Table and column names are internal constants, never user input, but the
    # vector SQL below is assembled with f-strings, so reject anything that is
    # not a bare SQL identifier as defense in depth against a future caller.
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name

# Dual-backend vector storage + search behind one interface (ADR 0019 cutover).
#
# Only two things actually differ between the local sqlite-vec backend and the
# hosted libSQL backend (proven by the 264c verification spike): the embeddings
# table DDL (a vec0 virtual table vs a native F32_BLOB table) and the KNN query
# (vec0 MATCH/k on L2 distance vs brute-force vector_distance_cos on a managed
# remote, since sqlite-vec cannot load there and the ANN index returned no
# rows). Everything else -- the little-endian float32 blob format and the
# INSERT/DELETE/SELECT of embedding rows -- is byte-for-byte identical, so it
# stays as shared SQL at the call sites rather than leaking into this interface.


class VectorBackend(Protocol):
    def prepare(self, conn: StorageConnection) -> None:
        """Ready ``conn`` for vector work (load the sqlite-vec extension locally;
        a no-op on managed libSQL)."""
        ...

    def create_embeddings_table(
        self, conn: StorageConnection, *, table: str, id_column: str
    ) -> None:
        """Create the ``{table}`` embedding store (id primary key + an
        ``EMBEDDING_DIM``-wide vector column) if it does not already exist."""
        ...

    def knn_subquery(self, *, table: str, id_column: str) -> str:
        """A subquery selecting ``_id`` and ``_distance`` for the nearest stored
        vectors. It binds exactly two params, in order -- the query blob and
        ``k`` -- on both backends, so call sites wrap it uniformly and add their
        own JOINs, filters, and limits."""
        ...

    def similarity(self, distance: float) -> float:
        """Convert this backend's native distance into cosine similarity, so the
        shared dedup/ranking thresholds are backend-independent."""
        ...


class SqliteVecBackend:
    """Local backend using the sqlite-vec loadable extension (``vec0``)."""

    def prepare(self, conn: StorageConnection) -> None:
        _load_vec_extension(conn)  # type: ignore[arg-type]  # sqlite3.Connection only

    def create_embeddings_table(
        self, conn: StorageConnection, *, table: str, id_column: str
    ) -> None:
        table = _safe_identifier(table)
        id_column = _safe_identifier(id_column)
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {table}
            USING vec0(
                {id_column} integer primary key,
                embedding float[{EMBEDDING_DIM}]
            )
            """
        )

    def knn_subquery(self, *, table: str, id_column: str) -> str:
        table = _safe_identifier(table)
        id_column = _safe_identifier(id_column)
        # vec0 exposes ``distance`` (L2) as an output column for a MATCH/k KNN.
        return (
            f"SELECT {id_column} AS _id, distance AS _distance "
            f"FROM {table} WHERE embedding MATCH ? AND k = ?"
        )

    def similarity(self, distance: float) -> float:
        return _similarity_from_distance(distance)


class LibsqlVectorBackend:
    """Hosted backend using native libSQL vectors: an ``F32_BLOB`` column and a
    brute-force ``vector_distance_cos`` scan. sqlite-vec cannot load on a managed
    remote connection and the ANN index (``vector_top_k``) returned no rows in
    the 264c spike, so this is an exact scan -- correct at per-customer
    memory-database scale."""

    def prepare(self, conn: StorageConnection) -> None:
        return None  # no loadable extensions on managed libSQL

    def create_embeddings_table(
        self, conn: StorageConnection, *, table: str, id_column: str
    ) -> None:
        table = _safe_identifier(table)
        id_column = _safe_identifier(id_column)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                {id_column} INTEGER PRIMARY KEY,
                embedding F32_BLOB({EMBEDDING_DIM})
            )
            """
        )

    def knn_subquery(self, *, table: str, id_column: str) -> str:
        table = _safe_identifier(table)
        id_column = _safe_identifier(id_column)
        # libSQL accepts the same little-endian float32 blob as a bound param
        # (spike-verified). ``LIMIT ?`` consumes the same ``k`` param position the
        # vec0 backend spends on ``k = ?``, so both subqueries bind (blob, k).
        return (
            f"SELECT {id_column} AS _id, vector_distance_cos(embedding, ?) AS _distance "
            f"FROM {table} ORDER BY _distance LIMIT ?"
        )

    def similarity(self, distance: float) -> float:
        # vector_distance_cos returns cosine distance; cosine similarity is 1 - d.
        return max(-1.0, min(1.0, 1.0 - distance))


_SQLITE_VEC_BACKEND: VectorBackend = SqliteVecBackend()
_LIBSQL_BACKEND: VectorBackend = LibsqlVectorBackend()


def select_vector_backend(conn: StorageConnection) -> VectorBackend:
    """Pick the vector backend from the live connection type: a real
    ``sqlite3.Connection`` uses sqlite-vec; any other connection is the hosted
    libSQL connection (ADR 0019).

    The type check is the reliable signal: the libSQL connection is a native
    object that cannot carry a marker attribute -- the same reason it has no
    settable ``row_factory`` (264c spike) -- so tagging the connection at
    creation is not an option.
    """
    if isinstance(conn, sqlite3.Connection):
        return _SQLITE_VEC_BACKEND
    return _LIBSQL_BACKEND
