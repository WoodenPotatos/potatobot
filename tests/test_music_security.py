import asyncio
import json
import re
import shlex
import threading
import time
import types
import unittest
from unittest.mock import patch

from cogs import music


class ExtractionProcessTests(unittest.IsolatedAsyncioTestCase):
    """Route C (2.19): each lookup is a process, and a process can be killed.

    A lookup on a thread that wedged (a hanging DNS resolution is outside
    yt-dlp's `socket_timeout`) could only be abandoned. These drive real
    processes through stand-in scripts in place of `core/music_extract.py`.
    """

    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.processes = []
        self.original_spawn = music.asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await self.original_spawn(*args, **kwargs)
            self.processes.append(process)
            return process

        self.spawn_patch = patch.object(music.asyncio, "create_subprocess_exec", spawn)
        self.spawn_patch.start()

    def tearDown(self):
        self.spawn_patch.stop()
        self.directory.cleanup()

    def script(self, body):
        from pathlib import Path
        path = Path(self.directory.name) / "child.py"
        path.write_text("import json, os, sys, time\n" + body, encoding="utf-8")
        return patch.object(music, "MUSIC_EXTRACT_SCRIPT", path)

    async def test_a_lookup_returns_what_the_process_answers(self):
        with self.script("request = json.load(sys.stdin)\n"
                         "json.dump({'title': request['source'], "
                         "'format': request['options']['format']}, sys.stdout)\n"):
            result = await music.extract_music_info("https://youtu.be/test")
        self.assertEqual({"title": "https://youtu.be/test",
                          "format": music.ytdl_format_options["format"]}, result)

    async def test_a_hung_lookup_is_killed_at_the_timeout(self):
        with self.script("time.sleep(60)\n"), \
                patch.object(music, "MUSIC_EXTRACT_TIMEOUT", 0.5):
            with self.assertRaises(TimeoutError):
                await music.extract_music_info("https://youtu.be/test")
        self.assertIsNotNone(self.processes[0].returncode,
                             "the timed-out lookup is still running")

    async def test_a_cancelled_lookup_is_killed(self):
        with self.script("time.sleep(60)\n"):
            task = asyncio.create_task(music.extract_music_info("https://youtu.be/test"))
            while not self.processes:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_the_process_never_sees_the_bot_token(self):
        with self.script("json.dump(sorted(os.environ), sys.stdout)\n"), \
                patch.dict(music.os.environ, {"DISCORD_TOKEN": "x", "POTATOBOT_DB_PATH": "y"}):
            seen = await music.extract_music_info("https://youtu.be/test")
        self.assertNotIn("DISCORD_TOKEN", seen)
        self.assertNotIn("POTATOBOT_DB_PATH", seen)
        self.assertTrue(set(seen) <= set(music.MUSIC_EXTRACT_ENV_KEYS), seen)

    async def test_a_failure_reports_its_kind_and_not_its_text(self):
        with self.script("sys.stderr.write('DownloadError'); sys.exit(1)\n"):
            with self.assertRaises(music.MusicExtractionError) as caught:
                await music.extract_music_info("ytsearch1:secret song")
        self.assertEqual("DownloadError", music.error_kind(caught.exception))
        self.assertEqual("TimeoutError", music.error_kind(TimeoutError()))

    async def test_stray_stderr_never_becomes_the_kind(self):
        """Found against YouTube: yt-dlp's own error line reached stderr
        carrying the video id, which for a search is the member's text."""
        with self.script("sys.stderr.write('ERROR: [youtube] secret song: unavailable');"
                         " sys.exit(1)\n"):
            with self.assertRaises(music.MusicExtractionError) as caught:
                await music.extract_music_info("ytsearch1:secret song")
        self.assertEqual("unknown", music.error_kind(caught.exception))


class ExtractionChildTests(unittest.TestCase):
    """`core/music_extract.py` itself, against a stand-in yt-dlp."""

    def run_child(self, extractor_class, request):
        import io
        import sys
        from core import music_extract
        fake = types.SimpleNamespace(YoutubeDL=extractor_class)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"yt_dlp": fake}), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(request))), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            code = music_extract.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_the_answer_is_sanitised_and_the_options_are_used(self):
        seen = {}

        class Extractor:
            def __init__(self, options):
                seen["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, source, download):
                seen["source"], seen["download"] = source, download
                return {"title": "song", "entries": iter([{"id": 1}])}

            def sanitize_info(self, info):
                return {**info, "entries": list(info["entries"])}

        code, out, _ = self.run_child(Extractor, {"source": "ytsearch1:x",
                                                  "options": {"quiet": True}})
        self.assertEqual(0, code)
        self.assertEqual({"title": "song", "entries": [{"id": 1}]}, json.loads(out))
        logger = seen["options"].pop("logger")
        self.assertEqual({"quiet": True}, seen["options"])
        # yt-dlp prints errors even when quiet, naming the search; the child
        # hands it a logger that says nothing.
        self.assertIsNone(logger.error("ERROR: [youtube] the member's search"))
        self.assertEqual(("ytsearch1:x", False), (seen["source"], seen["download"]))

    def test_a_failure_writes_only_the_kind(self):
        class Extractor:
            def __init__(self, options):
                raise ValueError("ytsearch1:the member's private search")

        code, out, err = self.run_child(Extractor, {"source": "x", "options": {}})
        self.assertEqual((1, "", "ValueError"), (code, out, err))


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

    def setUp(self):
        # These check pacing, stalls and order; the start-of-song reserve has
        # tests of its own, so here a song starts on its first packet.
        patcher = patch.object(music, "MUSIC_START_RESERVE_PACKETS", 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_song_starts_once_its_reserve_is_buffered(self):
        """Started on the first packet, the buffer had nothing in reserve and
        ran empty for a frame at the start of nearly every song."""
        gate = threading.Event()
        packets = [bytes([n % 250 + 1]) for n in range(40)]
        inner = _FakeInnerSource(packets, gate=gate)
        with patch.object(music, "MUSIC_START_RESERVE_PACKETS", 25):
            source = music.BufferedAudioSource(inner, max_packets=64)
            self.addCleanup(source.cleanup)
            self.assertEqual(music.OPUS_SILENCE, source.read())
            gate.set()
            self.assertTrue(self._wait_for(lambda: source._packets.qsize() >= 25))
            with self.assertNoLogs("PotatoBot.Music", level="WARNING"):
                received = [self._next_audio(source) for _ in packets]
        self.assertEqual(packets, received)

    def test_the_start_is_logged_once_with_its_wait_and_reserve(self):
        inner = _FakeInnerSource([b"a", b"b", b"c"])
        source = music.BufferedAudioSource(inner, max_packets=8)
        self.addCleanup(source.cleanup)
        self.assertTrue(self._wait_for(lambda: source._packets.qsize() == 4))
        with self.assertLogs("PotatoBot.Music", level="INFO") as logs:
            for packet in (b"a", b"b", b"c"):
                self.assertEqual(packet, source.read())
        started = [line for line in logs.output if "Music started" in line]
        self.assertEqual(1, len(started), logs.output)
        self.assertIn("reserve_packets=4", started[0])

    def test_a_stream_stalled_before_its_reserve_starts_with_what_it_has(self):
        stall = threading.Event()
        inner = _FakeInnerSource([b"a", b"b"], stall=stall)
        self.addCleanup(stall.set)
        with patch.object(music, "MUSIC_START_RESERVE_PACKETS", 25):
            source = music.BufferedAudioSource(inner, max_packets=64)
            self.addCleanup(source.cleanup)
            self.assertTrue(self._wait_for(lambda: source._packets.qsize() == 2))
            self.assertEqual(music.OPUS_SILENCE, source.read())
            source._created -= music.MUSIC_START_RESERVE_MAX_SECONDS + 1
            self.assertEqual(b"a", source.read())

    @staticmethod
    def _next_audio(source, timeout=2.0):
        """The next real packet, skipping the silent frames a read hands the
        player while it has nothing to play yet."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            packet = source.read()
            if packet != music.OPUS_SILENCE:
                return packet
        raise AssertionError("no audio arrived")

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
        received = [self._next_audio(source) for _ in packets]
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

    def test_a_late_ask_by_the_player_is_logged_and_a_pause_is_not(self):
        """A gap then a rush with the buffer full (2026-10-31) means the player
        was held up, not the stream; this is the line that tells them apart."""
        source = music.BufferedAudioSource(
            _FakeInnerSource([b"x"] * 10), max_packets=16)
        self.addCleanup(source.cleanup)
        self.assertTrue(self._wait_for(lambda: source._packets.qsize() >= 5))
        clock = [100.0]
        with patch.object(music.time, "monotonic", lambda: clock[0]), \
                self.assertLogs(music.music_logger, "WARNING") as logged:
            source.read()                     # the first packet: nothing to compare
            clock[0] += 0.02
            source.read()                     # on time
            clock[0] += 0.6
            source.read()                     # late: logged
            clock[0] += 60
            source.read()                     # a pause: not logged
            music.music_logger.warning("end of test")
        late = [line for line in logged.output if "asked late" in line]
        self.assertEqual(1, len(late), logged.output)
        self.assertIn("gap_ms=600", late[0])

    def test_a_stall_is_silence_on_time_logged_once_with_its_length(self):
        """A read that waited made discord.py's player fall behind and then
        rush the backlog; silence keeps it on schedule (2026-10-31)."""
        stall = threading.Event()
        inner = _FakeInnerSource([b"a"], stall=stall)
        source = music.BufferedAudioSource(inner, max_packets=4)
        self.addCleanup(source.cleanup)
        self.assertEqual(self._next_audio(source), b"a")
        with self.assertLogs("PotatoBot.Music", level="INFO") as logs:
            for _ in range(5):
                started = time.monotonic()
                self.assertEqual(music.OPUS_SILENCE, source.read())
                self.assertLess(time.monotonic() - started, 0.1,
                                "a read waited longer than a frame")
            inner._packets.append(b"b")
            stall.set()
            self.assertEqual(self._next_audio(source), b"b")
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(1, sum("ran empty" in message for message in messages), messages)
        self.assertEqual(1, sum("recovered after" in message for message in messages), messages)
        self.assertIn("ran empty at 0.0 s into the song", " ".join(messages))

    def test_the_start_of_a_song_is_silence_not_a_stall_and_not_a_wait(self):
        """ffmpeg still connecting is where the start-of-song rush came from:
        the player counted the wait for the first packet as lateness."""
        gate = threading.Event()
        inner = _FakeInnerSource([b"a"], gate=gate)
        source = music.BufferedAudioSource(inner, max_packets=4)
        self.addCleanup(source.cleanup)
        with self.assertLogs("PotatoBot.Music", level="INFO") as logs:
            started = time.monotonic()
            self.assertEqual(music.OPUS_SILENCE, source.read())
            self.assertLess(time.monotonic() - started, 0.1)
            gate.set()
            self.assertEqual(self._next_audio(source), b"a")
        # The start is announced, never reported as a stall.
        self.assertEqual(["Music started"],
                         [record.getMessage().split(" (")[0] for record in logs.records])

    def test_discord_pys_own_player_does_not_rush_after_a_slow_start(self):
        """End to end, with discord.py's real `AudioPlayer` and a client that
        records send times. With ffmpeg taking half a second to start, a read
        that waited sent ~24 packets back to back (half a second of audio at
        once, the start-of-song rush heard on 2026-10-31); silence on time
        sends none early."""
        from discord.player import AudioPlayer

        gate = threading.Event()
        source = music.BufferedAudioSource(_FakeInnerSource([b"x"] * 80, gate=gate),
                                           max_packets=100)
        sent = []
        client = types.SimpleNamespace(
            send_audio_packet=lambda data, encode=True: sent.append(
                (time.perf_counter(), data)),
            is_connected=lambda: True, wait_until_connected=lambda timeout: True)
        player = AudioPlayer(source, client)
        player._speak = lambda state: None
        threading.Timer(0.5, gate.set).start()
        player.start()
        player.join(timeout=10)
        audio = [moment for moment, data in sent if data == b"x"]
        self.assertEqual(80, len(audio))
        back_to_back = sum(1 for earlier, later in zip(audio, audio[1:])
                           if later - earlier < 0.005)
        self.assertLessEqual(back_to_back, 2, "the player rushed a backlog")

    def test_a_stream_that_never_starts_is_given_up(self):
        gate = threading.Event()
        inner = _FakeInnerSource([b"a"], gate=gate)
        source = music.BufferedAudioSource(inner, max_packets=4)
        self.addCleanup(source.cleanup)
        self.addCleanup(gate.set)
        source._created -= music.MUSIC_START_GIVE_UP_SECONDS + 1
        with self.assertLogs("PotatoBot.Music", level="WARNING"):
            self.assertEqual(b"", source.read())
        self.assertEqual(b"", source.read())

    def test_an_inner_read_error_ends_the_stream_and_forwards_the_error(self):
        inner = _FakeInnerSource([b"a"], error=OSError("pipe"))
        inner._current_error = OSError("ffmpeg exited")
        with self.assertLogs("PotatoBot.Music", level="WARNING"):
            source = music.BufferedAudioSource(inner, max_packets=4)
            self.addCleanup(source.cleanup)
            self.assertEqual(self._next_audio(source), b"a")
            self.assertEqual(self._next_audio(source), b"")
        self.assertIs(source._current_error, inner._current_error)


class SearchWithoutTimerTests(unittest.IsolatedAsyncioTestCase):
    """Queuing songs back to back (2026-10-31): the search button opens the
    box every time; a second lookup at once is what is refused, not a second
    search a few seconds later."""

    async def test_two_searches_in_a_row_both_open_the_box(self):
        opened = []
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(voice_client=object()),
            user=types.SimpleNamespace(id=4242),
            response=types.SimpleNamespace(
                send_modal=lambda modal: _record(opened, modal),
                send_message=lambda *a, **k: _record(opened, "refused")))
        view = music.MusicPanelView()
        await view.search_btn(interaction)
        await view.search_btn(interaction)
        self.assertEqual(2, len(opened))
        self.assertTrue(all(isinstance(item, music.MusicSearchModal) for item in opened))

    def test_play_has_no_timer_but_one_at_a_time(self):
        command = music.Music.play
        self.assertIsNone(command._buckets._cooldown, "a timer came back on /play")
        self.assertIsNotNone(command._max_concurrency)
        self.assertEqual(1, command._max_concurrency.number)


async def _record(collected, item):
    collected.append(item)


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
