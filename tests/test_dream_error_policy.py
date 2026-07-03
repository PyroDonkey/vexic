import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DREAM_MODULES = ("pipeline.py", "rem.py", "deep.py")

# dream_runs.error_detail and dream-phase prints are diagnostics. They must
# record what failed (exception type, stack shape) and never what the user
# said (fact text, message content, exception messages that embed either).
# The content-free renderer lives in vexic.error_reporting.


def _source(name: str) -> str:
    return (ROOT / "src" / "vexic" / name).read_text(encoding="utf-8")


def test_dream_modules_never_persist_raw_tracebacks() -> None:
    for name in DREAM_MODULES:
        source = _source(name)
        assert "format_exc" not in source, (
            f"{name}: use vexic.error_reporting.format_error_detail instead of "
            "traceback.format_exc; exception messages can embed user content"
        )
        if "error_detail=" in source:
            assert "format_error_detail" in source, name


def test_dream_error_prints_never_interpolate_exception_text() -> None:
    for name in DREAM_MODULES:
        source = _source(name)
        for line in source.splitlines():
            if "parser.exit(" in line:
                # CLI misconfiguration exits render HostPortNotConfigured,
                # whose message is fixed configuration guidance, not content.
                continue
            assert not re.search(r"\{exc[!:}]", line), (
                f"{name}: print type(exc).__name__, not the exception text: {line.strip()}"
            )


def test_candidate_validation_errors_never_embed_fact_text() -> None:
    source = _source("pipeline.py")
    assert "fact_text!r" not in source
    assert "{candidate.fact_text" not in source
