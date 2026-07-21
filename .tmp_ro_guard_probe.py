from pathlib import Path
from vexic.storage.transcript import _assert_local_read_only_target, load_messages_since

cases = [
    Path(":memory:"),
    Path("FILE:///tmp/memory.db"),
    " FILE:///tmp/memory.db",
    "file:/tmp/x ",
    "LIBSQL://tenant.turso.io",
    "HTTPS://tenant.turso.io",
    ":Memory:",
]
for c in cases:
    print("---", repr(c), type(c))
    try:
        _assert_local_read_only_target(c)
        print("GUARD_PASS")
        if not isinstance(c, Path) or True:
            p = Path(c)
            print("as_uri", p.resolve().as_uri())
    except Exception as e:
        print("GUARD_RAISE", type(e).__name__, e)
