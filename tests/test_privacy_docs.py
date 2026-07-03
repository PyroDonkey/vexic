from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# "Cleaned" strips provider metadata and tool payloads; it never meant
# secret/PII scrubbing. The architecture doc must say so explicitly so nobody
# reads "cleaned" as a privacy guarantee for stored user text.


def test_architecture_states_cleaned_is_not_redacted() -> None:
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    compact = " ".join(text.split()).lower()
    assert "cleaned is not redacted" in compact
    assert "stored verbatim" in compact
