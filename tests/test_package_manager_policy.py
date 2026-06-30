import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"


def _version_tuple(version: str) -> tuple[int, ...]:
    # ponytail: stable numeric pins only; use packaging.version if prereleases matter.
    return tuple(int(part) for part in version.split("."))


def _direct_pin(dependencies: list[str], package: str) -> str:
    prefix = f"{package}=="
    for dependency in dependencies:
        if dependency.startswith(prefix):
            return dependency.removeprefix(prefix)
    raise AssertionError(f"{package} is not directly pinned")


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


def test_console_defines_only_isolated_npm_package_surface() -> None:
    assert (CONSOLE / "package.json").exists()
    assert (CONSOLE / "package-lock.json").exists()

    package = json.loads((CONSOLE / "package.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert package["name"] == "vexic-console"
    assert set(package["scripts"]) == {"dev", "build", "start", "test"}

    for filename in (
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    ):
        assert not (CONSOLE / filename).exists()


def test_console_node_engine_pins_node_22_lts_for_vercel() -> None:
    package = json.loads((CONSOLE / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((CONSOLE / "package-lock.json").read_text(encoding="utf-8"))

    expected_node_engine = ">=22.0.0 <23"
    assert package["engines"]["node"] == expected_node_engine
    assert package_lock["packages"][""]["engines"]["node"] == expected_node_engine


def test_locked_dependencies_clear_known_supply_chain_advisories() -> None:
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    root_lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    console_package = json.loads((CONSOLE / "package.json").read_text(encoding="utf-8"))
    console_lock = json.loads((CONSOLE / "package-lock.json").read_text(encoding="utf-8"))

    assert _version_tuple(_direct_pin(root_project["project"]["dependencies"], "pydantic-ai")) >= (
        1,
        102,
        0,
    )

    python_packages = {package["name"]: package["version"] for package in root_lock["package"]}
    assert _version_tuple(python_packages["pydantic-ai"]) >= (1, 102, 0)
    assert _version_tuple(python_packages["pydantic-ai-slim"]) >= (1, 102, 0)

    assert _version_tuple(console_package["overrides"]["next"]["postcss"]) >= (8, 5, 10)
    postcss_packages = {
        name: package["version"]
        for name, package in console_lock["packages"].items()
        if name.endswith("node_modules/postcss")
    }
    assert postcss_packages
    assert all(_version_tuple(version) >= (8, 5, 10) for version in postcss_packages.values())


def test_readmes_scope_console_npm_flows_away_from_core_runtime() -> None:
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    console_readme = (CONSOLE / "README.md").read_text(encoding="utf-8")
    compact_console_readme = " ".join(console_readme.split())

    assert "repository root remains `uv`-managed" in root_readme
    assert "`console/package.json`" in root_readme
    assert "not Vexic package runtime" in root_readme
    assert "Install and test the Python memory core with `uv`" in root_readme

    assert "Root directory: `console/`" in console_readme
    assert "npm install" in console_readme
    assert "npm run build" in console_readme
    assert "not Vexic package runtime" in compact_console_readme


def test_console_env_contract_is_documented() -> None:
    env_example = (CONSOLE / ".env.example").read_text(encoding="utf-8")
    console_readme = (CONSOLE / "README.md").read_text(encoding="utf-8")
    compact_console_readme = " ".join(console_readme.split())

    for name in (
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
        "CLERK_SECRET_KEY",
        "NEXT_PUBLIC_CLERK_SIGN_IN_URL",
        "NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL",
        "VEXIC_INTERNAL_ORG_ID",
        "VEXIC_CONTROL_PLANE_URL",
        "VEXIC_CONTROL_PLANE_TOKEN",
    ):
        assert name in env_example
        assert name in console_readme

    required_section = console_readme.split("Route defaults:", 1)[0]
    assert "NEXT_PUBLIC_CLERK_SIGN_UP_URL" not in env_example
    assert "NEXT_PUBLIC_CLERK_SIGN_UP_URL" not in console_readme
    assert "NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL" not in env_example
    assert "NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL" not in console_readme
    assert "VEXIC_CONTROL_PLANE_URL" not in required_section
    assert "Control-plane backend:" in console_readme
    assert "does not fall back to stub data when a URL is configured" in compact_console_readme
    assert "In production, a missing URL returns an error" in compact_console_readme
    assert "VEXIC_HOSTED_API_BASE_URL" not in env_example
