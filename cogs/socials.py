import discord
import aiohttp
import logging
import os
import sys
import asyncio
from defusedxml import ElementTree as ET

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from discord.ext import commands, tasks
from cogs.utils import t, config
from feature_access import is_enabled

social_logger = logging.getLogger("PotatoBot.Socials")

class Socials(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        
        self.twitch_client_id = os.getenv("TWITCH_CLIENT_ID")
        self.twitch_client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        configured_streamers = config.get("socials", {}).get("twitch_streamers", [])
        if configured_streamers and not (
            self.twitch_client_id and self.twitch_client_secret
        ):
            social_logger.error(
                "Twitch notifications are disabled: TWITCH_CLIENT_ID and "
                "TWITCH_CLIENT_SECRET must both be configured."
            )
        
        # In-memory notification state prevents duplicate announcements between polls.
        self.live_twitch = set()
        self.latest_videos = {} 

    @commands.Cog.listener()
    async def on_ready(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
            
        social_logger.info("Social notification cog is ready.")
        
        if not self.twitch_check_loop.is_running():
            self.twitch_check_loop.start()
            social_logger.info("Twitch notification loop started.")
            
        if not self.youtube_rss_loop.is_running():
            self.youtube_rss_loop.start()
            social_logger.info("YouTube RSS notification loop started.")

    def cog_unload(self):
        if self.twitch_check_loop.is_running():
            self.twitch_check_loop.cancel()
        if self.youtube_rss_loop.is_running():
            self.youtube_rss_loop.cancel()
            
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())

    async def request(self, method, url, *, response_type="json", **kwargs):
        """Performs one bounded retry without leaking request credentials."""
        for attempt in range(2):
            try:
                async with self.session.request(method, url, **kwargs) as response:
                    if response.status >= 500 and attempt == 0:
                        await asyncio.sleep(1)
                        continue
                    payload = (
                        await response.json()
                        if response_type == "json"
                        else await response.text()
                    )
                    return response.status, payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                social_logger.warning(
                    "Social request failed after retry (endpoint=%s, error=%s)",
                    url.split("?", 1)[0],
                    type(exc).__name__,
                )
        return None, None

    async def get_twitch_token(self):
        url = "https://id.twitch.tv/oauth2/token"
        form_data = {
            "client_id": self.twitch_client_id,
            "client_secret": self.twitch_client_secret,
            "grant_type": "client_credentials",
        }
        status, data = await self.request("POST", url, data=form_data)
        if status == 200:
            return data.get("access_token")
        return None

    # Twitch live-status polling.
    @tasks.loop(minutes=5)
    async def twitch_check_loop(self):
        social_cfg = config.get("socials", {})
        channel = self.bot.get_channel(social_cfg.get("notification_channel"))
        twitch_streamers = social_cfg.get("twitch_streamers", [])
        if not channel or not is_enabled(channel.guild.id, "social_twitch") or not self.twitch_client_id or not self.twitch_client_secret or not twitch_streamers:
            return

        token = await self.get_twitch_token()
        if token:
            headers = {"Client-ID": self.twitch_client_id, "Authorization": f"Bearer {token}"}
            url = "https://api.twitch.tv/helix/streams"
            params = [("user_login", streamer) for streamer in twitch_streamers]
            status, data = await self.request(
                "GET", url, headers=headers, params=params
            )
            if status == 200:
                streams = data.get("data", [])
                currently_live_logins = [stream['user_name'].lower() for stream in streams]

                for stream_info in streams:
                    streamer_login = stream_info['user_name'].lower()
                    if streamer_login not in self.live_twitch:
                        self.live_twitch.add(streamer_login)
                        embed = discord.Embed(
                            title=t("socials.twitch_live_title", streamer=stream_info['user_name']),
                            description=t("socials.twitch_live_desc", title=stream_info['title'], game=stream_info['game_name']),
                            url=f"https://twitch.tv/{streamer_login}",
                            color=0x9146FF
                        )
                        thumb_url = stream_info['thumbnail_url'].replace("{width}", "1280").replace("{height}", "720")
                        embed.set_image(url=thumb_url)

                        role_id = social_cfg.get("twitch_role_id")
                        ping_msg = f"<@&{role_id}>" if role_id else ""
                        await channel.send(
                            content=ping_msg,
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions(
                                everyone=False, roles=True, users=False
                            ),
                        )

                for saved_streamer in list(self.live_twitch):
                    if saved_streamer not in currently_live_logins:
                        self.live_twitch.remove(saved_streamer)

    @twitch_check_loop.before_loop
    async def before_twitch_check(self):
        await self.bot.wait_until_ready()

    # YouTube RSS polling.
    @tasks.loop(minutes=5)
    async def youtube_rss_loop(self):
        social_cfg = config.get("socials", {})
        channel = self.bot.get_channel(social_cfg.get("notification_channel"))
        yt_channels = social_cfg.get("youtube_channels", [])
        if not channel or not is_enabled(channel.guild.id, "social_youtube") or not yt_channels:
            return

        for yt_id in yt_channels:
            rss_url = "https://www.youtube.com/feeds/videos.xml"
            status, text = await self.request(
                "GET", rss_url, response_type="text",
                params={"channel_id": str(yt_id)},
            )
            if status == 200:
                try:
                    root = ET.fromstring(text)
                    ns = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}

                    channel_name_elem = root.find('atom:title', ns)
                    channel_name = channel_name_elem.text if channel_name_elem is not None else t("socials.default_channel_name")

                    entry = root.find('atom:entry', ns)
                    if entry is not None:
                        video_id = entry.find('yt:videoId', ns).text
                        title = entry.find('atom:title', ns).text
                        link = entry.find('atom:link', ns).attrib['href']

                        if yt_id not in self.latest_videos:
                            self.latest_videos[yt_id] = video_id
                        elif self.latest_videos[yt_id] != video_id:
                            self.latest_videos[yt_id] = video_id
                            embed = discord.Embed(
                                title=t("socials.youtube_new_video_title", channel=channel_name),
                                description=t("socials.youtube_new_video_desc", title=title),
                                url=link,
                                color=0xFF0000
                            )
                            embed.set_image(url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg")

                            role_id = social_cfg.get("youtube_role_id")
                            ping_msg = f"<@&{role_id}>" if role_id else ""
                            await channel.send(
                                content=ping_msg,
                                embed=embed,
                                allowed_mentions=discord.AllowedMentions(
                                    everyone=False, roles=True, users=False
                                ),
                            )
                except Exception:
                    social_logger.exception(
                        "YouTube RSS processing failed for channel %s.", yt_id
                    )

    @youtube_rss_loop.before_loop
    async def before_youtube_rss_check(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(Socials(bot))
