from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path

import vexic


ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_version_is_declared() -> None:
    assert vexic.__version__ == "0.1.3"


def test_version_matches_pyproject() -> None:
    assert vexic.__version__ == _project_version()


def test_py_typed_marker_ships_with_package() -> None:
    marker = importlib.resources.files("vexic").joinpath("py.typed")
    assert marker.is_file()
