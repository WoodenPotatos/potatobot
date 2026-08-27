"""The two channel games the bot only polices.

Nothing here is a bot game: people post in a channel and the bot's whole job is
to notice the wrong message, remove it, and say why to its author alone. Three
things are therefore worth pinning down, and only one of them is the happy path.

**Chatter is left alone.** A channel where an aside is deleted is a channel
nobody talks in, so a message that is not an attempt at all must produce no
verdict rather than a refusal.

**The fold is applied to both sides.** Comparing an accented entry against a
bare one has to fold the message *and* the stored value, exactly as the word
filter does — folding one side is how a rule becomes an argument.

**Two people posting at once cannot both be accepted.** The turn is a
conditional UPDATE rather than a read followed by a write, so the loser is told
the chain moved rather than the count silently skipping.
"""

import os
import tempfile
import unittest

import database
from cogs.minigames import COUNT_PATTERN, WORD_PATTERN, MILESTONE_EVERY, fold


GUILD = 4242
ALICE = 11
BOB = 22


class FoldTests(unittest.TestCase):
    def test_case_and_accents_fold_away(self):
        self.assertEqual(fold("Alma"), fold("ALMA"))
        self.assertEqual(fold("almá"), fold("alma"))
        self.assertEqual(fold("ÁRVÍZTŰRŐ"), fold("arvizturo"))

    def test_different_words_stay_different(self):
        self.assertNotEqual(fold("alma"), fold("elme"))

    def test_the_fold_never_empties_a_word(self):
        """A word that folded to nothing would join every chain."""
        for word in ("alma", "ÉS", "őz", "Straße"):
            self.assertTrue(fold(word))


class PatternTests(unittest.TestCase):
    def test_a_count_is_a_number_and_nothing_else(self):
        self.assertTrue(COUNT_PATTERN.match(" 42 "))
        self.assertIsNone(COUNT_PATTERN.match("42!"))
        self.assertIsNone(COUNT_PATTERN.match("42 nice"))
        self.assertIsNone(COUNT_PATTERN.match("forty two"))

    def test_a_chain_entry_is_one_word_of_letters(self):
        self.assertTrue(WORD_PATTERN.match("  alma "))
        self.assertTrue(WORD_PATTERN.match("ÁRVÍZTŰRŐ"))
        self.assertIsNone(WORD_PATTERN.match("two words"))
        self.assertIsNone(WORD_PATTERN.match("https://example.invalid"))
        self.assertIsNone(WORD_PATTERN.match("a"))          # one letter
        self.assertIsNone(WORD_PATTERN.match("42"))         # a count, not a word

    def test_chatter_matches_neither_pattern(self):
        """Both patterns must decline, or one game would police the other's
        channel by accident."""
        for chatter in ("nice one", "lol :)", "@someone hi", ""):
            self.assertIsNone(COUNT_PATTERN.match(chatter))
            self.assertIsNone(WORD_PATTERN.match(chatter))


class MinigameStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "minigames.db")
        database.initialize_database()
        database.register_guild(GUILD, "Minigame Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_a_guild_that_has_never_played_reads_as_new(self):
        state = database.get_minigame_state(GUILD, "counting")
        self.assertEqual("", state["value"])
        self.assertIsNone(state["last_user_id"])
        self.assertEqual(0, state["streak"])

    def test_a_turn_advances_the_chain_and_records_who_took_it(self):
        moved = database.advance_minigame(GUILD, "counting", "1", ALICE, "")
        self.assertEqual({"value": "1", "streak": 1, "best_streak": 1}, moved)
        state = database.get_minigame_state(GUILD, "counting")
        self.assertEqual("1", state["value"])
        self.assertEqual(ALICE, state["last_user_id"])

    def test_a_second_turn_on_a_stale_value_is_refused(self):
        """Two people posting the next number in the same instant."""
        database.advance_minigame(GUILD, "counting", "1", ALICE, "")
        # Both believed the chain was at "1"; only one can be right afterwards.
        self.assertIsNotNone(
            database.advance_minigame(GUILD, "counting", "2", BOB, "1"))
        self.assertIsNone(
            database.advance_minigame(GUILD, "counting", "2", ALICE, "1"))
        self.assertEqual("2", database.get_minigame_state(GUILD, "counting")["value"])

    def test_the_first_turn_is_refused_when_the_chain_already_moved(self):
        """The insert branch is conditional too, or a racing first message
        would create the row twice."""
        database.advance_minigame(GUILD, "counting", "1", ALICE, "")
        self.assertIsNone(
            database.advance_minigame(GUILD, "counting", "1", BOB, ""))

    def test_the_two_games_keep_separate_chains(self):
        database.advance_minigame(GUILD, "counting", "1", ALICE, "")
        self.assertEqual("", database.get_minigame_state(GUILD, "word_chain")["value"])

    def test_two_guilds_keep_separate_chains(self):
        database.register_guild(GUILD + 1, "Other")
        database.advance_minigame(GUILD, "counting", "1", ALICE, "")
        self.assertEqual(
            "", database.get_minigame_state(GUILD + 1, "counting")["value"])

    def test_reset_keeps_the_best_streak_as_a_record(self):
        for number in range(1, 6):
            database.advance_minigame(GUILD, "counting", str(number), ALICE,
                                      str(number - 1) if number > 1 else "")
        database.reset_minigame(GUILD, "counting")
        state = database.get_minigame_state(GUILD, "counting")
        self.assertEqual("", state["value"])
        self.assertEqual(0, state["streak"])
        self.assertEqual(5, state["best_streak"])
        # And the chain accepts a first turn again afterwards.
        self.assertIsNotNone(
            database.advance_minigame(GUILD, "counting", "1", BOB, ""))

    def test_reset_on_a_guild_that_never_played_is_a_no_op(self):
        database.reset_minigame(GUILD, "word_chain")
        self.assertEqual(
            "", database.get_minigame_state(GUILD, "word_chain")["value"])

    def test_the_milestone_is_a_round_number_not_a_new_best(self):
        """The chain never breaks by itself, so `streak == best_streak` always.

        Reacting on a new best would therefore put a trophy on every message
        from the second one onward, which is why the trophy is a milestone.
        """
        previous = ""
        marked = []
        for number in range(1, 251):
            moved = database.advance_minigame(GUILD, "counting", str(number),
                                              ALICE, previous)
            previous = str(number)
            self.assertEqual(moved["streak"], moved["best_streak"])
            if moved["streak"] % MILESTONE_EVERY == 0:
                marked.append(moved["streak"])
        self.assertEqual([100, 200], marked)


if __name__ == "__main__":
    unittest.main()
