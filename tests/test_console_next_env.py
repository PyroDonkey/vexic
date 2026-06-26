from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_next_env_does_not_import_ignored_next_output() -> None:
    text = (ROOT / "console" / "next-env.d.ts").read_text(encoding="utf-8")

    assert "./.next/" not in text
