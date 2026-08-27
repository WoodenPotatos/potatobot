"""An LFG post that survives a restart.

`LFGView` was the codebase's own counter-example: non-persistent, a two-hour
timeout, and its whole state — host, game, party — held in memory. A restart left
three buttons answering "This interaction failed", and the post died two hours in
regardless. Making it persistent was never a matter of setting `timeout=None`,
because that state had to live somewhere.

Three properties matter, and the third is the one a persistent view gets wrong.
"""

import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import database

GUILD = 4242
MESSAGE = 1420070400000000001
CHANNEL = 1420070400000000002
HOST = 11
ALICE = 22
BOB = 33
CARA = 44


class LfgPostStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "lfg.db")
        database.initialize_database()
        database.register_guild(GUILD, "Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def create(self, needed=2, **kwargs):
        database.create_lfg_post(GUILD, MESSAGE, CHANNEL, HOST, needed,
                                 **({"game_text": "Valorant"} | kwargs))

    def test_a_post_reads_back_everything_the_embed_needs(self):
        """Nothing may be left in memory, or a restart loses it."""
        self.create(needed=3)
        post = database.get_lfg_post(GUILD, MESSAGE)
        self.assertEqual(CHANNEL, post["channel_id"])
        self.assertEqual(HOST, post["host_id"])
        self.assertEqual(3, post["needed"])
        self.assertEqual("Valorant", post["game_text"])
        self.assertIsNone(post["game_role_id"])
        self.assertEqual([], post["joined"])

    def test_a_role_backed_post_keeps_its_role(self):
        self.create(game_text=None, game_role_id=1420070400000000009)
        post = database.get_lfg_post(GUILD, MESSAGE)
        self.assertEqual(1420070400000000009, post["game_role_id"])
        self.assertIsNone(post["game_text"])

    def test_an_unknown_post_reads_as_absent_rather_than_raising(self):
        """The ordinary case for a post made before this table existed. The
        button answers "expired" instead of failing."""
        self.assertIsNone(database.get_lfg_post(GUILD, 999))
        self.assertEqual({"error": "gone"},
                         database.join_lfg_post(GUILD, 999, ALICE))

    def test_the_party_fills_and_then_refuses(self):
        self.create(needed=2)
        self.assertEqual([ALICE], database.join_lfg_post(GUILD, MESSAGE, ALICE)["joined"])
        result = database.join_lfg_post(GUILD, MESSAGE, BOB)
        self.assertEqual([ALICE, BOB], result["joined"])
        self.assertTrue(result["full"])
        self.assertEqual({"error": "full"},
                         database.join_lfg_post(GUILD, MESSAGE, CARA))

    def test_an_open_ended_post_never_fills(self):
        self.create(needed=0)
        for member in (ALICE, BOB, CARA):
            result = database.join_lfg_post(GUILD, MESSAGE, member)
            self.assertFalse(result["full"])
        self.assertEqual(3, len(database.get_lfg_post(GUILD, MESSAGE)["joined"]))

    def test_the_host_cannot_join_and_nobody_joins_twice(self):
        self.create()
        self.assertEqual({"error": "host"},
                         database.join_lfg_post(GUILD, MESSAGE, HOST))
        database.join_lfg_post(GUILD, MESSAGE, ALICE)
        self.assertEqual({"error": "already"},
                         database.join_lfg_post(GUILD, MESSAGE, ALICE))

    def test_leaving_reopens_a_full_party(self):
        self.create(needed=2)
        database.join_lfg_post(GUILD, MESSAGE, ALICE)
        database.join_lfg_post(GUILD, MESSAGE, BOB)
        result = database.leave_lfg_post(GUILD, MESSAGE, ALICE)
        self.assertEqual([BOB], result["joined"])
        self.assertFalse(result["full"])
        self.assertEqual([BOB, CARA],
                         database.join_lfg_post(GUILD, MESSAGE, CARA)["joined"])

    def test_leaving_a_party_you_are_not_in_is_refused(self):
        self.create()
        self.assertEqual({"error": "absent"},
                         database.leave_lfg_post(GUILD, MESSAGE, ALICE))

    def test_the_party_order_is_preserved(self):
        """The embed prints them in order, so a set would reshuffle the list on
        every read for no reason."""
        self.create(needed=0)
        for member in (CARA, ALICE, BOB):
            database.join_lfg_post(GUILD, MESSAGE, member)
        self.assertEqual([CARA, ALICE, BOB],
                         database.get_lfg_post(GUILD, MESSAGE)["joined"])

    def test_two_people_joining_at_once_cannot_both_take_the_last_slot(self):
        """A conditional UPDATE, not a read followed by a write: otherwise both
        read the same party and one of them silently vanishes."""
        self.create(needed=2)
        database.join_lfg_post(GUILD, MESSAGE, ALICE)

        outcomes = []
        barrier = threading.Barrier(2)

        def attempt(member):
            barrier.wait()
            outcomes.append(database.join_lfg_post(GUILD, MESSAGE, member))

        threads = [threading.Thread(target=attempt, args=(member,))
                   for member in (BOB, CARA)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [result for result in outcomes if "error" not in result]
        self.assertEqual(1, len(accepted), outcomes)
        self.assertEqual(2, len(database.get_lfg_post(GUILD, MESSAGE)["joined"]))

    def test_deleting_forgets_the_post(self):
        self.create()
        self.assertTrue(database.delete_lfg_post(GUILD, MESSAGE))
        self.assertIsNone(database.get_lfg_post(GUILD, MESSAGE))
        # And deleting twice is not an error, so a double click is harmless.
        self.assertFalse(database.delete_lfg_post(GUILD, MESSAGE))

    def test_two_guilds_do_not_share_a_message_id(self):
        database.register_guild(GUILD + 1, "Other")
        self.create()
        self.assertIsNone(database.get_lfg_post(GUILD + 1, MESSAGE))

    def test_a_malformed_party_reads_as_empty_rather_than_raising(self):
        """This runs inside a button callback, where a raise is a dead post."""
        self.create()
        with database.get_connection() as conn:
            conn.execute("UPDATE lfg_posts SET joined_json = 'not json'")
            conn.commit()
        self.assertEqual([], database.get_lfg_post(GUILD, MESSAGE)["joined"])

    def test_pruning_drops_only_the_old_rows(self):
        self.create()
        database.create_lfg_post(GUILD, MESSAGE + 1, CHANNEL, HOST, 0,
                                 game_text="Old")
        with database.get_connection() as conn:
            conn.execute("UPDATE lfg_posts SET created_at = '2020-01-01T00:00:00+00:00' "
                         "WHERE message_id = ?", (MESSAGE + 1,))
            conn.commit()
        self.assertEqual(1, database.prune_lfg_posts(7))
        self.assertIsNotNone(database.get_lfg_post(GUILD, MESSAGE))
        self.assertIsNone(database.get_lfg_post(GUILD, MESSAGE + 1))


class LfgViewTests(unittest.TestCase):
    """The persistent-view rules, which are the easy half to get wrong."""

    def test_the_registered_instance_is_persistent_and_holds_no_post(self):
        from cogs.general import LFGView

        view = LFGView()
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent(),
                        "without static custom_ids a restart loses the buttons")
        self.assertEqual(["lfg:join", "lfg:leave", "lfg:delete"],
                         [child.custom_id for child in view.children])
        # Nothing about any particular post: a persistent view is shared by every
        # message it serves.
        for attribute in ("host", "game_info", "needed", "joined"):
            self.assertFalse(hasattr(view, attribute), attribute)

    def test_every_button_is_enabled_on_the_registered_instance(self):
        """A panel posted while a party was full outlives that party, and a
        click on a button disabled here would be answered by nothing."""
        from cogs.general import LFGView

        self.assertEqual([False, False, False],
                         [child.disabled for child in LFGView().children])

    def test_a_full_party_is_rendered_into_a_fresh_view(self):
        """Disabling Join on the shared instance would disable it on every party
        in the guild."""
        from cogs.general import LFGView, lfg_view_for

        registered = LFGView()
        full = lfg_view_for({"needed": 2, "joined": [ALICE, BOB],
                             "host_id": HOST, "channel_id": CHANNEL,
                             "game_role_id": None, "game_text": "x"})
        self.assertTrue(full.children[0].disabled)
        self.assertFalse(registered.children[0].disabled)

    def test_an_open_ended_party_never_disables_join(self):
        from cogs.general import lfg_view_for

        view = lfg_view_for({"needed": 0, "joined": [ALICE, BOB, CARA],
                             "host_id": HOST, "channel_id": CHANNEL,
                             "game_role_id": None, "game_text": "x"})
        self.assertFalse(view.children[0].disabled)


if __name__ == "__main__":
    unittest.main()
