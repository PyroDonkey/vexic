from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_creation_clears_stale_status_before_navigation() -> None:
    text = (ROOT / "console" / "app" / "console" / "project-list.tsx").read_text(encoding="utf-8")
    clear_status = 'setStatus("");'

    assert clear_status in text
    assert text.index('setStatus("Project creation requires an active organization.");') < text.index(clear_status)
    assert text.index(clear_status) < text.index("router.push(")
