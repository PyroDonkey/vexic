"""Reproduce _uncheckpointed_wal_db fixture logic and compare counts."""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.storage.db import connect, init_db
from vexic.storage.transcript import save_messages


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
        db_path = str(Path(temp_dir) / "memory.db")
        init_db(db_path)  # already sets journal_mode=WAL
        holder = stack.enter_context(closing(connect(db_path)))
        holder.execute("SELECT COUNT(*) FROM messages").fetchone()
        save_messages(
            db_path,
            [ModelRequest(parts=[UserPromptPart(content="I ran the race last Sunday")])],
            session_id="s1",
            agent_id=None,
            timestamp="2023-11-17T09:30:00+00:00",
        )
        wal_path = Path(f"{db_path}-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else None
        print("wal_exists", wal_path.exists())
        print("wal_size", wal_size)

        immutable_uri = f"{Path(db_path).resolve().as_uri()}?immutable=1"
        with closing(sqlite3.connect(immutable_uri, uri=True)) as conn:
            immutable_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        print("immutable_count", immutable_count)

        with closing(connect(db_path)) as conn:
            normal_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        print("normal_count", normal_count)


if __name__ == "__main__":
    main()
