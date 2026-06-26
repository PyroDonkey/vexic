from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"


def test_root_remains_uv_managed() -> None:
    assert not (ROOT / "package.json").exists()
    assert not (ROOT / "package-lock.json").exists()


def test_console_does_not_define_package_manager_surface() -> None:
    for filename in (
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    ):
        assert not (CONSOLE / filename).exists()


def test_readmes_do_not_document_console_npm_flows() -> None:
    readmes = [
        ROOT / "README.md",
        CONSOLE / "README.md",
    ]
    forbidden_fragments = (
        "console/package.json",
        "package-lock.json",
        "npm ci",
        "npm install",
        "npm run",
        "npm test",
        "npm-managed",
    )

    for readme in readmes:
        text = readme.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in text


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
