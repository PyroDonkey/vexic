import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class VexicSchemaOwnershipTests(unittest.TestCase):
    def test_init_db_does_not_create_background_tool_audit(self) -> None:
        from vexic.storage import init_db

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            init_db(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertIn("messages", tables)
        self.assertNotIn("background_tool_audit", tables)


if __name__ == "__main__":
    unittest.main()
