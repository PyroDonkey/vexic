"""Fail when checked docs contain non-ASCII text."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = (ROOT / "docs",)


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_hidden(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(part.startswith(".") for part in parts)


def _iter_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file() and not _is_hidden(candidate, path):
                    files.append(candidate)
        elif path.is_file() and not _is_hidden(path, path.parent):
            files.append(path)
    return files


def check_paths(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in _iter_files(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{_display_path(path)}: not valid UTF-8: {exc}")
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            for column, char in enumerate(line, start=1):
                if ord(char) > 127:
                    errors.append(
                        f"{_display_path(path)}:{line_number}:{column}: "
                        f"non-ASCII U+{ord(char):04X}"
                    )
    return errors


def main(argv: list[str] | None = None) -> int:
    raw_paths = argv if argv is not None else sys.argv[1:]
    paths = [Path(raw_path) for raw_path in raw_paths] if raw_paths else list(DEFAULT_PATHS)
    errors = check_paths(paths)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
