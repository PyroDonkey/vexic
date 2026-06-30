from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def connect(target: str | Path, **kwargs: Any) -> sqlite3.Connection:
    """Open a Vexic storage connection for a local SQLite target.

    This is the single connection seam for Vexic storage. Every storage module
    opens its database through this function instead of calling
    ``sqlite3.connect`` directly, so the hosted Turso/libSQL cutover (ADR 0019)
    can dispatch a remote target here rather than editing every call site.

    Today it is a thin pass-through to ``sqlite3.connect`` that preserves the
    same positional target and keyword arguments (for example ``timeout``).
    """
    return sqlite3.connect(target, **kwargs)
