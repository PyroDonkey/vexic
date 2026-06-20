from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path


def main() -> int:
    project_dir = Path(os.environ.get("PROJECT_DIR", "."))
    pyproject_path = project_dir / "pyproject.toml"
    try:
        version = _project_version(pyproject_path)
    except (FileNotFoundError, tomllib.TOMLDecodeError, KeyError) as error:
        print(
            f"::error::unable to read project version from {pyproject_path}: {error}",
            file=sys.stderr,
        )
        return 1
    expected = f"v{version}"
    release_tag = os.environ.get("RELEASE_TAG", "")
    ref_name = os.environ.get("GITHUB_REF_NAME", "")

    if release_tag == expected and ref_name == expected:
        return 0

    print(
        "::error::expected release tag "
        f"{expected!r}; got release_tag={release_tag!r}, ref_name={ref_name!r}",
        file=sys.stderr,
    )
    return 1


def _project_version(path: Path) -> str:
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


if __name__ == "__main__":
    raise SystemExit(main())
