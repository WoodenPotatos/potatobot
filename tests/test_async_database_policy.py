import ast
import asyncio
import pathlib
import time
import unittest

from core import database


ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Pure functions in `database` that open no connection and may therefore be
#: called straight from the event loop. The rule this test enforces is "no
#: blocking database work off the executor", not "never name the module", and
#: arithmetic over the level curve is not database work. Kept as an explicit
#: list rather than letting a direct `from database import …` slip past the
#: check, which would exempt every accessor at once.
# `ValidationError` is here because raising `database.ValidationError(...)`
# from a cog is, to the AST walk below, a call on the module -- and it is the
# one exception type a cog is meant to raise to name a refusal. Constructing an
# exception is not database work; the purity test below holds it to that.
PURE_HELPERS = {"level_for_xp", "xp_for_level", "ValidationError"}


class AsyncDatabasePolicyTests(unittest.TestCase):
    def test_the_allowlist_names_only_functions_that_touch_nothing(self):
        """A name added here must be pure, or the allowlist becomes the hole.

        `get_connection` is the only way into SQLite in that module, so a helper
        whose source does not reach it cannot block.
        """
        import inspect

        for name in PURE_HELPERS:
            function = getattr(database, name)
            source = inspect.getsource(function)
            self.assertNotIn("get_connection", source,
                             f"{name} opens a connection and is not pure")
            self.assertNotIn("conn", source, f"{name} takes a connection")

    def test_async_code_uses_database_executor(self):
        violations = []
        paths = [ROOT / "main.py", *sorted((ROOT / "cogs").glob("*.py"))]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parents = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"database", "database_layer"}
                    and node.func.attr not in {"run", "run_read", "run_write"}
                    and node.func.attr not in PURE_HELPERS
                ):
                    continue
                current = node
                while current in parents:
                    current = parents[current]
                    if isinstance(current, ast.AsyncFunctionDef):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} database.{node.func.attr}"
                        )
                        break
                    if isinstance(current, (ast.FunctionDef, ast.Lambda)):
                        break
        self.assertEqual(violations, [])


class DatabaseExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_heartbeat(self, awaitable):
        """Keep Python 3.14's test selector awake for cross-thread callbacks."""
        running = True

        async def heartbeat():
            while running:
                await asyncio.sleep(0.01)

        task = asyncio.create_task(heartbeat())
        try:
            return await awaitable
        finally:
            running = False
            await task

    async def test_read_pool_allows_overlapping_operations(self):
        def slow_read():
            time.sleep(0.1)
            return True

        started = time.monotonic()
        results = await self.run_with_heartbeat(
            asyncio.gather(
                database.run_read(slow_read),
                database.run_read(slow_read),
            )
        )
        self.assertEqual(results, [True, True])
        self.assertLess(time.monotonic() - started, 0.19)

    async def test_writer_remains_serialized(self):
        active = 0
        peak = 0

        def slow_write():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.03)
            active -= 1

        await self.run_with_heartbeat(
            asyncio.gather(
                database.run_write(slow_write),
                database.run_write(slow_write),
            )
        )
        self.assertEqual(peak, 1)


if __name__ == "__main__":
    unittest.main()
