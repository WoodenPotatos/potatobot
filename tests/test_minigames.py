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

**A Hungarian letter may be more than one character.** `sz`, `gy` and their
seven siblings are single letters to anyone playing, so a chain that compares
one character at a time refuses the word the player is right about.

**Two people posting at once cannot both be accepted.** The turn is a
conditional UPDATE rather than a read followed by a write, so the loser is told
the chain moved rather than the count silently skipping.
"""

import os
import tempfile
import types
import unittest

from core import database
from core import settings_cache
from cogs.minigames import (COUNT_PATTERN, WORD_PATTERN, HUNGARIAN_LETTERS,
                            MILESTONE_EVERY, Minigames, first_letter, fold,
                            last_letter, unique_value)


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


class HungarianLetterTests(unittest.TestCase):
    """Nine letters are written with two characters here, and one with three.

    The alphabet is passed in rather than read, so these are the letters
    themselves rather than a test of which language is configured.
    """

    def test_a_word_ends_in_the_letter_a_player_would_name(self):
        for word, letter in (("busz", "sz"), ("kulcs", "cs"), ("torzs", "zs"),
                             ("hany", "ny"), ("hattyu", "u"), ("bridzs", "dzs"),
                             ("haz", "z"), ("alma", "a")):
            with self.subTest(word=word):
                self.assertEqual(letter, last_letter(word, HUNGARIAN_LETTERS))

    def test_a_doubled_digraph_needs_no_special_case(self):
        """Hungarian doubles the first character, so the last two already spell it."""
        for word, letter in (("rossz", "sz"), ("meggy", "gy"), ("asszony", "ny"),
                             ("kaccs", "cs"), ("hattyu", "u")):
            with self.subTest(word=word):
                self.assertEqual(letter, last_letter(word, HUNGARIAN_LETTERS))

    def test_the_match_is_longest_first(self):
        """`dzs` contains both `dz` and `zs`, and is the only reason for the order."""
        self.assertEqual("dzs", last_letter("bridzs", HUNGARIAN_LETTERS))
        self.assertEqual("dzs", first_letter("dzsungel", HUNGARIAN_LETTERS))
        self.assertEqual("dz", last_letter("edz", HUNGARIAN_LETTERS))
        self.assertEqual("zs", last_letter("zizs", HUNGARIAN_LETTERS))

    def test_a_word_starts_with_the_same_kind_of_letter(self):
        for word, letter in (("szek", "sz"), ("tyuk", "ty"), ("gyar", "gy"),
                             ("zebra", "z"), ("alma", "a")):
            with self.subTest(word=word):
                self.assertEqual(letter, first_letter(word, HUNGARIAN_LETTERS))

    def test_an_empty_alphabet_is_one_character_each_way(self):
        """What every other language plays by, and what this did before.

        `only` and `many` are the reason the alphabet is not simply always on:
        under Hungarian letters an English chain would demand a word beginning
        `ly` or `ny` after each of them.
        """
        self.assertEqual("y", last_letter("only", ()))
        self.assertEqual("y", last_letter("many", ()))
        self.assertEqual("s", first_letter("something", ()))


class WordChainLetterTests(unittest.TestCase):
    """The judgement itself, driven through the real method.

    `judge_word` reads nothing off the message but its content, so a stand-in
    with that one attribute exercises the real path.
    """

    def setUp(self):
        self.cog = Minigames(bot=None)
        settings_cache.invalidate()

    def tearDown(self):
        # A test that writes a setting must clear the cache: `settings_cache`
        # is process-global and would otherwise answer for a later test.
        settings_cache.invalidate()

    def speak(self, language):
        settings_cache.apply_changes(GUILD, {"language": {"value": language}})

    def judge(self, posted, previous):
        return self.cog.judge_word(types.SimpleNamespace(content=posted),
                                   {"value": previous})

    def test_hungarian_joins_a_digraph_to_a_digraph(self):
        self.speak("hu")
        accepted, value, _ = self.judge("szek", "busz")
        self.assertTrue(accepted)
        self.assertEqual("szek", value)

    def test_hungarian_refuses_the_bare_letter_the_digraph_ends_with(self):
        """The reported defect: `busz` accepted `zebra` and deleted `szek`."""
        self.speak("hu")
        accepted, _, reason = self.judge("zebra", "busz")
        self.assertFalse(accepted)
        self.assertIn("SZ", reason)

    def test_english_plays_by_single_letters(self):
        self.speak("en")
        self.assertTrue(self.judge("yellow", "only")[0])
        self.assertFalse(self.judge("nyu", "only")[0])

    def test_an_accent_still_joins_the_chain(self):
        """The fold runs before the letters, so both sides fold."""
        self.speak("hu")
        self.assertTrue(self.judge("Szék", "BUSZ")[0])

    def test_chatter_is_still_no_verdict_at_all(self):
        self.speak("hu")
        self.assertIsNone(self.judge("this is a sentence", "busz"))

    def test_the_first_word_of_a_chain_needs_no_letter(self):
        self.speak("hu")
        self.assertTrue(self.judge("szek", "")[0])


class UsedWordTests(unittest.TestCase):
    """A word counts once, and the claim rides inside the turn.

    The claim is an `INSERT OR IGNORE` against a primary key rather than a
    `SELECT` followed by an `INSERT`, for the same reason the turn itself is a
    conditional UPDATE: two people posting one word in the same instant must
    not both be accepted.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "minigames.db")
        database.initialize_database()
        database.register_guild(GUILD, "Minigame Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def play(self, word, expected, user=ALICE, guild=GUILD, game="word_chain"):
        return database.advance_minigame(guild, game, word, user, expected,
                                         fold(word))

    def test_a_word_already_played_is_refused_and_the_chain_stays_put(self):
        self.play("alma", "")
        self.play("aszal", "alma")
        self.assertEqual({"duplicate": True}, self.play("alma", "aszal", BOB))
        state = database.get_minigame_state(GUILD, "word_chain")
        self.assertEqual("aszal", state["value"])
        self.assertEqual(2, state["streak"])

    def test_a_refused_duplicate_is_told_apart_from_a_race(self):
        """Two rejections, two answers: a caller reads a streak off neither."""
        self.play("alma", "")
        self.assertIsNone(self.play("aszal", "stale"))
        self.assertEqual({"duplicate": True}, self.play("alma", "alma"))

    def test_a_word_the_chain_has_not_had_still_advances(self):
        self.play("alma", "")
        moved = self.play("aszal", "alma")
        self.assertEqual(2, moved["streak"])

    def test_the_fold_decides_what_counts_as_the_same_word(self):
        self.play("alma", "")
        self.play("aszal", "alma")
        self.assertEqual({"duplicate": True}, self.play("ALMÁ", "aszal"))

    def test_a_losing_turn_leaves_its_word_unclaimed(self):
        """A claim on its own connection would forbid a word nobody played."""
        self.play("alma", "")
        self.assertIsNone(self.play("aszal", "stale"))
        self.assertIsNotNone(self.play("aszal", "alma"))

    def test_counting_spends_nothing_and_records_nothing(self):
        """Its values cannot repeat, so a table of them would answer nothing."""
        self.assertIsNone(unique_value("counting", "7"))
        for number in range(1, 6):
            database.advance_minigame(
                GUILD, "counting", str(number), ALICE,
                str(number - 1) if number > 1 else "",
                unique_value("counting", str(number)))
        self.assertEqual(0, self.used_word_count())

    def test_a_word_is_folded_before_it_is_spent(self):
        self.assertEqual(fold("ALMÁ"), unique_value("word_chain", "ALMÁ"))

    def test_a_race_outranks_a_duplicate_when_a_turn_is_both(self):
        """The race is what actually happened to them, so it is what they hear.

        Either answer refuses the turn and neither strands the word — the claim
        rides in the same transaction, so a rollback gives it back whichever
        check fired. What differs is the message, and "somebody got there
        first" is the one a member can act on.
        """
        self.play("alma", "")
        self.play("aszal", "alma")
        self.assertIsNone(self.play("alma", "stale"))

    def test_the_two_games_and_two_guilds_keep_separate_histories(self):
        self.play("alma", "")
        database.register_guild(GUILD + 1, "Other Guild")
        self.assertIsNotNone(self.play("alma", "", guild=GUILD + 1))

    def test_reset_gives_every_word_back(self):
        self.play("alma", "")
        database.reset_minigame(GUILD, "word_chain")
        self.assertEqual(0, self.used_word_count())
        self.assertIsNotNone(self.play("alma", ""))

    def test_reset_leaves_the_other_game_alone(self):
        self.play("alma", "")
        database.reset_minigame(GUILD, "counting")
        self.assertEqual(1, self.used_word_count())

    def used_word_count(self):
        with database.get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM minigame_used_words").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
