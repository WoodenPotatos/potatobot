import re
import shlex
import threading
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
        base = {"url": "https://media.example/audio", "title": "Test",
                "extractor_key": "Youtube"}
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
            "extractor_key": "Youtube",
        }
        song = music.song_from_entry(entry, "user")
        self.assertEqual(song["http_headers"], {"User-Agent": "yt-dlp test client"})

    def test_a_song_with_no_http_headers_gets_an_empty_dict(self):
        song = music.song_from_entry(
            {"url": "https://media.example/audio", "title": "Test", "duration": 60,
             "extractor_key": "Youtube"},
            "user",
        )
        self.assertEqual(song["http_headers"], {})

    def test_spellings_no_youtube_extractor_claims_are_normalised_or_refused(self):
        """Each of these passed the host check but was handed over verbatim,
        so no YouTube extractor claimed it and it fell to `generic`."""
        self.assertEqual("https://www.youtube.com/watch?v=abc",
                         music._youtube_input("https://www.youtube.com./watch?v=abc"))
        self.assertEqual("https://www.youtube.com/watch?v=abc",
                         music._youtube_input("https://WWW.YOUTUBE.COM/watch?v=abc"))
        for source in ("https://youtu.be/../x", "https://youtu.be/%2e%2e/x"):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    music._youtube_input(source)

    def test_only_youtube_extractors_are_loaded(self):
        """The allowlist is what keeps `generic` out of reach entirely."""
        import yt_dlp
        with yt_dlp.YoutubeDL(dict(music.ytdl_format_options)) as extractor:
            loaded = set(extractor._ies)
        self.assertNotIn("Generic", loaded)
        self.assertTrue(loaded)
        self.assertTrue(all(key.startswith("Youtube") for key in loaded), loaded)

    def test_an_entry_from_another_extractor_is_refused(self):
        entry = {"url": "https://media.example/audio", "title": "Test",
                 "duration": 60, "extractor_key": "Generic"}
        self.assertIsNone(music.song_from_entry(entry, "user"))
        self.assertIsNone(music.song_from_entry(
            {**entry, "extractor_key": None}, "user"))


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


class OpusSourceTests(unittest.TestCase):
    """ffmpeg encodes Opus itself, so the bot's process never runs the encoder.

    `FFmpegPCMAudio` handed raw PCM to discord.py, which encoded Opus inside
    the bot a 20 ms frame at a time for every playing guild -- the heaviest
    per-guild CPU cost and the one place the GIL could turn music into
    event-loop lag for every other command. Nothing adjusts volume, so nothing
    needs PCM. The security options must reach the new class unchanged.
    """

    def _play(self, song):
        class _Loop:
            def create_task(self, coroutine):
                coroutine.close()

            def call_soon_threadsafe(self, *args):
                pass

        played = []
        voice = types.SimpleNamespace(play=lambda source, after: played.append(source))
        guild = types.SimpleNamespace(id=4242, voice_client=voice)
        cog = music.Music.__new__(music.Music)
        cog.bot = types.SimpleNamespace(loop=_Loop())
        original = dict(music.music_queues)
        self.addCleanup(lambda: (music.music_queues.clear(), music.music_queues.update(original)))
        music.music_queues[guild.id] = [song]
        with patch.object(music.discord, "FFmpegOpusAudio") as opus:
            cog.play_next(guild, types.SimpleNamespace())
        return opus, played

    def test_playback_uses_ffmpeg_opus_with_the_security_options(self):
        song = {"url": "https://media.example/audio", "title": "t",
                "requester": "r", "http_headers": {"User-Agent": "ua"}}
        opus, played = self._play(song)
        opus.assert_called_once_with(song["url"], **music._ffmpeg_options_for(song))
        # The ffmpeg source reaches the player through the read-ahead buffer.
        self.assertEqual(len(played), 1)
        self.assertIsInstance(played[0], music.BufferedAudioSource)
        self.assertIs(played[0].inner, opus.return_value)
        played[0].cleanup()
        options = opus.call_args.kwargs
        self.assertIn("-protocol_whitelist http,https,tcp,tls,crypto", options["before_options"])
        self.assertIn("-nostdin", options["before_options"])
        self.assertIn("-headers", options["before_options"])


class _FakeInnerSource:
    """Yields `packets`, then `b''`; `gate` (if given) blocks every read.

    `stall` (if given) blocks a read once the packets run out, standing in
    for ffmpeg mid-reconnect.
    """

    def __init__(self, packets, gate=None, error=None, stall=None):
        self._packets = list(packets)
        self.gate = gate
        self.error = error
        self.stall = stall
        self.cleaned = threading.Event()
        self._current_error = None

    def read(self):
        if self.gate is not None:
            self.gate.wait()
        if self.cleaned.is_set():
            raise ValueError("read of closed file")
        if self._packets:
            return self._packets.pop(0)
        if self.stall is not None:
            self.stall.wait()
            if self.cleaned.is_set():
                raise ValueError("read of closed file")
            if self._packets:
                return self._packets.pop(0)
        if self.error is not None:
            raise self.error
        return b""

    def is_opus(self):
        return True

    def cleanup(self):
        self.cleaned.set()
        for event in (self.gate, self.stall):
            if event is not None:
                event.set()


class BufferedAudioSourceTests(unittest.TestCase):
    """A stalled ffmpeg read must spend the buffer, not stall the player.

    discord.py's player sends a stalled backlog with no delay once the read
    returns, so a stall was heard as a gap and then as sped-up audio.
    """

    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_every_packet_arrives_in_order_then_the_end(self):
        packets = [bytes([n]) * 3 for n in range(1, 50)]
        source = music.BufferedAudioSource(_FakeInnerSource(packets), max_packets=8)
        self.addCleanup(source.cleanup)
        self.assertTrue(source.is_opus())
        received = [source.read() for _ in packets]
        self.assertEqual(received, packets)
        self.assertEqual(source.read(), b"")
        self.assertEqual(source.read(), b"")

    def test_buffered_packets_are_served_while_the_inner_read_is_stalled(self):
        stall = threading.Event()
        inner = _FakeInnerSource([b"a", b"b", b"c"], stall=stall)
        source = music.BufferedAudioSource(inner, max_packets=8)
        self.addCleanup(source.cleanup)
        # Three packets buffered; the producer's next inner read is stalled.
        self.assertTrue(self._wait_for(lambda: source._packets.qsize() == 3))
        started = time.monotonic()
        self.assertEqual([source.read() for _ in range(3)], [b"a", b"b", b"c"])
        self.assertLess(time.monotonic() - started, 0.5)

    def test_cleanup_cleans_the_inner_source_and_the_producer_exits(self):
        gate = threading.Event()
        inner = _FakeInnerSource([b"x"] * 100, gate=gate)
        gate.set()
        source = music.BufferedAudioSource(inner, max_packets=4)
        # The producer is now blocked on a full queue.
        self.assertTrue(self._wait_for(lambda: source._packets.full()))
        source.cleanup()
        self.assertTrue(inner.cleaned.is_set())
        source._producer.join(timeout=2.0)
        self.assertFalse(source._producer.is_alive())
        self.assertEqual(source.read(), b"")

    def test_a_stall_after_playback_started_is_logged_with_its_length(self):
        stall = threading.Event()
        inner = _FakeInnerSource([b"a"], stall=stall)
        source = music.BufferedAudioSource(inner, max_packets=4)
        self.addCleanup(source.cleanup)
        self.assertEqual(source.read(), b"a")
        threading.Timer(0.5, lambda: (inner._packets.append(b"b"), stall.set())).start()
        with self.assertLogs("PotatoBot.Music", level="INFO") as logs:
            self.assertEqual(source.read(), b"b")
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(1, sum("ran empty" in message for message in messages), messages)
        self.assertEqual(1, sum("recovered after" in message for message in messages), messages)

    def test_waiting_for_the_first_packet_is_not_a_stall(self):
        gate = threading.Event()
        inner = _FakeInnerSource([b"a"], gate=gate)
        source = music.BufferedAudioSource(inner, max_packets=4)
        self.addCleanup(source.cleanup)
        threading.Timer(0.5, gate.set).start()
        with self.assertNoLogs("PotatoBot.Music", level="INFO"):
            self.assertEqual(source.read(), b"a")

    def test_an_inner_read_error_ends_the_stream_and_forwards_the_error(self):
        inner = _FakeInnerSource([b"a"], error=OSError("pipe"))
        inner._current_error = OSError("ffmpeg exited")
        with self.assertLogs("PotatoBot.Music", level="WARNING"):
            source = music.BufferedAudioSource(inner, max_packets=4)
            self.addCleanup(source.cleanup)
            self.assertEqual(source.read(), b"a")
            self.assertEqual(source.read(), b"")
        self.assertIs(source._current_error, inner._current_error)


class MusicSearchModalTests(unittest.IsolatedAsyncioTestCase):
    """The panel's search box takes a pasted link as well as a search."""

    VIDEO = {
        "url": "https://media.example/audio", "title": "Bekopog",
        "duration": 218, "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=LSY3iG895D0",
    }

    async def _submit(self, query, extracted):
        sent = []

        async def followup_send(message, **kwargs):
            sent.append(message)

        async def defer(**kwargs):
            pass

        async def extract(source):
            return extracted

        async def allowed(interaction, feature):
            return True

        guild = types.SimpleNamespace(
            id=7171,
            voice_client=types.SimpleNamespace(
                is_playing=lambda: True, is_paused=lambda: False
            ),
        )
        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=55, display_name="woody"),
            guild=guild,
            channel=types.SimpleNamespace(),
            response=types.SimpleNamespace(defer=defer),
            followup=types.SimpleNamespace(send=followup_send),
        )
        modal = music.MusicSearchModal()
        modal.query = types.SimpleNamespace(value=query)
        original = dict(music.music_queues)
        self.addCleanup(lambda: (music.music_queues.clear(), music.music_queues.update(original)))
        music.music_queues.pop(guild.id, None)
        with patch.object(music, "require_interaction_feature", allowed), \
                patch.object(music, "can_control_music", lambda *a: True), \
                patch.object(music, "extract_music_info", extract):
            await modal.on_submit(interaction)
        return music.music_queues.get(guild.id, []), sent

    async def test_a_pasted_video_link_is_queued(self):
        queue, sent = await self._submit(
            "https://www.youtube.com/watch?v=LSY3iG895D0", dict(self.VIDEO)
        )
        self.assertEqual([song["title"] for song in queue], ["Bekopog"])
        self.assertEqual(sent, [music.t("music.added_to_queue", title="Bekopog")])

    async def test_a_search_result_is_queued(self):
        queue, sent = await self._submit(
            "bekopog", {"entries": [dict(self.VIDEO)], "extractor_key": "YoutubeSearch"}
        )
        self.assertEqual([song["title"] for song in queue], ["Bekopog"])

    async def test_a_search_with_no_results_says_so(self):
        queue, sent = await self._submit(
            "justvidman bekopog", {"entries": [], "extractor_key": "YoutubeSearch"}
        )
        self.assertEqual(queue, [])
        self.assertEqual(sent, [music.t("music.search_no_results")])


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


class PanelRepliesSurviveThePanelTests(unittest.IsolatedAsyncioTestCase):
    """A panel action's public result must not be a reply to the panel.

    `play_next` deletes the panel whenever the song changes, so a public reply
    to a panel button was left pointing at a deleted message and Discord drew
    "Message could not be loaded" (reported 2026-10-22, on skip).
    """

    PANEL_CLASSES = ("MusicPanelView", "MusicRemoveModal", "MusicSearchModal")

    def test_no_panel_callback_replies_publicly(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(music))
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef) and node.name in self.PANEL_CLASSES):
                continue
            for call in ast.walk(node):
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "send_message"):
                    continue
                private = any(
                    keyword.arg == "ephemeral"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in call.keywords
                )
                if not private:
                    offenders.append(f"{node.name}:{call.lineno}")
        self.assertEqual(offenders, [], "use announce_to_channel for a public result")

    async def test_skip_acknowledges_silently_and_announces_in_the_channel(self):
        calls = []

        async def defer(**kwargs):
            calls.append(("defer", kwargs))

        async def send_message(*args, **kwargs):
            calls.append(("reply", args))

        async def channel_send(message, **kwargs):
            calls.append(("channel", message))

        stopped = []
        voice = types.SimpleNamespace(is_playing=lambda: True, stop=lambda: stopped.append(1))
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1, voice_client=voice),
            response=types.SimpleNamespace(defer=defer, send_message=send_message),
            channel=types.SimpleNamespace(send=channel_send),
        )
        view = music.MusicPanelView.__new__(music.MusicPanelView)
        await music.MusicPanelView.skip(view, interaction)
        self.assertEqual(stopped, [1])
        self.assertEqual(calls, [("defer", {}), ("channel", music.t("music.skipped"))])


class VoiceDependenciesTests(unittest.TestCase):
    """Voice needs PyNaCl and, since discord.py 2.7, `davey` (Discord's
    end-to-end-encrypted voice). Without either, `VoiceClient.__init__` raises
    `RuntimeError` and every `/join` and `/play` answers "could not connect".

    `davey` was installed into the bare-metal venv by hand and never pinned,
    so the container, which installs strictly from `requirements.lock`, had no
    voice at all on the podman cutover (2026-10-22). The lock is asserted as
    well as the import, because CI's environment is built from the lock and a
    hand-installed package anywhere else hides the gap.
    """

    def test_discord_py_finds_both_voice_libraries(self):
        import discord.voice_client
        import discord.voice_state

        self.assertTrue(discord.voice_client.has_nacl, "PyNaCl is not importable")
        self.assertTrue(discord.voice_state.has_dave, "davey is not importable")

    def test_both_are_pinned_in_the_lock_the_image_installs(self):
        from pathlib import Path

        lock = (Path(__file__).resolve().parents[1] / "requirements.lock").read_text(
            encoding="utf-8")
        for package in ("pynacl==", "davey=="):
            with self.subTest(package=package):
                self.assertRegex(lock, rf"(?m)^{re.escape(package)}")
