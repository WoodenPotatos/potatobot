"""cogs/patchbot.py's Minecraft adapter.

No adapter here made a real network call before this file existed --
`minecraft_adapter` had zero direct test coverage. A recorded fixture is not
possible for the article-slug guess itself (it is a pattern read off the
live sitemap, not a stable payload -- see docs/patchbot_data_sources.md), so
this drives the adapter against a fake `aiohttp`-shaped session instead,
which is what the guess-then-verify design needs proving: that a resolving
guess is used and a non-resolving one falls back to the hub link.
"""

import unittest

from cogs import patchbot


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    """Maps a URL to a canned status/payload; unlisted URLs 404."""

    def __init__(self, responses):
        self._responses = responses

    def get(self, url, **kwargs):
        status, payload = self._responses.get(url, (404, None))
        return FakeResponse(status, payload)

    def head(self, url, **kwargs):
        status, _ = self._responses.get(url, (404, None))
        return FakeResponse(status)


MANIFEST_URL = patchbot.MOJANG_VERSION_MANIFEST_URL


def _manifest(latest_release):
    return {"latest": {"release": latest_release, "snapshot": latest_release}}


class MinecraftAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_specific_article_is_used_when_it_resolves(self):
        article_url = patchbot.MINECRAFT_ARTICLE_BASE_URL + "minecraft-java-edition-26-3"
        session = FakeSession({
            MANIFEST_URL: (200, _manifest("26.3")),
            article_url: (200, None),
        })

        version, url, title = await patchbot.minecraft_adapter(session)

        self.assertEqual(version, "26.3")
        self.assertEqual(url, article_url)
        self.assertEqual(title, "Minecraft 26.3")

    async def test_every_dot_in_a_patch_version_becomes_a_dash(self):
        article_url = patchbot.MINECRAFT_ARTICLE_BASE_URL + "minecraft-java-edition-26-1-1"
        session = FakeSession({
            MANIFEST_URL: (200, _manifest("26.1.1")),
            article_url: (200, None),
        })

        _, url, _ = await patchbot.minecraft_adapter(session)

        self.assertEqual(url, article_url)

    async def test_a_non_resolving_guess_falls_back_to_the_hub(self):
        """The slug pattern is reverse-engineered, not documented by Mojang,
        so a wrong guess must degrade rather than post a 404 to a guild."""
        session = FakeSession({MANIFEST_URL: (200, _manifest("99.9"))})

        version, url, _ = await patchbot.minecraft_adapter(session)

        self.assertEqual(version, "99.9")
        self.assertEqual(url, patchbot.MINECRAFT_UPDATES_HUB_URL)

    async def test_an_unreachable_article_check_also_falls_back(self):
        """`_minecraft_article_exists` must treat a raised client error as a
        non-match rather than letting it escape and fail the whole tick."""
        import aiohttp

        class RaisingSession(FakeSession):
            def head(self, url, **kwargs):
                raise aiohttp.ClientConnectionError("boom")

        session = RaisingSession({MANIFEST_URL: (200, _manifest("26.3"))})

        version, url, _ = await patchbot.minecraft_adapter(session)

        self.assertEqual(version, "26.3")
        self.assertEqual(url, patchbot.MINECRAFT_UPDATES_HUB_URL)

    async def test_an_unexpected_manifest_shape_still_raises(self):
        session = FakeSession({MANIFEST_URL: (200, {"unexpected": True})})

        with self.assertRaises(patchbot.PatchSourceError):
            await patchbot.minecraft_adapter(session)


NEWS_URL = patchbot.HOYOLAB_NEWS_URL


def _news_list(*subjects):
    return {"data": {"list": [
        {"post": {"subject": subject, "post_id": str(1000 + index)}}
        for index, subject in enumerate(subjects)
    ]}}


class HoyolabAdapterTests(unittest.IsolatedAsyncioTestCase):
    """The title heuristic HSR's `Version 4.6 "Nameless Honor" Update` slipped
    through for over a day of ticks: the old exact three-suffix list
    (`update details`, `what's new`, `update notice`) never matched a bare
    `"... Update"`, even though every one of those suffixes already contains
    `"update"` as a substring. Confirmed live against HoYoLAB on 2026-09-25 --
    see docs/patchbot_data_sources.md."""

    async def test_update_details_still_matches(self):
        session = FakeSession({NEWS_URL: (200, _news_list(
            'Version 4.5 "To Roll the Stars in Astropolis" Update Details'))})

        adapter = patchbot.make_hoyolab_adapter(6)
        post_id, url, title = await adapter(session)

        self.assertEqual("1000", post_id)
        self.assertEqual("https://www.hoyolab.com/article/1000", url)

    async def test_whats_new_still_matches(self):
        session = FakeSession({NEWS_URL: (200, _news_list(
            'Version "Luna VIII" Version Details - What\'s New'))})

        adapter = patchbot.make_hoyolab_adapter(2)
        post_id, _, _ = await adapter(session)

        self.assertEqual("1000", post_id)

    async def test_a_bare_update_now_matches(self):
        """The regression: HSR's real 4.6 post, which the old suffix list
        missed on every tick since it was posted."""
        session = FakeSession({NEWS_URL: (200, _news_list(
            'Version 4.6 "Nameless Honor" Update'))})

        adapter = patchbot.make_hoyolab_adapter(6)
        post_id, _, title = await adapter(session)

        self.assertEqual("1000", post_id)
        self.assertEqual('Version 4.6 "Nameless Honor" Update', title)

    async def test_an_unrelated_post_is_skipped_in_favour_of_a_later_match(self):
        """A feed item that matches neither substring must not stop the scan
        -- the adapter keeps looking rather than raising or guessing."""
        session = FakeSession({NEWS_URL: (200, _news_list(
            "Nameless Honor Event Guide",
            'Version 4.6 "Nameless Honor" Update',
        ))})

        adapter = patchbot.make_hoyolab_adapter(6)
        post_id, _, _ = await adapter(session)

        self.assertEqual("1001", post_id)

    async def test_no_matching_post_raises(self):
        session = FakeSession({NEWS_URL: (200, _news_list(
            "Nameless Honor Event Guide", "Fan Art Contest Winners"))})

        adapter = patchbot.make_hoyolab_adapter(6)
        with self.assertRaises(patchbot.PatchSourceError):
            await adapter(session)

    async def test_an_unexpected_response_shape_still_raises(self):
        session = FakeSession({NEWS_URL: (200, {"unexpected": True})})

        adapter = patchbot.make_hoyolab_adapter(6)
        with self.assertRaises(patchbot.PatchSourceError):
            await adapter(session)


if __name__ == "__main__":
    unittest.main()
