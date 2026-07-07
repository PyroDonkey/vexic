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
    base_dependencies = root_project["project"]["dependencies"]
    assert "pydantic-ai-slim>=1.102,<2" in base_dependencies

    # The slim swap exists to keep the fat pydantic-ai meta-package (and its
    # provider-SDK tree) out of the base install. Guard against a regression
    # that re-adds it alongside slim. Match the package name exactly: a bare
    # "pydantic-ai" or "pydantic-ai" followed by a version specifier, extras
    # bracket, or environment marker, without false-positiving on
    # "pydantic-ai-slim".
    fat_boundaries = {"=", "<", ">", "!", "~", " ", "[", ";"}

    def _is_fat_pydantic_ai(dep: str) -> bool:
        name = "pydantic-ai"
        if dep == name:
            return True
        return dep.startswith(name) and dep[len(name) : len(name) + 1] in fat_boundaries

    assert not any(_is_fat_pydantic_ai(dep) for dep in base_dependencies)

    python_packages = {package["name"]: package["version"] for package in root_lock["package"]}
    assert _version_tuple(python_packages["pydantic-ai-slim"]) >= (1, 102, 0)
    # The fat meta-package was dropped from the resolved set entirely.
    assert "pydantic-ai" not in python_packages
