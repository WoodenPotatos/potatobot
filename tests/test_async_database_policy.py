import ast
import asyncio
import pathlib
import time
import unittest

import database


ROOT = pathlib.Path(__file__).resolve().parents[1]


class AsyncDatabasePolicyTests(unittest.TestCase):
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
