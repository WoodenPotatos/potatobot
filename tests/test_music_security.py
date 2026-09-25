import time
import unittest
from concurrent.futures import ThreadPoolExecutor

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


if __name__ == "__main__":
    unittest.main()
