import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "console"


def test_next_env_uses_only_the_next_generated_route_type_import() -> None:
    text = (CONSOLE / "next-env.d.ts").read_text(encoding="utf-8")
    tsconfig = json.loads((CONSOLE / "tsconfig.json").read_text(encoding="utf-8"))

    assert '/// <reference types="next" />' in text
    assert '/// <reference types="next/image-types/global" />' in text
    assert ".next/dev/" not in text
    assert [line for line in text.splitlines() if line.startswith('import "./.next/')] == [
        'import "./.next/types/routes.d.ts";'
    ]
    assert ".next/types/**/*.ts" in tsconfig["include"]
    assert ".next/dev/types/**/*.ts" in tsconfig["include"]
