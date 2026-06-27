import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"


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


def test_console_node_engine_pins_node_20_at_next_minimum() -> None:
    package = json.loads((CONSOLE / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((CONSOLE / "package-lock.json").read_text(encoding="utf-8"))

    expected_node_engine = ">=20.9.0 <21"
    assert package["engines"]["node"] == expected_node_engine
    assert package_lock["packages"][""]["engines"]["node"] == expected_node_engine


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

    for name in (
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
        "CLERK_SECRET_KEY",
        "NEXT_PUBLIC_CLERK_SIGN_IN_URL",
        "NEXT_PUBLIC_CLERK_SIGN_UP_URL",
        "NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL",
        "NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL",
        "VEXIC_INTERNAL_ORG_ID",
        "VEXIC_HOSTED_API_BASE_URL",
    ):
        assert name in env_example
        assert name in console_readme

    required_section = console_readme.split("Route defaults:", 1)[0]
    assert "VEXIC_HOSTED_API_BASE_URL" not in required_section
    assert "Reserved until hosted endpoints are wired:" in console_readme
    assert "Reserved until hosted control-plane endpoints are wired." in env_example
