from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vexic.storage.operators import (
    create_memory_rebuild_copy,
    export_memory_review,
)


def _existing_db_path(value: str) -> str:
    # The operator functions open the database through `init_db`, which creates
    # a fresh empty schema when the file is absent. Without this check a typo'd
    # --db-path exits 0 after reviewing (or copying) nothing at all.
    if not Path(value).is_file():
        raise argparse.ArgumentTypeError(f"no memory database at {value}")
    return value


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
