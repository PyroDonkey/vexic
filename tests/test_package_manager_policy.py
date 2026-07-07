import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _version_tuple(version: str) -> tuple[int, ...]:
    # NOTE(alpha): stable numeric pins only; use packaging.version if prereleases matter.
    return tuple(int(part) for part in version.split("."))


def test_root_remains_uv_managed() -> None:
    for filename in (
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    ):
        assert not (ROOT / filename).exists()


def test_locked_dependencies_clear_known_supply_chain_advisories() -> None:
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    root_lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))

    # Exact range: floor clears the 1.102.0 advisory, ceiling protects
    # downstream installs from an untested 2.x.
    assert "pydantic-ai-slim>=1.102,<2" in root_project["project"]["dependencies"]

    python_packages = {package["name"]: package["version"] for package in root_lock["package"]}
    assert _version_tuple(python_packages["pydantic-ai-slim"]) >= (1, 102, 0)
