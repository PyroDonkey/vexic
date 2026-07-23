from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from vexic.storage.connection import connect
from vexic.storage.operators import (
    create_memory_rebuild_copy,
    export_memory_review,
)

# Tier 1, Tier 2, and Tier 3 canonical tables. A file carrying all three is a
# memory database; anything else is a wrong path, whatever its extension.
_REQUIRED_MEMORY_TABLES = frozenset(
    {"messages", "memory_candidates", "long_term_memory"}
)


def _existing_db_path(value: str) -> str:
    # The operator functions open the database through `init_db`, which creates
    # a fresh empty schema when the file is absent. Without this check a typo'd
    # --db-path exits 0 after reviewing (or copying) nothing at all. Existence
    # alone is not enough; `_require_memory_database` finishes the job once the
    # rest of the command line has parsed.
    if not Path(value).is_file():
        raise argparse.ArgumentTypeError(f"no memory database at {value}")
    return value


def _require_memory_database(db_path: str) -> None:
    """Reject a --db-path that is not already an initialized memory database.

    An existing *empty* file passes `_existing_db_path`, and `init_db` would
    then stamp a fresh schema onto it and hand the operator a review of
    nothing -- exactly the mistyped-path failure the existence check exists to
    catch. Probe the schema read-only instead: `mode=ro` opens no file that is
    absent and initializes nothing, so a rejected source is left as it was.
    `Path.as_uri` builds the URI so a path containing `?` or `#` is escaped
    rather than silently reinterpreted as URI query syntax.
    """
    try:
        with closing(
            connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
        ) as conn:
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read {db_path} as a memory database: {exc}") from exc

    missing = sorted(_REQUIRED_MEMORY_TABLES - names)
    if missing:
        raise ValueError(
            f"{db_path} is not an initialized vexic memory database "
            f"(missing tables: {', '.join(missing)})"
        )


def _reject_output_aliasing_source(db_path: str, output_path: Path) -> None:
    """Reject an --output that names the source database itself.

    `review-export --overwrite` would otherwise replace the memory database
    with the markdown report and exit 0. `os.path.realpath` catches the plain
    and symlinked spellings without either path having to exist;
    `Path.samefile` additionally catches a hard link, but raises when either
    path is missing, so it only runs once both are there.
    """
    source = Path(db_path)
    aliased = os.path.realpath(output_path) == os.path.realpath(source)
    if not aliased and output_path.exists() and source.exists():
        aliased = output_path.samefile(source)
    if aliased:
        raise ValueError(
            f"Refusing to write {output_path}: it is the source memory database {db_path}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vexic operator",
        description="Operator-run memory audit and recovery tooling (ADR 0011).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    review_export = subparsers.add_parser(
        "review-export",
        description=(
            "Write a markdown review of Tier 2 candidates and Tier 3 facts "
            "for operator audit."
        ),
    )
    review_export.add_argument("--db-path", type=_existing_db_path, required=True)
    review_export.add_argument("--output", type=Path, required=True)
    review_export.add_argument("--overwrite", action="store_true")
    review_export.add_argument("--forbidden-value", action="append", default=[])

    rebuild_copy = subparsers.add_parser(
        "rebuild-copy",
        description=(
            "Copy the memory database to a new file and rebuild its "
            "projections, for corruption and data-loss recovery."
        ),
    )
    rebuild_copy.add_argument("--db-path", type=_existing_db_path, required=True)
    rebuild_copy.add_argument("--output", type=Path, required=True)
    rebuild_copy.add_argument("--forbidden-value", action="append", default=[])

    return parser


def _review_export(args: argparse.Namespace) -> int:
    _require_memory_database(args.db_path)
    _reject_output_aliasing_source(args.db_path, args.output)
    report = export_memory_review(
        args.db_path,
        args.output,
        forbidden_secret_values=tuple(args.forbidden_value),
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "output_path": str(report.output_path),
                "rows_exported": report.rows_exported,
                "bytes_written": report.bytes_written,
            },
            sort_keys=True,
        )
    )
    return 0


def _rebuild_copy(args: argparse.Namespace) -> int:
    _require_memory_database(args.db_path)
    # `create_memory_rebuild_copy` already refuses any existing --output, so an
    # aliased target is blocked there too. Guard it here as well: the CLI's
    # promise that --output is never the source should not rest on a
    # refuse-to-clobber check that lives in another module.
    _reject_output_aliasing_source(args.db_path, args.output)
    report = create_memory_rebuild_copy(
        args.db_path,
        args.output,
        forbidden_secret_values=tuple(args.forbidden_value),
    )
    repair = report.repair_report
    print(
        json.dumps(
            {
                "ok": True,
                "output_path": str(report.output_path),
                "messages_fts_rows": repair.messages_fts_rows,
                "candidate_fts_rows": repair.candidate_fts_rows,
                "long_term_fts_rows": repair.long_term_fts_rows,
                "candidate_counters_recomputed": repair.candidate_counters_recomputed,
                "long_term_counters_recomputed": repair.long_term_counters_recomputed,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    parser = _parser()
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        if args.command == "review-export":
            return _review_export(args)
        if args.command == "rebuild-copy":
            return _rebuild_copy(args)
        raise ValueError(f"unknown command: {args.command}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
