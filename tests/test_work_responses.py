"""Per-guild `/work` outcome odds, payouts and response text.

Every response lives in `work_responses`: the installation defaults at
`WORK_DEFAULT_GUILD_ID` and a guild's own rows above them. A response has no
language dimension on purpose — it is one guild's flavour text and a guild speaks
one language — so the shipped defaults are English and anything a guild writes is
taken as written.

Three rules matter. A guild that has written nothing gets the defaults and the
previous one-in-a-thousand odds exactly. A guild that writes responses for one
tier overrides only that tier. And operator-authored text is escaped and
substituted safely before it can reach message content.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import database
from cogs import casino
from settings_registry import SETTING_DEFINITIONS

ROOT = Path(__file__).resolve().parents[1]


class WorkResponseStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "work.db")
        database.initialize_database()
        database.register_guild(7, "Work Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_a_guild_with_no_rows_of_its_own_sees_the_installation_defaults(self):
        rows = database.get_work_responses(7)
        self.assertEqual(len(database.WORK_DEFAULT_RESPONSES), len(rows))
        self.assertEqual({"default"}, {row["scope"] for row in rows})
        # Every tier has something, or that outcome would have nothing to say.
        self.assertEqual(set(database.WORK_TIERS), {row["tier"] for row in rows})

    def test_seeding_the_defaults_twice_changes_nothing(self):
        """It is gated on absence rather than on a schema version, so re-running
        the migration must not duplicate them or overwrite an edit."""
        with database.get_connection() as conn:
            self.assertEqual(0, database.seed_default_work_responses(conn))
        self.assertEqual(len(database.WORK_DEFAULT_RESPONSES),
                         len(database.get_work_responses(7)))

    def test_the_shipped_defaults_are_english(self):
        """They travel with every copy of the bot, so they cannot be one
        installation's language."""
        hungarian = re.compile(r"[ÁÉÍÓÖŐÚÜŰáéíóöőúüű]")
        for tier, message in database.WORK_DEFAULT_RESPONSES:
            with self.subTest(message=message[:40]):
                self.assertIn(tier, database.WORK_TIERS)
                self.assertIsNone(hungarian.search(message))

    def test_a_paying_tier_default_carries_the_earnings_placeholder(self):
        """A `normal` or `high` response that never says what was earned is a
        worse message than no message."""
        for tier, message in database.WORK_DEFAULT_RESPONSES:
            if tier == "free":
                continue
            with self.subTest(message=message[:40]):
                self.assertIn(database.WORK_EARNINGS_PLACEHOLDER, message)

    def test_a_free_tier_default_never_promises_a_payout(self):
        for tier, message in database.WORK_DEFAULT_RESPONSES:
            if tier != "free":
                continue
            with self.subTest(message=message[:40]):
                self.assertNotIn(database.WORK_EARNINGS_PLACEHOLDER, message)

    def own_rows(self, guild_id=7):
        return [row for row in database.get_work_responses(guild_id)
                if row["scope"] == "guild"]

    def test_create_read_update_delete_round_trip(self):
        created = database.create_work_response(7, 99, "normal", "Paid {earnings}.", 5)
        self.assertEqual(1, created["revision"])
        stored = self.own_rows()
        self.assertEqual(1, len(stored))
        self.assertEqual("Paid {earnings}.", stored[0]["message"])

        updated = database.update_work_response(
            7, 99, created["response_id"], "high", "Big {earnings}!", 3, False, 1
        )
        self.assertEqual(2, updated["revision"])
        self.assertFalse(self.own_rows()[0]["enabled"])

        with self.assertRaises(database.RevisionConflictError):
            database.delete_work_response(7, 99, created["response_id"], 1)
        database.delete_work_response(7, 99, created["response_id"], 2)
        self.assertEqual([], self.own_rows())

    def test_editing_or_deleting_an_unknown_response_is_a_lookup_failure(self):
        with self.assertRaises(LookupError):
            database.update_work_response(7, 99, 4242, "normal", "x", 1, True, 1)
        with self.assertRaises(LookupError):
            database.delete_work_response(7, 99, 4242, 1)

    def test_blank_overlong_and_badly_weighted_responses_are_refused(self):
        with self.assertRaises(database.ValidationError) as blank:
            database.create_work_response(7, 99, "normal", "   ")
        self.assertEqual("work_message_invalid", blank.exception.reason)
        with self.assertRaises(database.ValidationError):
            database.create_work_response(
                7, 99, "normal", "x" * (database.WORK_MESSAGE_MAX_LENGTH + 1)
            )
        with self.assertRaises(database.ValidationError) as weight:
            database.create_work_response(7, 99, "normal", "ok", 0)
        self.assertEqual("work_weight_invalid", weight.exception.reason)
        with self.assertRaises(database.ValidationError) as tier:
            database.create_work_response(7, 99, "legendary", "ok")
        self.assertEqual("work_tier_invalid", tier.exception.reason)

    def test_a_boolean_weight_is_refused_rather_than_counted_as_one(self):
        with self.assertRaises(database.ValidationError):
            database.create_work_response(7, 99, "normal", "ok", True)

    def test_editing_a_shipped_response_adopts_its_tier_first(self):
        """The shipped set is shared, so it is copied rather than edited.

        It used to be unreachable: a guild could see a default and never touch
        it, so the page rendered half its rows read-only with a badge and offered
        a separate "copy them all" button. That reads as something broken rather
        than as a starting point. Editing one now copies that tier into the guild
        and edits the copy, in one transaction.
        """
        shipped = [row for row in database.get_work_responses(7)
                   if row["tier"] == "normal"]
        self.assertTrue(all(row["scope"] == "default" for row in shipped))
        target = shipped[0]

        database.update_work_response(
            7, 99, target["response_id"], "normal", "my own line", 4, True,
            target["revision"],
        )

        after = [row for row in database.get_work_responses(7)
                 if row["tier"] == "normal"]
        self.assertTrue(all(row["scope"] == "guild" for row in after),
                        "the whole tier is adopted, not just the edited row")
        self.assertEqual(len(shipped), len(after))
        self.assertIn("my own line", [row["message"] for row in after])

        # The shipped rows themselves are untouched, because every other guild
        # still resolves to them.
        installation = database.get_work_responses(database.WORK_DEFAULT_GUILD_ID)
        self.assertNotIn("my own line", [row["message"] for row in installation])

    def test_deleting_a_shipped_response_adopts_the_tier_and_drops_that_line(self):
        shipped = [row for row in database.get_work_responses(7)
                   if row["tier"] == "high"]
        target = shipped[0]

        database.delete_work_response(7, 99, target["response_id"],
                                      target["revision"])

        after = [row for row in database.get_work_responses(7)
                 if row["tier"] == "high"]
        self.assertEqual(len(shipped) - 1, len(after))
        self.assertTrue(all(row["scope"] == "guild" for row in after))
        self.assertNotIn(target["message"], [row["message"] for row in after])

    def test_adopting_a_tier_leaves_the_other_tiers_shipped(self):
        """Resolution is per tier, so writing your own big-payday lines does not
        blank the other two."""
        target = next(row for row in database.get_work_responses(7)
                      if row["tier"] == "high")
        database.update_work_response(
            7, 99, target["response_id"], "high", "mine", 1, True,
            target["revision"],
        )
        scopes = {}
        for row in database.get_work_responses(7):
            scopes.setdefault(row["tier"], set()).add(row["scope"])
        self.assertEqual({"guild"}, scopes["high"])
        self.assertEqual({"default"}, scopes["normal"])
        self.assertEqual({"default"}, scopes["free"])

    def test_adoption_happens_once(self):
        first = next(row for row in database.get_work_responses(7)
                     if row["tier"] == "normal")
        database.update_work_response(
            7, 99, first["response_id"], "normal", "one", 1, True,
            first["revision"],
        )
        owned = [row for row in database.get_work_responses(7)
                 if row["tier"] == "normal"]
        second = owned[0]
        database.update_work_response(
            7, 99, second["response_id"], "normal", "two", 1, True,
            second["revision"],
        )
        after = [row for row in database.get_work_responses(7)
                 if row["tier"] == "normal"]
        self.assertEqual(len(owned), len(after), "a second edit must not re-adopt")

    def test_the_per_tier_limit_is_enforced(self):
        for index in range(database.WORK_RESPONSES_PER_TIER):
            database.create_work_response(7, 99, "free", f"line {index}")
        with self.assertRaises(database.ValidationError) as error:
            database.create_work_response(7, 99, "free", "one too many")
        self.assertEqual("work_response_limit", error.exception.reason)
        # The cap is per tier, so another tier is unaffected.
        database.create_work_response(7, 99, "high", "still fine")

    def test_every_change_is_audited(self):
        created = database.create_work_response(7, 99, "normal", "one")
        database.update_work_response(
            7, 99, created["response_id"], "normal", "two", 1, True, 1
        )
        database.delete_work_response(7, 99, created["response_id"], 2)
        actions = {row["action"] for row in database.get_settings_audit(7)}
        self.assertLessEqual(
            {"work_response.create", "work_response.update", "work_response.delete"},
            actions,
        )


class WorkSelectionTests(unittest.TestCase):
    def test_shipped_weights_reproduce_the_previous_one_in_a_thousand_odds(self):
        resolved = casino.work_settings({})
        self.assertEqual(
            {"normal": 998, "free": 1, "high": 1},
            casino.work_tier_weights(resolved),
        )

    def test_default_payout_and_xp_match_the_previous_hard_coded_values(self):
        resolved = casino.work_settings({})
        self.assertEqual(500, resolved["work_payout_min"])
        self.assertEqual(3000, resolved["work_payout_max"])
        self.assertEqual(10000, resolved["work_high_payout_min"])
        self.assertEqual(30000, resolved["work_high_payout_max"])
        self.assertEqual((25, 50, 5), (resolved["work_xp_normal"],
                                      resolved["work_xp_free"],
                                      resolved["work_xp_high"]))

    def test_a_stored_setting_overrides_its_default(self):
        resolved = casino.work_settings({"work_tier_high_weight": {"value": 400,
                                                                  "revision": 1}})
        self.assertEqual(400, casino.work_tier_weights(resolved)["high"])

    def test_a_wrongly_typed_stored_value_falls_back_to_the_default(self):
        """A row written by an older or broken client must not crash a command."""
        resolved = casino.work_settings({"work_payout_min": {"value": "nope",
                                                            "revision": 1}})
        self.assertEqual(500, resolved["work_payout_min"])

    def test_all_zero_weights_still_pay_an_ordinary_shift(self):
        self.assertEqual(
            "normal",
            casino.weighted_tier({"normal": 0, "free": 0, "high": 0}),
        )

    def test_a_tier_with_all_its_weight_always_wins(self):
        for _ in range(20):
            self.assertEqual(
                "high",
                casino.weighted_tier({"normal": 0, "free": 0, "high": 5}),
            )

    def test_an_inverted_payout_range_does_not_raise(self):
        """An operator can save a minimum above the maximum; randint would raise."""
        for _ in range(20):
            self.assertTrue(100 <= casino.random_payout(900, 100) <= 900)
        self.assertEqual(400, casino.random_payout(400, 400))

    def test_a_tier_with_no_row_at_all_says_so_rather_than_going_blank(self):
        """Discord rejects an empty embed description, so an installation whose
        defaults were all deleted needs something to send."""
        rendered = casino.work_response_text("free", [], 0)
        self.assertTrue(rendered.strip())
        self.assertNotIn("[", rendered)

    def test_it_draws_only_from_the_pool_it_was_given(self):
        """Choosing which scope is in effect is `get_work_responses`' job now.

        This used to loop over the scopes itself, which was a second copy of that
        rule — and it had a bug the duplication hid: a guild whose only row for a
        tier was *disabled* fell through to the shipped line, so switching your
        own line off silently brought the default back. `CLAUDE.md` says the
        opposite, and says why: disabling is a decision about that tier.
        """
        pool = [
            {"tier": "normal", "weight": 1, "enabled": True,
             "message": "resolved normal"},
            {"tier": "free", "weight": 1, "enabled": True,
             "message": "resolved free"},
        ]
        for _ in range(20):
            self.assertEqual("resolved normal",
                             casino.work_response_text("normal", pool, 0))
            self.assertEqual("resolved free",
                             casino.work_response_text("free", pool, 0))

    def test_a_disabled_row_does_not_resurrect_anything(self):
        """A tier whose every line is off has nothing to say, and says so."""
        pool = [{"tier": "high", "weight": 1, "enabled": False,
                 "message": "switched off"}]
        from cogs.utils import t
        self.assertEqual(t("casino.work_no_response"),
                         casino.work_response_text("high", pool, 0))

    def test_stored_responses_override_only_their_own_tier(self):
        stored = [{"tier": "normal", "weight": 1, "enabled": True,
                   "message": "Custom normal"}]
        self.assertEqual("Custom normal",
                         casino.work_response_text("normal", stored, 10))
        self.assertNotEqual("Custom normal",
                            casino.work_response_text("high", stored, 10))

    def test_a_disabled_or_zero_weight_response_is_never_drawn(self):
        stored = [
            {"tier": "normal", "weight": 1, "enabled": False, "message": "off"},
            {"tier": "normal", "weight": 1, "enabled": True, "message": "on"},
        ]
        for _ in range(20):
            self.assertEqual("on", casino.work_response_text("normal", stored, 1))

    def test_the_earnings_placeholder_is_substituted(self):
        stored = [{"tier": "high", "weight": 1, "enabled": True,
                   "message": "You earned {earnings} coins."}]
        self.assertEqual("You earned 1234 coins.",
                         casino.work_response_text("high", stored, 1234))

    def test_operator_text_cannot_inject_a_mention(self):
        """The text is operator supplied and lands in message content."""
        stored = [{"tier": "normal", "weight": 1, "enabled": True,
                   "message": "@everyone got paid"}]
        rendered = casino.work_response_text("normal", stored, 0)
        self.assertNotIn("@everyone", rendered)

    def test_a_stray_brace_in_operator_text_does_not_raise(self):
        """Substitution is a literal replace, not str.format."""
        stored = [{"tier": "normal", "weight": 1, "enabled": True,
                   "message": "Paid {earnings} out of {unknown_field}"}]
        rendered = casino.work_response_text("normal", stored, 5)
        self.assertEqual("Paid 5 out of {unknown_field}", rendered)

    def test_every_work_setting_is_registered_and_owned_by_economy(self):
        work_keys = [key for key in SETTING_DEFINITIONS if key.startswith("work_")]
        self.assertEqual(10, len(work_keys))
        for key in work_keys:
            with self.subTest(setting=key):
                definition = SETTING_DEFINITIONS[key]
                self.assertEqual("economy", definition.owner_feature)
                self.assertEqual("work", definition.page)

    def test_no_work_response_is_left_in_a_locale_catalog(self):
        """They moved into the database, so a `casino.job_*` key surviving would
        be one installation's jokes shipping with every copy of the bot."""
        for language in ("hu", "en"):
            catalog = json.loads(
                (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )["casino"]
            with self.subTest(language=language):
                self.assertEqual(
                    [], [key for key in catalog if key.startswith("job")]
                )


if __name__ == "__main__":
    unittest.main()
