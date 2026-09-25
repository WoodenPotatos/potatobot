"""Per-game patch-notes announcements.

Watches a fixed roster of real-world games and, when a new patch or update
is published for one, posts a link to it in a channel configured per guild
per game. See docs/patchbot_data_sources.md for what each upstream source
actually is (several were probed and turned out different from the initial
assumption) and docs/subsystems/patchbot.md for the full subsystem rules
this module is governed by.

Structurally this borrows cogs/socials.py's loop/detect/post shape, with
one deliberate difference: socials.py polls per guild because each guild
watches a different streamer list, while a game's latest patch is the same
fact for every guild, so this cog fetches each source once per tick and
fans out to guilds only at the posting step.
"""

import json
import logging
import os
import re
import sys

import aiohttp
import discord
from discord.ext import commands, tasks

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from cogs.utils import guild_setting_sync, handle_loop_error, t
from core import database
from core.feature_access import is_enabled

patchbot_logger = logging.getLogger("PotatoBot.Patchbot")


class PatchSourceError(Exception):
    """An upstream source was unreachable or answered in an unexpected shape.

    Raised rather than returning None so a schema change upstream is loud in
    the log instead of silently reading as "no patch this tick" forever.
    """


# --- HTTP helpers ---------------------------------------------------------

async def _fetch_json(session, url, **kwargs):
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=20), **kwargs
        ) as response:
            if response.status != 200:
                raise PatchSourceError(
                    f"HTTP {response.status} from {url.split('?', 1)[0]}"
                )
            return await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise PatchSourceError(
            f"{type(exc).__name__} from {url.split('?', 1)[0]}"
        ) from exc


async def _fetch_text(session, url, **kwargs):
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=20), **kwargs
        ) as response:
            if response.status != 200:
                raise PatchSourceError(
                    f"HTTP {response.status} from {url.split('?', 1)[0]}"
                )
            return await response.text()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise PatchSourceError(
            f"{type(exc).__name__} from {url.split('?', 1)[0]}"
        ) from exc


# --- Adapters --------------------------------------------------------------
# Each adapter takes the cog's aiohttp session and returns
# (version, url, title), or raises PatchSourceError. `version` only needs to
# be stable and unique per release -- it is compared byte-for-byte against
# the stored game_patch_state row, never parsed as a real semantic version.

STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_OFFICIAL_FEED = "steam_community_announcements"


def make_steam_news_adapter(appid):
    """The newest official Steam News post for a Steam-native game.

    A game's Steam feed also carries marketing/event posts and, for at least
    one of these games, syndicated third-party posts -- confirmed by probing
    Wuthering Waves' feed, see docs/patchbot_data_sources.md. Filtering to
    the official feed name is required, not a defensive extra.
    """
    async def adapter(session):
        data = await _fetch_json(
            session, STEAM_NEWS_URL,
            params={"appid": appid, "count": 15, "maxlength": 200, "format": "json"},
        )
        try:
            items = data["appnews"]["newsitems"]
        except (KeyError, TypeError) as exc:
            raise PatchSourceError(
                f"Unexpected Steam News response shape for appid {appid}"
            ) from exc
        for item in items:
            if item.get("feedname") == STEAM_OFFICIAL_FEED:
                return str(item["gid"]), item["url"], item["title"]
        raise PatchSourceError(f"No official Steam news item found for appid {appid}")
    return adapter


_NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_RIOT_ARTICLE_GRID_BLADE = "articleCardGrid"


def make_riot_news_adapter(base_url, tag_path):
    """The newest article on a Riot news-tag page (LoL, Valorant share this CMS).

    Riot has no public patch-notes API. Both sites serve their patch-notes
    tag listing server-rendered with a Next.js data blob, confirmed live --
    see docs/patchbot_data_sources.md. A version-number API exists for both
    games (Data Dragon, valorant-api.com) but its numbering does not match
    the site's own patch-slug numbering, so the newest article is read
    directly off this listing rather than constructed from a version.
    """
    async def adapter(session):
        html = await _fetch_text(session, base_url + tag_path)
        match = _NEXT_DATA_RE.search(html)
        if not match:
            raise PatchSourceError(
                f"No __NEXT_DATA__ block found at {base_url}{tag_path}"
            )
        try:
            data = json.loads(match.group(1))
            blades = data["props"]["pageProps"]["page"]["blades"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PatchSourceError(
                f"Unexpected Riot news page shape at {base_url}{tag_path}"
            ) from exc
        for blade in blades:
            if blade.get("type") == _RIOT_ARTICLE_GRID_BLADE:
                items = blade.get("items") or []
                if not items:
                    raise PatchSourceError(f"Empty article grid at {base_url}{tag_path}")
                newest = items[0]
                title = newest.get("title")
                url = (newest.get("action") or {}).get("payload", {}).get("url")
                if not title or not url:
                    raise PatchSourceError(
                        f"Article card missing title/url at {base_url}{tag_path}"
                    )
                return title, base_url + url, title
        raise PatchSourceError(f"No article grid blade found at {base_url}{tag_path}")
    return adapter


HOYOLAB_NEWS_URL = "https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList"
_HOYOLAB_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://www.hoyolab.com",
    "Referer": "https://www.hoyolab.com/",
}
_VERSION_UPDATE_SUFFIXES = ("update details", "what's new", "update notice")


def make_hoyolab_adapter(game_id):
    """The newest HoYoLAB notice whose title reads like a version-update post.

    Neither Genshin Impact nor Honkai: Star Rail is on Steam and neither has
    an official public API; this is the real backend HoYoLAB's own site and
    app use, undocumented but first-party -- the same trust level this
    project already extends to the community APIs in
    scripts/everydle_sources.py. The title heuristic is not perfectly
    consistent across every historical post (older phrasing differs), so a
    non-matching item is skipped rather than treated as the answer.
    """
    async def adapter(session):
        data = await _fetch_json(
            session, HOYOLAB_NEWS_URL,
            params={"gids": game_id, "page_size": 10, "type": 1},
            headers=_HOYOLAB_HEADERS,
        )
        try:
            posts = [entry["post"] for entry in data["data"]["list"]]
        except (KeyError, TypeError) as exc:
            raise PatchSourceError(
                f"Unexpected HoYoLAB response shape for gids={game_id}"
            ) from exc
        for post in posts:
            subject = post.get("subject") or ""
            lowered = subject.lower()
            if "version" in lowered and any(
                suffix in lowered for suffix in _VERSION_UPDATE_SUFFIXES
            ):
                post_id = post["post_id"]
                return post_id, f"https://www.hoyolab.com/article/{post_id}", subject
        raise PatchSourceError(f"No version-update post found for gids={game_id}")
    return adapter


MOJANG_VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
MINECRAFT_UPDATES_HUB_URL = "https://www.minecraft.net/en-us/updates"
MINECRAFT_ARTICLE_BASE_URL = "https://www.minecraft.net/en-us/article/"


async def _minecraft_article_exists(session, url):
    """Confirm a guessed article slug actually resolves before trusting it.

    The slug pattern below was found by reading `www.minecraft.net`'s own
    sitemap (see docs/patchbot_data_sources.md) rather than from anything
    Mojang documents, so it is a pattern match, not a stated contract. A
    wrong guess must degrade to the hub link, never post a 404 to a guild.
    """
    try:
        async with session.head(
            url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True
        ) as response:
            return response.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


async def minecraft_adapter(session):
    """Mojang's own version manifest as a "did a release ship" trigger.

    Every full release's announcement article lives at
    `minecraft-java-edition-<id>`, id with every `.` replaced by `-`
    (`26.3` -> `minecraft-java-edition-26-3`, `26.1.1` ->
    `minecraft-java-edition-26-1-1`) -- confirmed live against five releases
    spanning both the current date-based scheme and the legacy `1.21.x` one;
    see docs/patchbot_data_sources.md for how this was found (the site's own
    sitemap, not the article grid, which still loads client-side and 404s
    the previously-known JSON endpoint). Never assumed blindly: the guess is
    verified with a HEAD request each tick and falls back to the general
    updates hub if it does not resolve, since the pattern is reverse-engineered
    rather than documented by Mojang.
    """
    data = await _fetch_json(session, MOJANG_VERSION_MANIFEST_URL)
    try:
        latest_id = data["latest"]["release"]
    except (KeyError, TypeError) as exc:
        raise PatchSourceError("Unexpected Mojang version manifest shape") from exc
    article_url = MINECRAFT_ARTICLE_BASE_URL + (
        "minecraft-java-edition-" + latest_id.replace(".", "-")
    )
    url = (article_url if await _minecraft_article_exists(session, article_url)
           else MINECRAFT_UPDATES_HUB_URL)
    return latest_id, url, f"Minecraft {latest_id}"


# --- Supported games ---------------------------------------------------
# One adapter and one FeatureDefinition/setting family per game key. Keys
# here must match the `patchbot_<key>` feature and `patchbot_<key>_channel`
# / `patchbot_<key>_role` settings in core/settings_registry.py exactly.

SUPPORTED_GAMES = {
    "lol": make_riot_news_adapter(
        "https://www.leagueoflegends.com", "/en-us/news/tags/patch-notes/"
    ),
    "valorant": make_riot_news_adapter(
        "https://playvalorant.com", "/en-us/news/tags/patch-notes/"
    ),
    "minecraft": minecraft_adapter,
    "phasmophobia": make_steam_news_adapter(739630),
    "dbd": make_steam_news_adapter(381210),
    "genshin": make_hoyolab_adapter(2),
    "cs2": make_steam_news_adapter(730),
    "r6": make_steam_news_adapter(359550),
    "hsr": make_hoyolab_adapter(6),
    "wuwa": make_steam_news_adapter(3513350),
    "zzz": make_steam_news_adapter(4162040),
}

GAME_FEATURE_KEYS = {game_key: f"patchbot_{game_key}" for game_key in SUPPORTED_GAMES}


class Patchbot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None

    @commands.Cog.listener()
    async def on_ready(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

        patchbot_logger.info("Patchbot cog is ready.")

        if not self.patch_check_loop.is_running():
            self.patch_check_loop.start()
            patchbot_logger.info("Patch notification loop started.")

    def cog_unload(self):
        if self.patch_check_loop.is_running():
            self.patch_check_loop.cancel()
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())

    @tasks.loop(minutes=45)
    async def patch_check_loop(self):
        changed_games = {}
        for game_key, adapter in SUPPORTED_GAMES.items():
            try:
                version, url, title = await adapter(self.session)
            except PatchSourceError as exc:
                # One broken source must not stop the other ten from being
                # checked this tick, mirroring the per-item isolation
                # cogs/socials.py already uses for a bad YouTube feed.
                patchbot_logger.warning(
                    "Patch source check failed (game=%s, error=%s)", game_key, exc,
                )
                continue
            except Exception:
                patchbot_logger.exception(
                    "Unexpected error checking patch source (game=%s)", game_key,
                )
                continue

            previous = await database.run_read(database.get_game_patch_state, game_key)
            await database.run_write(
                database.set_game_patch_state, game_key, version, url, title,
            )
            if previous is not None and previous["latest_version"] != version:
                changed_games[game_key] = (version, url, title)
            # previous is None: first sight of this game's state, ever (a
            # fresh install or right after this migration). Baseline it
            # silently rather than announcing whatever happened to be
            # "latest" the moment the feature shipped, exactly as
            # cogs/socials.py's first-seen video/stream is never announced.

        if not changed_games:
            return

        for guild in self.bot.guilds:
            for game_key, (version, url, title) in changed_games.items():
                await self._announce_to_guild(guild, game_key, version, url, title)

    async def _announce_to_guild(self, guild, game_key, version, url, title):
        # A guild that enables a game's feature between two patches gets no
        # backlog: this method only ever runs for a game already found
        # "changed" this tick, so there is nothing to baseline per guild --
        # unlike socials.py, no separate first-sight branch is needed here.
        if not is_enabled(guild.id, GAME_FEATURE_KEYS[game_key]):
            return
        channel_id = guild_setting_sync(guild.id, f"patchbot_{game_key}_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not channel:
            return

        already_announced = await database.run_read(
            database.get_guild_patch_announcement, guild.id, game_key,
        )
        if already_announced == version:
            return

        role_id = guild_setting_sync(guild.id, f"patchbot_{game_key}_role")
        embed = discord.Embed(
            title=t("patchbot.new_patch_title", game=t(f"dashboard.game_names.{game_key}")),
            description=t("patchbot.new_patch_desc", title=title),
            url=url,
            color=0x2ECC71,
        )
        ping_msg = f"<@&{role_id}>" if role_id else ""
        try:
            await channel.send(
                content=ping_msg,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=True, users=False
                ),
            )
        except discord.Forbidden:
            patchbot_logger.warning(
                "Missing permission to post patch announcement "
                "(guild=%s, game=%s)", guild.id, game_key,
            )
            return
        # Recorded only after the send lands: a Forbidden here must not mark
        # this version "announced" and silently swallow it forever, exactly
        # the ordering cogs/socials.py already learned the hard way.
        await database.run_write(
            database.set_guild_patch_announcement, guild.id, game_key, version,
        )

    @patch_check_loop.before_loop
    async def before_patch_check(self):
        await self.bot.wait_until_ready()

    @patch_check_loop.error
    async def patch_check_loop_error(self, error):
        await handle_loop_error(
            self.bot, self.patch_check_loop, "patch_check_loop", error,
            patchbot_logger,
        )


async def setup(bot):
    await bot.add_cog(Patchbot(bot))
