import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"


def test_next_env_avoids_generated_next_type_imports() -> None:
    text = (CONSOLE / "next-env.d.ts").read_text(encoding="utf-8")
    tsconfig = json.loads((CONSOLE / "tsconfig.json").read_text(encoding="utf-8"))

    assert '/// <reference types="next" />' in text
    assert '/// <reference types="next/image-types/global" />' in text
    assert ".next/dev/" not in text
    assert ".next/" not in text
    assert ".next/types/**/*.ts" in tsconfig["include"]
    assert ".next/dev/types/**/*.ts" in tsconfig["include"]
