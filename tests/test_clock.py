"""The one clock, and the bridge from the rows written before it existed.

Every write is UTC-aware now. The part that has to be right on the day it ships
is the *read* of what was already there: months of `datetime.now().isoformat()`
rows carry no offset and mean the host's local time. Reading one as UTC would
move every existing cooldown by the offset, once, silently.
"""

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from core import database
from core.clock import local_date, local_time, parse_stored, utc_now


class LocalZone:
    """Pin the process to a zone with a spring clock change, then put it back.

    `astimezone()` on a naive value reads the process's local zone, so the tests
    that depend on it must not depend on wherever the runner happens to be.
    """

    def __init__(self, zone="Europe/Budapest"):
        self.zone = zone

    def __enter__(self):
        self.previous = os.environ.get("TZ")
        os.environ["TZ"] = self.zone
        time.tzset()
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.previous
        time.tzset()


class ClockTests(unittest.TestCase):
    def test_now_is_aware_and_utc(self):
        now = utc_now()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(timedelta(0), now.utcoffset())

    def test_an_aware_value_round_trips_to_the_same_instant(self):
        written = utc_now().isoformat()
        self.assertEqual(datetime.fromisoformat(written), parse_stored(written))

    def test_a_naive_value_is_local_time_not_utc(self):
        with LocalZone():
            legacy = "2026-07-01T14:00:00"  # written by datetime.now() in summer
            parsed = parse_stored(legacy)
            # Budapest is UTC+2 in July, so 14:00 local is 12:00 UTC.
            self.assertEqual(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc), parsed)
            self.assertEqual(14, local_time(parsed).hour)

    def test_a_cooldown_across_the_clock_change_is_the_real_elapsed_time(self):
        """Naive subtraction said two hours; one hour passed."""
        with LocalZone():
            before = "2026-03-29T01:30:00"  # clocks go 02:00 -> 03:00 that night
            after = "2026-03-29T03:30:00"
            naive = datetime.fromisoformat(after) - datetime.fromisoformat(before)
            self.assertEqual(timedelta(hours=2), naive)
            self.assertEqual(timedelta(hours=1), parse_stored(after) - parse_stored(before))

    def test_the_calendar_day_is_the_hosts_not_utcs(self):
        with LocalZone():
            # 23:30 UTC on the 1st is 01:30 on the 2nd in Budapest (summer).
            instant = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
            self.assertEqual(2, local_date(instant).day)
            self.assertEqual(1, instant.date().day)

    def test_garbage_still_raises_like_fromisoformat_did(self):
        with self.assertRaises(ValueError):
            parse_stored("not a time")


class LegacyRowsKeepTheirCooldownsTests(unittest.TestCase):
    """The rows already in the database are read as what they were."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "clock.db")
        database.initialize_database()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (1, 100)")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _set(self, column, value):
        with database.get_connection() as conn:
            conn.execute(f"UPDATE users SET {column} = ? WHERE user_id = 1", (value,))

    def test_a_naive_work_stamp_ten_minutes_ago_still_blocks(self):
        with LocalZone():
            legacy = (datetime.now() - timedelta(minutes=10)).isoformat()  # naive, local
            self._set("last_job", legacy)
            result = database.claim_timed_reward(
                1, "last_job", utc_now().isoformat(), 50, 5, interval_seconds=15 * 60)
        self.assertFalse(result["claimed"])

    def test_a_naive_work_stamp_twenty_minutes_ago_has_expired(self):
        with LocalZone():
            legacy = (datetime.now() - timedelta(minutes=20)).isoformat()
            self._set("last_job", legacy)
            result = database.claim_timed_reward(
                1, "last_job", utc_now().isoformat(), 50, 5, interval_seconds=15 * 60)
        self.assertTrue(result["claimed"])

    def test_a_naive_streak_stamp_from_yesterday_continues_the_streak(self):
        with LocalZone():
            yesterday = (datetime.now() - timedelta(days=1)).isoformat()
            self._set("streak_count", 4)
            self._set("last_streak_update", yesterday)
            result = database.claim_everydle_reward(
                1, "last_valdle", utc_now().isoformat(), 500, 100)
        self.assertTrue(result["claimed"])
        self.assertEqual(5, result["streak"])

    def test_a_daily_claimed_earlier_today_local_time_is_refused(self):
        with LocalZone():
            earlier = datetime.now().replace(hour=0, minute=5, second=0).isoformat()
            self._set("last_daily", earlier)
            result = database.claim_timed_reward(
                1, "last_daily", utc_now().isoformat(), 500, 50, once_per_day=True)
        self.assertFalse(result["claimed"])


if __name__ == "__main__":
    unittest.main()
