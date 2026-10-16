"""Upstream titles are inert inside the bot's embeds.

A stream, video, song or patch title is chosen by whoever published it, and
each template wraps it in `**…**` beside a link the bot vouches for -- so
`foo](https://evil) [bar` rendered a second, masked link under the bot's
identity. `discord.utils.escape_markdown` does not stop that: it skips anything
that looks like a URL, which is exactly the payload.
"""

import ast
import pathlib
import unittest

from cogs.utils import plain_markdown

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Every locale key that interpolates an upstream title, and where.
SITES = {
    "music.now_playing_desc": "cogs/music.py",
    "socials.twitch_live_desc": "cogs/socials.py",
    "socials.youtube_new_video_desc": "cogs/socials.py",
    "patchbot.new_patch_desc": "cogs/patchbot.py",
}


class PlainMarkdownTests(unittest.TestCase):
    def test_a_masked_link_cannot_be_formed(self):
        escaped = plain_markdown("foo](https://evil) [click](https://evil.example)")
        self.assertNotRegex(escaped, r"(?<!\\)[\[\]]")

    def test_emphasis_and_code_are_escaped(self):
        self.assertEqual(r"\*\*x\*\* \_y\_ \`z\` \~\~s\~\~ \|\|p\|\| a\\b",
                         plain_markdown(r"**x** _y_ `z` ~~s~~ ||p|| a\b"))

    def test_a_line_break_cannot_start_a_new_block(self):
        self.assertEqual("title # Heading", plain_markdown("title\n# Heading"))

    def test_absent_text_is_empty(self):
        self.assertEqual("", plain_markdown(None))


class EverySiteEscapesTests(unittest.TestCase):
    def test_every_title_keyword_passes_through_plain_markdown(self):
        for key, path in SITES.items():
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            calls = [node for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and getattr(node.func, "id", None) == "t"
                     and node.args and isinstance(node.args[0], ast.Constant)
                     and node.args[0].value == key]
            with self.subTest(key=key):
                self.assertEqual(1, len(calls))
                title = next(k.value for k in calls[0].keywords if k.arg == "title")
                self.assertEqual("plain_markdown", getattr(title.func, "id", None),
                                 ast.unparse(title))



class EverydleWinMentionTests(unittest.TestCase):
    """A public win message is plain message content, where a mention pings.

    A nickname is the member's own text, so each `*_public_win` message passes
    it through `escape_mentions`, as `/search`'s LFG ping already does.
    """

    def test_every_public_win_message_escapes_the_display_name(self):
        tree = ast.parse((ROOT / "cogs" / "everydle.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and getattr(node.func, "id", None) == "t"
                 and node.args and isinstance(node.args[0], ast.Constant)
                 and str(node.args[0].value).endswith("_public_win")]
        self.assertEqual(5, len(calls))
        for call in calls:
            user = next(k.value for k in call.keywords if k.arg == "user")
            with self.subTest(key=call.args[0].value):
                self.assertEqual("discord.utils.escape_mentions",
                                 ast.unparse(user.func) if isinstance(user, ast.Call) else None)


if __name__ == "__main__":
    unittest.main()
