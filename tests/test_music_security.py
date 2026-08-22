import unittest

from cogs import music


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
