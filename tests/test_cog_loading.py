import os
import unittest

import discord
from discord.ext import commands


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CogLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_cogs_load(self):
        bot = commands.Bot(command_prefix="?", intents=discord.Intents.none())
        bot.remove_command("help")
        loaded = []
        try:
            for filename in sorted(os.listdir(os.path.join(ROOT, "cogs"))):
                if not filename.endswith(".py") or filename in {
                    "__init__.py", "utils.py", "database.py"
                }:
                    continue
                extension = f"cogs.{filename[:-3]}"
                await bot.load_extension(extension)
                loaded.append(extension)
            registered = {command.name for command in bot.walk_commands()}
            registered.update(command.name for command in bot.tree.walk_commands())
        finally:
            await bot.close()

        expected = {
            f"cogs.{filename[:-3]}"
            for filename in os.listdir(os.path.join(ROOT, "cogs"))
            if filename.endswith(".py")
            and filename not in {"__init__.py", "utils.py", "database.py"}
        }
        self.assertEqual(set(loaded), expected)

        from cogs.general import get_help_data

        documented = {
            signature.split()[0].lstrip("/")
            for category in get_help_data().values()
            for signature in category["commands"]
        }
        self.assertEqual(registered - documented, set())
        self.assertEqual(documented - registered, set())


if __name__ == "__main__":
    unittest.main()
