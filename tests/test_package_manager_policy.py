from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_console_is_not_npm_managed() -> None:
    assert not (ROOT / "console" / "package.json").exists()
    assert not (ROOT / "console" / "package-lock.json").exists()


def test_readmes_do_not_document_npm_console_commands() -> None:
    forbidden_commands = ("npm install", "npm test", "npm run build")
    readmes = (
        ROOT / "README.md",
        ROOT / "console" / "README.md",
    )

    for readme in readmes:
        text = readme.read_text(encoding="utf-8")
        for command in forbidden_commands:
            assert command not in text
