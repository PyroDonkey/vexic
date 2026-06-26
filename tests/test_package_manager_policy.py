import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"


def test_root_remains_uv_managed() -> None:
    assert not (ROOT / "package.json").exists()
    assert not (ROOT / "package-lock.json").exists()


def test_console_is_standalone_npm_package() -> None:
    manifest_path = CONSOLE / "package.json"
    lockfile_path = CONSOLE / "package-lock.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lockfile = json.loads(lockfile_path.read_text(encoding="utf-8"))

    assert manifest["private"] is True
    assert manifest["scripts"]["build"] == "next build"
    assert manifest["scripts"]["test"] == "node --test"
    assert manifest["engines"]["node"] == "24.x"

    expected_dependencies = {
        "@clerk/nextjs": "6.39.5",
        "next": "16.2.9",
        "react": "18.3.1",
        "react-dom": "18.3.1",
    }
    expected_dev_dependencies = {
        "@types/node": "20.17.58",
        "@types/react": "18.3.25",
        "@types/react-dom": "18.3.7",
        "typescript": "5.9.3",
    }

    assert manifest["dependencies"] == expected_dependencies
    assert manifest["devDependencies"] == expected_dev_dependencies
    assert manifest["overrides"]["postcss"] == "8.5.15"

    root_package = lockfile["packages"][""]
    assert lockfile["lockfileVersion"] == 3
    assert root_package["dependencies"] == expected_dependencies
    assert root_package["devDependencies"] == expected_dev_dependencies


def test_console_env_contract_is_documented() -> None:
    env_example = (CONSOLE / ".env.example").read_text(encoding="utf-8")
    console_readme = (CONSOLE / "README.md").read_text(encoding="utf-8")

    for name in (
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
        "CLERK_SECRET_KEY",
        "VEXIC_HOSTED_API_BASE_URL",
        "NEXT_PUBLIC_CLERK_SIGN_IN_URL",
        "NEXT_PUBLIC_CLERK_SIGN_UP_URL",
        "NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL",
        "NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL",
        "VEXIC_INTERNAL_ORG_ID",
    ):
        assert name in env_example
        assert name in console_readme
