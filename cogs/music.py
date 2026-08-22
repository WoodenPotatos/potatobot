import discord
import logging
import random
import asyncio
import time
import yt_dlp
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database

from discord.ext import commands
from datetime import timedelta
from cogs.utils import BoundedCooldownMap, t
from feature_access import require_interaction_feature

music_logger = logging.getLogger("PotatoBot.Music")

music_queues = {}
music_timers = {}
music_states = {}
last_panels = {}
music_interaction_times = BoundedCooldownMap()
music_search_times = BoundedCooldownMap()
music_search_active = set()
MUSIC_QUEUE_LIMIT = 100
MUSIC_PLAYLIST_LIMIT = 25
MUSIC_MAX_DURATION = 3 * 60 * 60
MUSIC_EXTRACT_TIMEOUT = 20
music_extract_executor = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="music-extract"
)
music_extract_slots = asyncio.Semaphore(2)


def can_control_music(member, guild):
    if guild is None:
        return False
    if member.guild_permissions.administrator:
        return True
    voice_client = guild.voice_client
    member_voice = member.voice
    return (
        voice_client is not None
        and member_voice is not None
        and member_voice.channel is not None
        and member_voice.channel.id == voice_client.channel.id
    )

# Prefer Opus-compatible audio and reconnect FFmpeg streams after brief network loss.
ytdl_format_options = {
    'format': 'bestaudio[ext=webm][acodec=opus]/bestaudio/best', 
    'noplaylist': False,
    'playlistend': MUSIC_PLAYLIST_LIMIT,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch1',
    'socket_timeout': 10,
    'retries': 2,
    'fragment_retries': 2,
    'ignoreconfig': True,
}

ffmpeg_options = {
    'options': '-vn -sn -dn',
    "before_options": (
        # Network audio never needs the local file protocol.
        "-nostdin -loglevel warning -protocol_whitelist "
        "http,https,tcp,tls,crypto -reconnect 1 "
        "-reconnect_streamed 1 -reconnect_delay_max 5"
    ),
}


def _youtube_input(value: str) -> str:
    """Return a YouTube URL or an explicit YouTube search expression."""
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("unsupported music source")
        try:
            if parsed.port is not None:
                raise ValueError("unsupported music source")
        except ValueError as exc:
            raise ValueError("unsupported music source") from exc
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if hostname != "youtu.be" and not (
            hostname == "youtube.com" or hostname.endswith(".youtube.com")
        ):
            raise ValueError("unsupported music source")
        return value
    return f"ytsearch1:{value}"


def _extract_music_info(source: str):
    # yt-dlp instances are intentionally not shared across worker threads.
    with yt_dlp.YoutubeDL(dict(ytdl_format_options)) as extractor:
        return extractor.extract_info(source, download=False)


async def extract_music_info(source: str):
    loop = asyncio.get_running_loop()
    async with music_extract_slots:
        return await asyncio.wait_for(
            loop.run_in_executor(
                music_extract_executor, _extract_music_info, source
            ),
            timeout=MUSIC_EXTRACT_TIMEOUT,
        )


def song_from_entry(entry, requester):
    if not entry or entry.get("is_live") or entry.get("live_status") == "is_live":
        return None
    duration = int(entry.get("duration") or 0)
    if duration > MUSIC_MAX_DURATION:
        return None
    stream_url = entry.get("url")
    if not stream_url:
        return None
    return {
        "url": stream_url,
        "title": entry.get("title") or t("music.unknown_song"),
        "webpage_url": entry.get("webpage_url") or entry.get("original_url"),
        "thumbnail": entry.get("thumbnail"),
        "duration": duration,
        "duration_text": f"{duration // 60}:{duration % 60:02d}" if duration else "?",
        "duration_string": str(timedelta(seconds=duration)) if duration else "?",
        "requester": requester,
    }

# Queue-index removal submitted from the persistent music panel.
class MusicRemoveModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title=t("music.remove_modal_title"))
        self.number = discord.ui.TextInput(
            label=t("music.remove_number_label"),
            placeholder=t("music.remove_number_placeholder"),
            min_length=1,
            max_length=3
        )
        self.add_item(self.number)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "music"):
            return
        if not can_control_music(interaction.user, interaction.guild):
            return await interaction.response.send_message(
                t("music.not_in_vc"), ephemeral=True
            )
        guild_id = interaction.guild.id
        if guild_id not in music_queues or len(music_queues[guild_id]) == 0:
            return await interaction.response.send_message(t("music.queue_empty"), ephemeral=True)
        
        try:
            num = int(self.number.value)
            if num < 1 or num > len(music_queues[guild_id]):
                return await interaction.response.send_message(t("music.invalid_queue_number"), ephemeral=True)

            removed_song = music_queues[guild_id].pop(num - 1)
            await interaction.response.send_message(t("music.song_removed", title=removed_song['title']))
        except ValueError:
            await interaction.response.send_message(t("music.invalid_number_format"), ephemeral=True)

# Search requests run off the event loop because yt-dlp is synchronous.
class MusicSearchModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title=t("music.search_modal_title"))
        self.query = discord.ui.TextInput(
            label=t("music.search_query_label"),
            placeholder=t("music.search_query_placeholder"),
            min_length=1
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "music"):
            return
        if not can_control_music(interaction.user, interaction.guild):
            return await interaction.response.send_message(
                t("music.not_in_vc"), ephemeral=True
            )
        if interaction.user.id in music_search_active:
            return await interaction.response.send_message(
                t("utils.command_in_progress"), ephemeral=True
            )
        music_search_active.add(interaction.user.id)
        await interaction.response.defer(ephemeral=True)

        try:
            search_query = _youtube_input(self.query.value)
            guild = interaction.guild
            vc = guild.voice_client
            data = await extract_music_info(search_query)
            if not data or not data.get('entries'):
                return await interaction.followup.send(t("music.search_no_results"), ephemeral=True)
            
            video = data['entries'][0]
            song = song_from_entry(video, interaction.user.display_name)
            if song is None:
                return await interaction.followup.send(
                    t("music.unsupported_video"), ephemeral=True
                )
        except ValueError:
            return await interaction.followup.send(
                t("music.invalid_source"), ephemeral=True
            )
        except Exception as exc:
            music_logger.warning(
                "Music search failed (user_id=%s, error=%s)",
                interaction.user.id, type(exc).__name__,
            )
            return await interaction.followup.send(
                t("music.search_failed"), ephemeral=True
            )
        finally:
            music_search_active.discard(interaction.user.id)

        if guild.id not in music_queues:
            music_queues[guild.id] = []
        if len(music_queues[guild.id]) >= MUSIC_QUEUE_LIMIT:
            return await interaction.followup.send(
                t("music.queue_full"), ephemeral=True
            )
        
        music_queues[guild.id].append(song)

        if vc and (vc.is_playing() or vc.is_paused()):
            await interaction.followup.send(t("music.added_to_queue", title=song['title']), ephemeral=True)
        else:
            music_cog = interaction.client.get_cog("Music")
            if music_cog:
                music_cog.play_next(guild, interaction.channel)
            await interaction.followup.send(t("music.starting_song", title=song['title']), ephemeral=True)


# Persistent controls shared by music-panel messages.
class MusicPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        btn_pause = discord.ui.Button(label=t("music.btn_pause_resume"), style=discord.ButtonStyle.secondary, emoji="⏯️", row=0, custom_id="music_pause")
        btn_pause.callback = self.pause_resume; self.add_item(btn_pause)

        btn_skip = discord.ui.Button(label=t("music.btn_skip"), style=discord.ButtonStyle.primary, emoji="⏭️", row=0, custom_id="music_skip")
        btn_skip.callback = self.skip; self.add_item(btn_skip)

        btn_stop = discord.ui.Button(label=t("music.btn_stop"), style=discord.ButtonStyle.danger, emoji="🛑", row=0, custom_id="music_stop")
        btn_stop.callback = self.stop_leave; self.add_item(btn_stop)

        btn_search = discord.ui.Button(label=t("music.btn_search"), style=discord.ButtonStyle.success, emoji="🔍", row=0, custom_id="music_search")
        btn_search.callback = self.search_btn; self.add_item(btn_search)

        btn_np = discord.ui.Button(label=t("music.btn_np"), style=discord.ButtonStyle.secondary, emoji="ℹ️", row=0, custom_id="music_np")
        btn_np.callback = self.now_playing; self.add_item(btn_np)

        btn_queue = discord.ui.Button(label=t("music.btn_queue"), style=discord.ButtonStyle.secondary, emoji="📜", row=1, custom_id="music_queue")
        btn_queue.callback = self.show_queue; self.add_item(btn_queue)

        btn_shuffle = discord.ui.Button(label=t("music.btn_shuffle"), style=discord.ButtonStyle.secondary, emoji="🔀", row=1, custom_id="music_shuffle")
        btn_shuffle.callback = self.shuffle_btn; self.add_item(btn_shuffle)

        btn_loop = discord.ui.Button(label=t("music.btn_loop"), style=discord.ButtonStyle.secondary, emoji="🔁", row=1, custom_id="music_loop")
        btn_loop.callback = self.loop_btn; self.add_item(btn_loop)

        btn_remove = discord.ui.Button(label=t("music.btn_remove"), style=discord.ButtonStyle.secondary, emoji="🗑️", row=1, custom_id="music_remove")
        btn_remove.callback = self.remove_btn; self.add_item(btn_remove)

    async def interaction_check(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "music"):
            return False
        if not can_control_music(interaction.user, interaction.guild):
            await interaction.response.send_message(t("music.not_in_vc"), ephemeral=True)
            return False
        now = time.monotonic()
        retry_after = 3 - (now - music_interaction_times.get(interaction.user.id, 0))
        if retry_after > 0:
            await interaction.response.send_message(
                t("utils.command_cooldown", seconds=int(retry_after) + 1),
                ephemeral=True,
            )
            return False
        music_interaction_times[interaction.user.id] = now
        return True

    async def pause_resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc: 
            return await interaction.response.send_message(t("music.nothing_playing"), ephemeral=True)
    
        if vc.is_paused():
            vc.resume()
            await interaction.response.send_message(t("music.music_resumed"), ephemeral=True)
        elif vc.is_playing():
            vc.pause()
            await interaction.response.send_message(t("music.music_paused"), ephemeral=True)

    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing(): 
            return await interaction.response.send_message(t("music.nothing_to_skip"), ephemeral=True)
    
        vc.stop() 
        await interaction.response.send_message(t("music.skipped"), ephemeral=False)

    async def stop_leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc: 
            return await interaction.response.send_message(t("music.bot_not_in_vc"), ephemeral=True)

        owner_id = await database.run(database.get_active_channel_owner, vc.channel.id)
        is_admin = interaction.user.guild_permissions.administrator
        is_owner = owner_id and interaction.user.id == owner_id

        if owner_id and not is_owner and not is_admin:
            return await interaction.response.send_message(t("music.owner_only_kick"), ephemeral=True)

        music_queues[interaction.guild.id] = [] 
        await vc.disconnect()
    
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(t("music.stopped_by_owner"), delete_after=20)

    async def search_btn(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message(t("music.use_join_first"), ephemeral=True)
        now = time.monotonic()
        retry_after = 15 - (now - music_search_times.get(interaction.user.id, 0))
        if retry_after > 0:
            return await interaction.response.send_message(
                t("utils.command_cooldown", seconds=int(retry_after) + 1),
                ephemeral=True,
            )
        music_search_times[interaction.user.id] = now
        await interaction.response.send_modal(MusicSearchModal())

    async def now_playing(self, interaction: discord.Interaction):
        state = music_states.get(interaction.guild.id)
        vc = interaction.guild.voice_client

        if not vc or not vc.is_playing() or not state or not state['current']:
            return await interaction.response.send_message(t("music.currently_nothing_playing"), ephemeral=True)

        song = state['current']
        total_seconds = song.get('duration', 0)
    
        if total_seconds > 0:
            elapsed = int(time.time() - state['start_time'])
            elapsed = min(elapsed, total_seconds)
            progress = elapsed / total_seconds
            filled = int(20 * progress)
            bar = "▬" * filled + "🔘" + "▬" * (20 - filled - 1)
        
            m, s = divmod(elapsed, 60)
            time_text = f"`{m:02d}:{s:02d} {bar} {song.get('duration_string', '?')}`"
        else:
            time_text = f"`{t('music.live_stream')}`"

        embed = discord.Embed(title=t("music.np_title"), description=t("music.np_desc", title=song['title'], time_text=time_text), color=discord.Color.purple())
        embed.set_footer(text=t("music.np_footer", requester=song['requester'], loop=state['loop'].upper()))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show_queue(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        if guild_id not in music_queues or len(music_queues[guild_id]) == 0:
            return await interaction.response.send_message(t("music.queue_totally_empty"), ephemeral=True)

        q_list = music_queues[guild_id]
        desc = ""
        for i, song in enumerate(q_list[:10], start=1):
            desc += t("music.queue_item", index=i, title=song['title'], requester=song['requester'])
        
        if len(q_list) > 10:
            desc += t("music.queue_more_songs", count=len(q_list) - 10)

        embed = discord.Embed(title=t("music.queue_title"), description=desc, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def shuffle_btn(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        if guild_id not in music_queues or len(music_queues[guild_id]) < 2:
            return await interaction.response.send_message(t("music.not_enough_for_shuffle"), ephemeral=True)
        
        random.shuffle(music_queues[guild_id])
        await interaction.response.send_message(t("music.shuffled_success"))

    async def loop_btn(self, interaction: discord.Interaction):
        state = music_states.setdefault(interaction.guild.id, {'current': None, 'loop': 'off', 'start_time': 0})
    
        if state['loop'] == 'off':
            state['loop'] = 'song'
            msg = t("music.loop_song")
        elif state['loop'] == 'song':
            state['loop'] = 'queue'
            msg = t("music.loop_queue")
        else:
            state['loop'] = 'off'
            msg = t("music.loop_off")
        
        await interaction.response.send_message(msg)

    async def remove_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(MusicRemoveModal())

# Guild-scoped queue orchestration and slash/prefix command handlers.
class Music(commands.Cog): 
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(MusicPanelView()) 

    async def require_same_voice(self, ctx):
        if can_control_music(ctx.author, ctx.guild):
            return True
        await ctx.send(t("music.not_in_vc"), ephemeral=True)
        return False

    async def auto_leave_timer(self, guild, text_channel):
        await asyncio.sleep(180) 
        vc = guild.voice_client
        if vc and not vc.is_playing():
            await vc.disconnect()
            await text_channel.send(t("music.auto_leave_msg"))

    def play_next(self, guild, text_channel):
        if guild.id in music_queues and music_queues[guild.id]:
            song = music_queues[guild.id].pop(0)
            vc = guild.voice_client

            if not vc:
                return

            def handle_next(error):
                if error:
                    music_logger.error("Audio playback failed: %s", error)
                self.bot.loop.call_soon_threadsafe(self.play_next, guild, text_channel)

            vc.play(
                discord.FFmpegPCMAudio(song['url'], **ffmpeg_options),
                after=handle_next,
            )

            async def update_ui():
                if guild.id in last_panels:
                    try:
                        await last_panels[guild.id].delete()
                    except:
                        pass

                embed = discord.Embed(
                    title=t("music.now_playing_title"),
                    description=t("music.now_playing_desc", title=song['title'], url=song.get('webpage_url', song['url'])),
                    color=discord.Color.blurple()
                )
                embed.set_thumbnail(url=song.get('thumbnail'))
                embed.add_field(name=t("music.duration_label"), value=song.get('duration_text', t("music.unknown_duration")), inline=True)
                embed.set_footer(text=t("music.requested_by", requester=song['requester']))

                new_msg = await text_channel.send(embed=embed, view=MusicPanelView())
                last_panels[guild.id] = new_msg

            self.bot.loop.create_task(update_ui())
        else:
            if guild.id in last_panels:
                async def cleanup_panel():
                    try:
                        await last_panels[guild.id].edit(content=t("music.queue_empty_panel"), embed=None, view=None)
                    except discord.NotFound:
                        pass 
            
                self.bot.loop.create_task(cleanup_panel())
            
            music_timers[guild.id] = self.bot.loop.create_task(self.auto_leave_timer(guild, text_channel))

    @commands.hybrid_command(name="skip", description=t("general.cmd_skip"))
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def skip_cmd(self, ctx):
        if not await self.require_same_voice(ctx):
            return
        vc = ctx.voice_client
        if not vc or not vc.is_playing(): 
            return await ctx.send(t("music.nothing_to_skip"), ephemeral=True)
        vc.stop() 
        await ctx.send(t("music.skipped"))

    @commands.hybrid_command(name="queue", description=t("general.cmd_queue"))
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def queue_cmd(self, ctx):
        if not await self.require_same_voice(ctx):
            return
        guild_id = ctx.guild.id
        if guild_id not in music_queues or len(music_queues[guild_id]) == 0:
            return await ctx.send(t("music.queue_totally_empty"))

        q_list = music_queues[guild_id]
        desc = ""
        for i, song in enumerate(q_list[:10], start=1):
            desc += t("music.queue_item", index=i, title=song['title'], requester=song['requester'])
        
        if len(q_list) > 10:
            desc += t("music.queue_more_songs", count=len(q_list) - 10)

        embed = discord.Embed(title=t("music.queue_title"), description=desc, color=discord.Color.blue())
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="shuffle", description=t("general.cmd_shuffle"))
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def shuffle_cmd(self, ctx):
        if not await self.require_same_voice(ctx):
            return
        guild_id = ctx.guild.id
        if guild_id not in music_queues or len(music_queues[guild_id]) < 2:
            return await ctx.send(t("music.not_enough_for_shuffle"), ephemeral=True)
        
        random.shuffle(music_queues[guild_id])
        await ctx.send(t("music.shuffled_success"))

    @commands.hybrid_command(name="remove", description=t("general.cmd_remove"))
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def remove_cmd(self, ctx, number: int):
        if not await self.require_same_voice(ctx):
            return
        guild_id = ctx.guild.id
        if guild_id not in music_queues or len(music_queues[guild_id]) == 0:
            return await ctx.send(t("music.queue_empty"), ephemeral=True)
        
        if number < 1 or number > len(music_queues[guild_id]):
            return await ctx.send(t("music.invalid_queue_number_cmd"), ephemeral=True)

        removed_song = music_queues[guild_id].pop(number - 1)
        await ctx.send(t("music.song_removed", title=removed_song['title']))

    @commands.hybrid_command(name="loop", description=t("general.cmd_loop"))
    @commands.cooldown(1, 3, commands.BucketType.user)
    @discord.app_commands.choices(mode=[
        discord.app_commands.Choice(name=t("music.loop_choice_off"), value="off"),
        discord.app_commands.Choice(name=t("music.loop_choice_song"), value="song"),
        discord.app_commands.Choice(name=t("music.loop_choice_queue"), value="queue")
    ])
    async def loop_cmd(self, ctx, mode: str):
        if not await self.require_same_voice(ctx):
            return
        state = music_states.setdefault(ctx.guild.id, {'current': None, 'loop': 'off', 'start_time': 0})
        state['loop'] = mode
    
        if mode == "off":
            await ctx.send(t("music.loop_off"))
        elif mode == "song":
            await ctx.send(t("music.loop_song"))
        elif mode == "queue":
            await ctx.send(t("music.loop_queue"))

    @commands.hybrid_command(name="np", description=t("general.cmd_np"))
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def np_cmd(self, ctx):
        if not await self.require_same_voice(ctx):
            return
        state = music_states.get(ctx.guild.id)
        vc = ctx.voice_client

        if not vc or not vc.is_playing() or not state or not state['current']:
            return await ctx.send(t("music.currently_nothing_playing"))

        song = state['current']
        duration_str = song.get('duration_string', t("music.unknown_duration"))
        total_seconds = song.get('duration', 0)

        if total_seconds > 0:
            elapsed = int(time.time() - state['start_time'])
            elapsed = min(elapsed, total_seconds)
            progress = elapsed / total_seconds
            bar_length = 20
            filled_length = int(bar_length * progress)
            bar = "▬" * filled_length + "🔘" + "▬" * (bar_length - filled_length - 1)
            m, s = divmod(elapsed, 60)
            time_text = f"`{m:02d}:{s:02d} {bar} {duration_str}`"
        else:
            time_text = f"`{t('music.live_stream')}`"

        embed = discord.Embed(title=t("music.np_title"), description=t("music.np_desc", title=song['title'], time_text=time_text), color=discord.Color.purple())
        embed.set_footer(text=t("music.np_footer", requester=song['requester'], loop=state['loop'].upper()))
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="join", description=t("general.cmd_join"))
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def join_cmd(self, ctx):
        if not ctx.author.voice:
            return await ctx.send(t("music.join_vc_first"), ephemeral=True)
        
        vc = ctx.voice_client
        if vc and vc.channel.id != ctx.author.voice.channel.id:
            return await ctx.send(t("music.bot_in_other_vc"), ephemeral=True)

        if not vc:
            try:
                vc = await ctx.author.voice.channel.connect()
            except Exception as exc:
                music_logger.warning(
                    "Voice connection failed (guild_id=%s, error=%s)",
                    ctx.guild.id, type(exc).__name__,
                )
                return await ctx.send(t("music.join_failed_safe"), ephemeral=True)
            
        vc_channel = ctx.author.voice.channel 
    
        embed = discord.Embed(
            title=t("music.panel_title"), 
            description=t("music.panel_desc"), 
            color=discord.Color.green()
        )
    
        try:
            await vc_channel.send(embed=embed, view=MusicPanelView())
            await ctx.send(t("music.panel_sent"), ephemeral=True)
        except Exception as exc:
            music_logger.warning(
                "Music panel delivery failed (guild_id=%s, error=%s)",
                ctx.guild.id, type(exc).__name__,
            )
            await ctx.send(t("music.panel_send_failed"), ephemeral=True)

    @commands.hybrid_command(name="stop", description=t("general.cmd_stop"))
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def stop(self, ctx):
        if not await self.require_same_voice(ctx):
            return
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send(t("music.stopped_and_left"))
        else:
            await ctx.send(t("music.bot_not_in_vc"), ephemeral=True)

    @commands.hybrid_command(name="play", description=t("general.cmd_play"))
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.user, wait=False)
    async def play(self, ctx, *, search: str):
        if not ctx.author.voice:
            return await ctx.send(t("music.join_vc_first"), ephemeral=True)

        vc = ctx.voice_client
        if vc and vc.channel.id != ctx.author.voice.channel.id:
            return await ctx.send(t("music.bot_in_other_vc_play"), ephemeral=True)

        if not vc:
            try:
                vc = await ctx.author.voice.channel.connect()
            except Exception as exc:
                music_logger.warning(
                    "Voice connection failed (guild_id=%s, error=%s)",
                    ctx.guild.id, type(exc).__name__,
                )
                return await ctx.send(t("music.join_failed_safe"), ephemeral=True)
            
        if ctx.guild.id in music_timers and not music_timers[ctx.guild.id].done():
            music_timers[ctx.guild.id].cancel()

        try:
            data = await extract_music_info(_youtube_input(search))
        
            if 'entries' in data:
                entries = list(data['entries'])[:MUSIC_PLAYLIST_LIMIT]
                queue = music_queues.setdefault(ctx.guild.id, [])
                capacity = max(0, MUSIC_QUEUE_LIMIT - len(queue))
                songs = [
                    song_from_entry(entry, ctx.author.display_name)
                    for entry in entries
                ]
                songs = [song for song in songs if song is not None][:capacity]
                if not songs:
                    message = "queue_full" if capacity == 0 else "unsupported_video"
                    return await ctx.send(t(f"music.{message}"), ephemeral=True)
                queue.extend(songs)
                added_count = len(songs)
            
                if not vc.is_playing() and not vc.is_paused():
                    self.play_next(ctx.guild, ctx.channel) 
                    await ctx.send(t("music.playlist_added_play", count=added_count))
                else:
                    await ctx.send(t("music.playlist_added_queue", count=added_count))
                return 

            else:
                song = song_from_entry(data, ctx.author.display_name)
                if song is None:
                    return await ctx.send(t("music.unsupported_video"), ephemeral=True)
            
        except ValueError:
            return await ctx.send(t("music.invalid_source"), ephemeral=True)
        except Exception as exc:
            music_logger.warning(
                "Music extraction failed (guild_id=%s, user_id=%s, error=%s)",
                ctx.guild.id, ctx.author.id, type(exc).__name__,
            )
            return await ctx.send(t("music.search_failed"), ephemeral=True)

        if ctx.guild.id not in music_queues:
            music_queues[ctx.guild.id] = []
        if len(music_queues[ctx.guild.id]) >= MUSIC_QUEUE_LIMIT:
            return await ctx.send(t("music.queue_full"), ephemeral=True)

        if vc.is_playing() or vc.is_paused():
            music_queues[ctx.guild.id].append(song)
        
            embed = discord.Embed(title=t("music.queue_added_title"), description=t("music.queue_added_desc", title=song['title']), color=discord.Color.blue())
            embed.set_footer(text=t("music.queue_added_footer", pos=len(music_queues[ctx.guild.id])))
            await ctx.send(embed=embed)
        else:
            music_queues[ctx.guild.id].append(song)
            self.play_next(ctx.guild, ctx.channel)
            await ctx.send(t("music.search_success", title=song['title']), delete_after=5)

async def setup(bot):
    await bot.add_cog(Music(bot))
