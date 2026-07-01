import ast
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from vexic.storage.connection import connect


class ConnectSeamTests(unittest.TestCase):
    def test_connect_to_path_returns_usable_sqlite_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "seam.db")
            with closing(connect(db_path)) as conn:
                conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
                conn.execute("INSERT INTO t (v) VALUES (?)", ("hello",))
                conn.commit()
                row = conn.execute("SELECT v FROM t WHERE id = 1").fetchone()
            self.assertEqual(row[0], "hello")

    def test_connect_forwards_keyword_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "seam.db")
            with closing(connect(db_path, isolation_level=None)) as conn:
                self.assertIsNone(conn.isolation_level)

    def test_connect_rejects_plaintext_libsql_auth_token(self) -> None:
        for target in ("http://example.test/db", "ws://example.test/db"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    connect(target, auth_token="secret")


class SeamGateTests(unittest.TestCase):
    """Vexic runtime must open SQLite only through the connect() seam.

    Uses AST (not text search) so comments and docstrings can mention
    ``sqlite3.connect`` without tripping the gate. The seam module itself is
    the one allowed caller.
    """

    def test_sqlite3_connect_only_lives_in_the_seam(self) -> None:
        package_root = Path(__file__).resolve().parent.parent / "src" / "vexic"
        seam = package_root / "storage" / "connection.py"
        offenders: list[str] = []
        for module_path in package_root.rglob("*.py"):
            if module_path == seam:
                continue
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                func = getattr(node, "func", None)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(func, ast.Attribute)
                    and func.attr == "connect"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "sqlite3"
                ):
                    rel = module_path.relative_to(package_root)
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            f"sqlite3.connect must go through vexic.storage.connection.connect: {offenders}",
        )

    def test_storage_code_does_not_iterate_execute_cursors_directly(self) -> None:
        package_root = Path(__file__).resolve().parent.parent / "src" / "vexic"
        offenders: set[str] = set()

        def is_execute_call(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
            )

        for module_path in package_root.rglob("*.py"):
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            scopes = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            for scope in scopes:
                cursor_names = {
                    target.id
                    for node in ast.walk(scope)
                    if isinstance(node, ast.Assign) and is_execute_call(node.value)
                    for target in node.targets
                    if isinstance(target, ast.Name)
                }
                for node in ast.walk(scope):
                    rel = module_path.relative_to(package_root)
                    if isinstance(node, ast.For) and (
                        is_execute_call(node.iter)
                        or (isinstance(node.iter, ast.Name) and node.iter.id in cursor_names)
                    ):
                        offenders.add(f"{rel}:{node.lineno}")
                    elif isinstance(node, ast.comprehension) and (
                        is_execute_call(node.iter)
                        or (isinstance(node.iter, ast.Name) and node.iter.id in cursor_names)
                    ):
                        offenders.add(f"{rel}:{getattr(node, 'lineno', '?')}")

        self.assertEqual(
            sorted(offenders),
            [],
            "libSQL cursors are not iterable; call .fetchall()/.fetchone() before iterating: "
            f"{sorted(offenders)}",
        )


if __name__ == "__main__":
    unittest.main()
