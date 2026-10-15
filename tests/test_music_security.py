import shlex
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cogs import music


class ExtractionPoolReapTests(unittest.TestCase):
    """A wedged extraction thread cannot be stopped, only abandoned.

    `wait_for` timing out does not kill the underlying `yt_dlp` thread, and its
    own `socket_timeout` does not cover a hanging DNS resolution — so the pool
    has to notice a job that has run far past any real extraction's length and
    replace itself, rather than leaving every worker eventually stuck.
    """

    def setUp(self):
        self.original_executor = music.music_extract_executor
        self.original_jobs = dict(music.music_extract_job_started)
        music.music_extract_job_started.clear()

    def tearDown(self):
        music.music_extract_executor.shutdown(wait=False, cancel_futures=True)
        music.music_extract_executor = self.original_executor
        music.music_extract_job_started.clear()
        music.music_extract_job_started.update(self.original_jobs)

    def test_a_recent_job_is_left_alone(self):
        music.music_extract_job_started[1] = time.monotonic()
        self.assertEqual(music.reap_wedged_extraction_pool(), 0)
        self.assertIs(music.music_extract_executor, self.original_executor)
        self.assertIn(1, music.music_extract_job_started)

    def test_no_in_flight_jobs_is_a_no_op(self):
        self.assertEqual(music.reap_wedged_extraction_pool(), 0)
        self.assertIs(music.music_extract_executor, self.original_executor)

    def test_a_job_past_the_wedge_ceiling_gets_the_pool_replaced(self):
        music.music_extract_job_started[1] = (
            time.monotonic() - music.MUSIC_EXTRACT_WEDGE_SECONDS - 1
        )
        music.music_extract_job_started[2] = time.monotonic()

        stuck = music.reap_wedged_extraction_pool()

        self.assertEqual(stuck, 2)
        self.assertIsNot(music.music_extract_executor, self.original_executor)
        self.assertEqual(music.music_extract_job_started, {})
        # The new pool is a working one, not a stand-in.
        self.assertIsInstance(music.music_extract_executor, ThreadPoolExecutor)
        self.assertEqual(
            music.music_extract_executor.submit(lambda: 1 + 1).result(timeout=5), 2
        )


class ExtractionJobTrackingTests(unittest.IsolatedAsyncioTestCase):
    """`extract_music_info` must not leak a tracked job on the ordinary path."""

    async def test_a_successful_call_leaves_no_tracked_job_behind(self):
        original = music._extract_music_info
        music._extract_music_info = lambda source: {"title": source}
        try:
            result = await music.extract_music_info("https://youtu.be/test")
        finally:
            music._extract_music_info = original
        self.assertEqual(result, {"title": "https://youtu.be/test"})
        self.assertEqual(music.music_extract_job_started, {})

    async def test_a_timed_out_call_still_stops_tracking_the_job(self):
        """`wait_for` giving up must not leave the job "in flight" forever --
        that bookkeeping leak is exactly what would make every future call
        look wedged to `reap_wedged_extraction_pool`."""
        original_timeout = music.MUSIC_EXTRACT_TIMEOUT
        original_extract = music._extract_music_info
        music.MUSIC_EXTRACT_TIMEOUT = 0.01
        music._extract_music_info = lambda source: time.sleep(0.2) or {}
        try:
            with self.assertRaises(TimeoutError):
                await music.extract_music_info("https://youtu.be/test")
        finally:
            music.MUSIC_EXTRACT_TIMEOUT = original_timeout
            music._extract_music_info = original_extract
        self.assertEqual(music.music_extract_job_started, {})


class MusicInputSecurityTests(unittest.TestCase):
    def test_only_https_youtube_urls_are_accepted(self):
        accepted = [
            "https://youtube.com/watch?v=abc",
            "https://www.youtube.com/watch?v=abc",
            "https://music.youtube.com/watch?v=abc",
            "https://youtu.be/abc",
        ]
        for source in accepted:
            with self.subTest(source=source):
                self.assertEqual(music._youtube_input(source), source)

    def test_non_youtube_or_credentialed_urls_are_rejected(self):
        rejected = [
            "http://youtube.com/watch?v=abc",
            "https://example.com/audio",
            "https://user:pass@youtube.com/watch?v=abc",
            "https://youtube.com:443/watch?v=abc",
            "ftp://youtube.com/file",
        ]
        for source in rejected:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    music._youtube_input(source)

    def test_plain_text_is_forced_through_youtube_search(self):
        self.assertEqual(
            music._youtube_input("artist: song"), "ytsearch1:artist: song"
        )

    def test_live_and_overlong_entries_are_rejected(self):
        base = {"url": "https://media.example/audio", "title": "Test"}
        self.assertIsNone(music.song_from_entry({**base, "is_live": True}, "user"))
        self.assertIsNone(
            music.song_from_entry(
                {**base, "duration": music.MUSIC_MAX_DURATION + 1}, "user"
            )
        )
        self.assertIsNotNone(
            music.song_from_entry({**base, "duration": 60}, "user")
        )

    def test_the_extracted_format_s_http_headers_survive_onto_the_song(self):
        entry = {
            "url": "https://media.example/audio",
            "title": "Test",
            "duration": 60,
            "http_headers": {"User-Agent": "yt-dlp test client"},
        }
        song = music.song_from_entry(entry, "user")
        self.assertEqual(song["http_headers"], {"User-Agent": "yt-dlp test client"})

    def test_a_song_with_no_http_headers_gets_an_empty_dict(self):
        song = music.song_from_entry(
            {"url": "https://media.example/audio", "title": "Test", "duration": 60},
            "user",
        )
        self.assertEqual(song["http_headers"], {})


class FfmpegHeaderForwardingTests(unittest.TestCase):
    """A googlevideo stream URL is bound to the player client yt-dlp used to
    extract it; fetching it with ffmpeg's bare defaults instead is exactly
    the 403 this replays the format's own `http_headers` to avoid."""

    def test_no_headers_reuses_the_shared_options_object(self):
        song = {"url": "https://media.example/audio", "http_headers": {}}
        self.assertIs(music._ffmpeg_options_for(song), music.ffmpeg_options)

    def test_headers_are_appended_as_a_single_shlex_safe_token(self):
        song = {
            "url": "https://media.example/audio",
            "http_headers": {"User-Agent": "yt-dlp test client"},
        }
        options = music._ffmpeg_options_for(song)
        self.assertNotEqual(options, music.ffmpeg_options)
        self.assertTrue(options["before_options"].startswith(music.ffmpeg_options["before_options"]))
        tokens = shlex.split(options["before_options"])
        self.assertIn("-headers", tokens)
        header_block = tokens[tokens.index("-headers") + 1]
        self.assertEqual(header_block, "User-Agent: yt-dlp test client\r\n")

    def test_a_header_carrying_cr_or_lf_is_dropped_rather_than_forwarded(self):
        song = {
            "url": "https://media.example/audio",
            "http_headers": {
                "User-Agent": "safe",
                "X-Injected": "value\r\nX-Extra: evil",
            },
        }
        options = music._ffmpeg_options_for(song)
        header_block = shlex.split(options["before_options"])[
            shlex.split(options["before_options"]).index("-headers") + 1
        ]
        self.assertEqual(header_block, "User-Agent: safe\r\n")


class _FakeVoiceClient:
    def __init__(self, channel_id):
        self.channel = types.SimpleNamespace(id=channel_id)
        self.disconnect_called = False

    async def disconnect(self):
        self.disconnect_called = True


class _FakeChannel:
    def __init__(self, guild, members, channel_id=1):
        self.guild = guild
        self.members = members
        self.id = channel_id


class _FakeGuild:
    def __init__(self, guild_id, voice_client=None):
        self.id = guild_id
        self.voice_client = voice_client


class _FakePanel:
    def __init__(self):
        self.sent = []
        self.channel = types.SimpleNamespace(send=self._send)

    async def _send(self, content):
        self.sent.append(content)


def _member(is_bot=False):
    return types.SimpleNamespace(bot=is_bot)


class LeftAloneDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """`on_voice_state_update` must disconnect the moment the bot is left
    alone in a voice channel -- there was no such check at all before this,
    so the bot would otherwise sit connected until the unrelated 3-minute
    inactivity timer (`auto_leave_timer`) happened to also be running."""

    def setUp(self):
        self.original_timers = dict(music.music_timers)
        self.original_queues = dict(music.music_queues)
        self.original_states = dict(music.music_states)
        self.original_panels = dict(music.last_panels)
        music.music_timers.clear()
        music.music_queues.clear()
        music.music_states.clear()
        music.last_panels.clear()

    def tearDown(self):
        music.music_timers.clear()
        music.music_timers.update(self.original_timers)
        music.music_queues.clear()
        music.music_queues.update(self.original_queues)
        music.music_states.clear()
        music.music_states.update(self.original_states)
        music.last_panels.clear()
        music.last_panels.update(self.original_panels)

    async def _fire(self, member, before_channel, after_channel):
        before = types.SimpleNamespace(channel=before_channel)
        after = types.SimpleNamespace(channel=after_channel)
        with patch.object(music, "is_enabled", return_value=True):
            await music.Music.on_voice_state_update(None, member, before, after)

    async def test_the_bot_s_own_state_update_is_ignored(self):
        guild = _FakeGuild(1, voice_client=_FakeVoiceClient(channel_id=10))
        channel = _FakeChannel(guild, members=[], channel_id=10)
        await self._fire(_member(is_bot=True), channel, None)
        self.assertFalse(guild.voice_client.disconnect_called)

    async def test_a_mute_or_deafen_toggle_is_not_a_departure(self):
        guild = _FakeGuild(1, voice_client=_FakeVoiceClient(channel_id=10))
        channel = _FakeChannel(guild, members=[_member()], channel_id=10)
        await self._fire(_member(), channel, channel)
        self.assertFalse(guild.voice_client.disconnect_called)

    async def test_a_departure_from_a_channel_the_bot_is_not_in_is_ignored(self):
        guild = _FakeGuild(1, voice_client=_FakeVoiceClient(channel_id=10))
        other_channel = _FakeChannel(guild, members=[], channel_id=99)
        await self._fire(_member(), other_channel, None)
        self.assertFalse(guild.voice_client.disconnect_called)

    async def test_a_real_member_still_present_stops_nothing(self):
        guild = _FakeGuild(1, voice_client=_FakeVoiceClient(channel_id=10))
        channel = _FakeChannel(guild, members=[_member(), _member(is_bot=True)], channel_id=10)
        await self._fire(_member(), channel, None)
        self.assertFalse(guild.voice_client.disconnect_called)

    async def test_the_feature_being_disabled_stops_the_check(self):
        guild = _FakeGuild(1, voice_client=_FakeVoiceClient(channel_id=10))
        channel = _FakeChannel(guild, members=[], channel_id=10)
        before = types.SimpleNamespace(channel=channel)
        after = types.SimpleNamespace(channel=None)
        with patch.object(music, "is_enabled", return_value=False):
            await music.Music.on_voice_state_update(None, _member(), before, after)
        self.assertFalse(guild.voice_client.disconnect_called)

    async def test_everyone_real_leaving_disconnects_cancels_the_timer_and_clears_state(self):
        guild = _FakeGuild(42, voice_client=_FakeVoiceClient(channel_id=10))
        channel = _FakeChannel(guild, members=[_member(is_bot=True)], channel_id=10)
        panel = _FakePanel()
        music.last_panels[guild.id] = panel
        music.music_queues[guild.id] = [{"title": "leftover"}]
        music.music_states[guild.id] = {"current": None, "loop": "off", "start_time": 0}

        cancelled = []

        class _Timer:
            def done(self_inner):
                return False

            def cancel(self_inner):
                cancelled.append(True)

        music.music_timers[guild.id] = _Timer()

        await self._fire(_member(), channel, None)

        self.assertTrue(guild.voice_client.disconnect_called)
        self.assertEqual(cancelled, [True])
        self.assertNotIn(guild.id, music.music_queues)
        self.assertNotIn(guild.id, music.music_states)
        self.assertNotIn(guild.id, music.last_panels)
        self.assertEqual(panel.sent, [music.t("music.left_channel_empty")])

    async def test_no_panel_means_no_send_and_no_crash(self):
        guild = _FakeGuild(7, voice_client=_FakeVoiceClient(channel_id=10))
        channel = _FakeChannel(guild, members=[], channel_id=10)
        await self._fire(_member(), channel, None)
        self.assertTrue(guild.voice_client.disconnect_called)

    async def test_a_panel_send_failure_is_swallowed(self):
        guild = _FakeGuild(9, voice_client=_FakeVoiceClient(channel_id=10))
        channel = _FakeChannel(guild, members=[], channel_id=10)
        panel = _FakePanel()

        async def _boom(content):
            raise RuntimeError("channel gone")

        panel.channel.send = _boom
        music.last_panels[guild.id] = panel

        await self._fire(_member(), channel, None)
        self.assertTrue(guild.voice_client.disconnect_called)


if __name__ == "__main__":
    unittest.main()
