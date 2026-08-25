"""Tagged warnings, their consequences, and the word filter.

Three things here fail silently rather than loudly, which is why each has a
test. A normaliser that only folds case is evaded by a full stop. An escalation
that reads the count on a second connection lets two moderators both miss the
threshold. And a filter that acts on its own staff is worse than no filter.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import database
from settings_registry import (
    SETTING_DEFINITIONS,
    WARN_ACTIONS,
    WARN_DEFAULT_TAG,
    WARN_TAGS,
)


class WarnRegistryTests(unittest.TestCase):
    def test_every_tag_has_a_threshold_an_action_and_a_duration(self):
        for tag in WARN_TAGS:
            for key in (f"warn_threshold_{tag}", f"warn_action_{tag}",
                        f"warn_timeout_minutes_{tag}"):
                with self.subTest(key=key):
                    self.assertIn(key, SETTING_DEFINITIONS)

    def test_nothing_escalates_out_of_the_box(self):
        """An upgrade must not start handing out consequences."""
        for tag in WARN_TAGS:
            self.assertEqual(0, SETTING_DEFINITIONS[f"warn_threshold_{tag}"].default,
                             "a shipped threshold of anything but 0 acts unasked")
            self.assertEqual("none", SETTING_DEFINITIONS[f"warn_action_{tag}"].default)

    def test_an_action_setting_is_constrained_to_the_action_list(self):
        for tag in WARN_TAGS:
            definition = SETTING_DEFINITIONS[f"warn_action_{tag}"]
            self.assertEqual(WARN_ACTIONS, definition.choices)
            # Without a prefix the dashboard would show the raw identifier.
            self.assertEqual("dashboard.warn_actions",
                             definition.choice_locale_prefix)

    def test_the_filter_files_a_warning_rather_than_deciding_a_consequence(self):
        definition = SETTING_DEFINITIONS["word_filter_tag"]
        self.assertEqual(WARN_TAGS, definition.choices)
        self.assertEqual(WARN_DEFAULT_TAG, definition.default)

    def test_exempt_roles_are_recognised_not_granted(self):
        """Filtering the selector by grantability once un-premiumed a guild's
        staff, because those roles sit above the bot deliberately."""
        self.assertFalse(
            SETTING_DEFINITIONS["word_filter_exempt_roles"].role_must_be_assignable)


class FilterNormalisationTests(unittest.TestCase):
    """A filter that only catches the literal spelling is security theatre."""

    def setUp(self):
        from cogs.moderation import normalise_for_filter
        self.fold = normalise_for_filter

    def test_every_evasion_form_folds_to_the_same_thing(self):
        target = self.fold("bad")
        for probe in ("bad", "BAD", "B.A.D", "b a d", "b-a-d", "baaaad", "b4d",
                      "b@d", "bád", "ｂａｄ", "B A     D"):
            with self.subTest(probe=probe):
                self.assertIn(target, self.fold(probe))

    def test_an_unrelated_word_does_not_fold_onto_it(self):
        self.assertNotIn(self.fold("bad"), self.fold("wonderful"))

    def test_punctuation_only_folds_to_nothing(self):
        # Such an entry must be dropped from the list, or it matches everything.
        self.assertEqual("", self.fold("..."))
        self.assertEqual("", self.fold(""))
        self.assertEqual("", self.fold(None))

    def test_collapsing_repeats_is_applied_to_both_sides(self):
        """Documented widening: the fold is lossy, so it has to be symmetric."""
        self.assertEqual(self.fold("good"), self.fold("god"))


class StreakFreezeTests(unittest.TestCase):
    """One item, two ways to obtain it, and one forgiven day."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        self.guild, self.user = 55, 7
        self.now = datetime(2026, 6, 20)

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _set_streak(self, count, days_ago):
        when = (self.now - timedelta(days=days_ago)).isoformat()
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                         (self.user,))
            conn.execute(
                "UPDATE users SET streak_count = ?, last_streak_update = ?, "
                "last_valdle = NULL, last_dbdle_killer = NULL WHERE user_id = ?",
                (count, when, self.user))

    def _give(self, quantity):
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO user_inventory (guild_id, user_id, item_key, "
                "quantity, updated_at) VALUES (?, ?, 'streak_freeze', ?, ?) "
                "ON CONFLICT(guild_id, user_id, item_key) "
                "DO UPDATE SET quantity = ?",
                (self.guild, self.user, quantity, "now", quantity))

    def _held(self):
        return database.get_user_inventory(self.guild, self.user).get(
            "streak_freeze", 0)

    def _claim(self, guild_id=None, column="last_valdle"):
        return database.claim_everydle_reward(
            self.user, column, self.now.isoformat(), 500, 100, guild_id)

    def test_the_item_is_one_catalog_entry_reachable_from_both_systems(self):
        import item_catalog
        definition = item_catalog.ITEM_DEFINITIONS["streak_freeze"]
        self.assertTrue(definition.sold_in_shop)
        self.assertTrue(definition.drawable_in_gacha)
        self.assertIn("streak_freeze", item_catalog.INVENTORY_ITEM_KEYS)
        # Four stars, which is where the plan put it.
        four_star = database.DEFAULT_GACHA_CONFIG["rewards"]["4"]
        self.assertIn("streak_freeze", [entry["key"] for entry in four_star])

    def test_the_built_in_grace_is_used_before_an_item_is_spent(self):
        # A single missed day was always forgiven; charging for it would be a
        # regression dressed up as a feature.
        self._set_streak(10, 2)
        self._give(1)
        result = self._claim(self.guild)
        self.assertEqual(11, result["streak"])
        self.assertFalse(result["froze_streak"])
        self.assertEqual(1, self._held())

    def test_a_freeze_covers_the_day_that_would_have_reset_the_streak(self):
        self._set_streak(10, 3)
        self._give(1)
        result = self._claim(self.guild)
        self.assertEqual(11, result["streak"])
        self.assertTrue(result["froze_streak"])
        self.assertEqual(0, self._held())

    def test_without_one_that_day_resets_the_streak(self):
        self._set_streak(10, 3)
        result = self._claim(self.guild)
        self.assertEqual(1, result["streak"])
        self.assertFalse(result["froze_streak"])

    def test_a_longer_absence_resets_and_does_not_waste_the_item(self):
        """One freeze is one day, not a permanent streak."""
        self._set_streak(10, 4)
        self._give(1)
        result = self._claim(self.guild)
        self.assertEqual(1, result["streak"])
        self.assertFalse(result["froze_streak"])
        self.assertEqual(1, self._held(), "a hopeless gap must not spend it")

    def test_one_freeze_cannot_be_spent_twice(self):
        self._set_streak(10, 3)
        self._give(1)
        first = self._claim(self.guild)
        self._set_streak(10, 3)
        second = self._claim(self.guild, "last_dbdle_killer")
        self.assertTrue(first["froze_streak"])
        self.assertFalse(second["froze_streak"])
        self.assertEqual(0, self._held())

    def test_inventory_is_guild_local_so_another_guild_cannot_spend_it(self):
        self._set_streak(10, 3)
        self._give(1)
        result = self._claim(999)
        self.assertEqual(1, result["streak"])
        self.assertEqual(1, self._held())

    def test_no_guild_context_behaves_exactly_as_before(self):
        self._set_streak(10, 3)
        self._give(1)
        result = self._claim(None)
        self.assertEqual(1, result["streak"])
        self.assertEqual(1, self._held())


class EscalationTests(unittest.IsolatedAsyncioTestCase):
    """Alerting and acting are separate, and neither may act on the untouchable."""

    class _Role:
        """A role that orders by position, the way discord.Role does.

        The production check is `guild.me.top_role <= member.top_role`, which is
        the idiomatic comparison; a stand-in that only carries a number would
        have forced that into comparing `.position` by hand.
        """

        def __init__(self, position):
            self.position = position

        def __le__(self, other):
            return self.position <= other.position

        def __lt__(self, other):
            return self.position < other.position

    def _guild(self, *, bot_position=10, log_channel=1):
        sent = []
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=lambda **kw: sent.append(kw)))
        guild = SimpleNamespace(
            id=55, owner_id=1,
            me=SimpleNamespace(top_role=self._Role(bot_position)),
            get_channel=lambda cid: channel if cid == log_channel else None,
        )
        return guild, sent

    def _member(self, *, position=1, administrator=False, member_id=7):
        return SimpleNamespace(
            id=member_id, mention=f"<@{member_id}>",
            top_role=self._Role(position),
            guild_permissions=SimpleNamespace(administrator=administrator),
            timeout=AsyncMock(), kick=AsyncMock(), ban=AsyncMock(),
        )

    async def _run(self, settings, *, alerts=True, actions=True,
                   member=None, guild=None, tag="spam", count=3):
        import cogs.moderation as moderation
        guild = guild or self._guild()[0]
        member = member or self._member()
        flags = {"moderation_warn_alerts": alerts,
                 "moderation_warn_actions": actions}
        with patch.object(moderation, "guild_settings_many",
                          AsyncMock(return_value=settings)), \
             patch.object(moderation, "is_enabled",
                          lambda gid, key: flags.get(key, True)):
            applied = await moderation.apply_warn_escalation(
                guild, member, tag, count, "because")
        return applied, member

    async def test_a_zero_threshold_never_acts(self):
        applied, member = await self._run({
            "warn_threshold_spam": 0, "warn_action_spam": "ban",
            "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1})
        self.assertIsNone(applied)
        member.ban.assert_not_awaited()

    async def test_below_the_threshold_nothing_happens(self):
        applied, member = await self._run({
            "warn_threshold_spam": 5, "warn_action_spam": "kick",
            "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1})
        self.assertIsNone(applied)
        member.kick.assert_not_awaited()

    async def test_reaching_the_threshold_applies_the_configured_action(self):
        for action, attribute in (("timeout", "timeout"), ("kick", "kick"),
                                  ("ban", "ban")):
            with self.subTest(action=action):
                applied, member = await self._run({
                    "warn_threshold_spam": 3, f"warn_action_spam": action,
                    "warn_timeout_minutes_spam": 15,
                    "moderation_log_channel": 1})
                self.assertEqual(action, applied)
                getattr(member, attribute).assert_awaited_once()

    async def test_a_timeout_uses_the_tag_s_own_duration(self):
        applied, member = await self._run({
            "warn_threshold_spam": 3, "warn_action_spam": "timeout",
            "warn_timeout_minutes_spam": 15, "moderation_log_channel": 1})
        self.assertEqual("timeout", applied)
        self.assertEqual(timedelta(minutes=15),
                         member.timeout.await_args.args[0])

    async def test_alerting_works_with_actions_off(self):
        """The whole point of the split."""
        guild, sent = self._guild()
        applied, member = await self._run({
            "warn_threshold_spam": 3, "warn_action_spam": "ban",
            "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1},
            actions=False, guild=guild)
        self.assertIsNone(applied)
        member.ban.assert_not_awaited()
        self.assertEqual(1, len(sent), "the alert must still be posted")

    async def test_acting_works_with_alerts_off(self):
        guild, sent = self._guild()
        applied, member = await self._run({
            "warn_threshold_spam": 3, "warn_action_spam": "kick",
            "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1},
            alerts=False, guild=guild)
        self.assertEqual("kick", applied)
        self.assertEqual([], sent)

    async def test_an_administrator_is_never_escalated_against(self):
        applied, member = await self._run(
            {"warn_threshold_spam": 3, "warn_action_spam": "ban",
             "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1},
            member=self._member(administrator=True))
        self.assertIsNone(applied)
        member.ban.assert_not_awaited()

    async def test_the_guild_owner_is_never_escalated_against(self):
        applied, member = await self._run(
            {"warn_threshold_spam": 3, "warn_action_spam": "ban",
             "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1},
            member=self._member(member_id=1))
        self.assertIsNone(applied)
        member.ban.assert_not_awaited()

    async def test_a_member_above_the_bot_is_not_attempted(self):
        applied, member = await self._run(
            {"warn_threshold_spam": 3, "warn_action_spam": "kick",
             "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1},
            member=self._member(position=99))
        self.assertIsNone(applied)
        member.kick.assert_not_awaited()

    async def test_a_refused_action_still_reports_what_happened(self):
        guild, sent = self._guild()
        member = self._member()
        member.kick = AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="no"), "forbidden"))
        applied, _ = await self._run(
            {"warn_threshold_spam": 3, "warn_action_spam": "kick",
             "warn_timeout_minutes_spam": 60, "moderation_log_channel": 1},
            member=member, guild=guild)
        self.assertIsNone(applied)
        self.assertEqual(1, len(sent))

    async def test_no_log_channel_does_not_stop_the_action(self):
        applied, member = await self._run({
            "warn_threshold_spam": 3, "warn_action_spam": "kick",
            "warn_timeout_minutes_spam": 60, "moderation_log_channel": None})
        self.assertEqual("kick", applied)


class WordFilterListenerTests(unittest.IsolatedAsyncioTestCase):
    """What the filter must refuse to act on, and what it must not say out loud."""

    def _cog(self, **overrides):
        import cogs.moderation as moderation
        cog = moderation.Moderation.__new__(moderation.Moderation)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        from bounded import BoundedValueMap
        cog._filter_cache = BoundedValueMap(max_entries=8)
        settings = {"word_filter_words": ["bad"],
                    "word_filter_exempt_roles": [],
                    "word_filter_tag": "language",
                    "word_filter_delete_message": True}
        settings.update(overrides)
        return cog, settings

    def _message(self, content, *, roles=(), manage_messages=False,
                 author_id=7, owner_id=1):
        deleted, dmed = [], []
        # Spec'd against discord.Member so the listener's isinstance guard —
        # which is what keeps it off a webhook or a partial author — holds.
        author = MagicMock(spec=discord.Member)
        author.id = author_id
        author.mention = f"<@{author_id}>"
        author.roles = [SimpleNamespace(id=role_id) for role_id in roles]
        author.bot = False
        author.guild_permissions = SimpleNamespace(
            manage_messages=manage_messages)
        author.send = AsyncMock(side_effect=lambda text: dmed.append(text))
        guild = SimpleNamespace(id=55, name="Guild", owner_id=owner_id,
                                get_channel=lambda cid: None)
        message = SimpleNamespace(
            guild=guild, author=author, content=content,
            channel=SimpleNamespace(name="general"),
            delete=AsyncMock(side_effect=lambda: deleted.append(True)),
        )
        return message, deleted, dmed

    async def _dispatch(self, cog, settings, message, *, enabled=True,
                        maintenance=False):
        import cogs.moderation as moderation
        recorded = []
        async def record(function, *args):
            recorded.append(args)
            return {"warning_id": 1, "total": 1, "tag_count": 1, "tag": args[-1]}
        with patch.object(moderation, "guild_settings_many",
                          AsyncMock(return_value=settings)), \
             patch.object(moderation, "is_enabled", lambda gid, key: enabled), \
             patch.object(moderation, "maintenance_blocks",
                          lambda guild, user: maintenance), \
             patch.object(moderation.database, "run", AsyncMock(side_effect=record)), \
             patch.object(moderation, "apply_warn_escalation",
                          AsyncMock(return_value=None)) as escalate:
            await cog.on_message(message)
        return recorded, escalate

    async def test_a_match_is_deleted_warned_and_escalated(self):
        cog, settings = self._cog()
        message, deleted, dmed = self._message("this is b.a.d actually")
        recorded, escalate = await self._dispatch(cog, settings, message)
        self.assertEqual([True], deleted)
        self.assertEqual(1, len(recorded), "one warning must be filed")
        self.assertEqual("language", recorded[0][-1], "under the configured tag")
        self.assertEqual(1, len(dmed), "the member is told privately")
        escalate.assert_awaited_once()

    async def test_the_stored_reason_never_repeats_the_matched_word(self):
        cog, settings = self._cog()
        message, _, dmed = self._message("b a d")
        recorded, _ = await self._dispatch(cog, settings, message)
        reason = recorded[0][2]
        self.assertNotIn("bad", reason.casefold())
        self.assertNotIn("bad", dmed[0].casefold())

    async def test_a_clean_message_is_left_alone(self):
        cog, settings = self._cog()
        message, deleted, _ = self._message("a perfectly nice sentence")
        recorded, escalate = await self._dispatch(cog, settings, message)
        self.assertEqual([], deleted)
        self.assertEqual([], recorded)
        escalate.assert_not_awaited()

    async def test_staff_are_exempt_by_permission(self):
        """A filter that times out the moderators is worse than no filter."""
        cog, settings = self._cog()
        message, deleted, _ = self._message("bad", manage_messages=True)
        recorded, _ = await self._dispatch(cog, settings, message)
        self.assertEqual(([], []), (deleted, recorded))

    async def test_the_guild_owner_is_exempt(self):
        cog, settings = self._cog()
        message, deleted, _ = self._message("bad", author_id=1, owner_id=1)
        recorded, _ = await self._dispatch(cog, settings, message)
        self.assertEqual(([], []), (deleted, recorded))

    async def test_a_configured_exempt_role_is_honoured(self):
        cog, settings = self._cog(word_filter_exempt_roles=[42])
        message, deleted, _ = self._message("bad", roles=(42,))
        recorded, _ = await self._dispatch(cog, settings, message)
        self.assertEqual(([], []), (deleted, recorded))

    async def test_maintenance_outranks_the_flag(self):
        cog, settings = self._cog()
        message, deleted, _ = self._message("bad")
        recorded, _ = await self._dispatch(cog, settings, message, maintenance=True)
        self.assertEqual(([], []), (deleted, recorded))

    async def test_a_disabled_feature_does_nothing(self):
        cog, settings = self._cog()
        message, deleted, _ = self._message("bad")
        recorded, _ = await self._dispatch(cog, settings, message, enabled=False)
        self.assertEqual(([], []), (deleted, recorded))

    async def test_an_empty_word_list_does_nothing(self):
        cog, settings = self._cog(word_filter_words=[])
        message, deleted, _ = self._message("bad")
        recorded, _ = await self._dispatch(cog, settings, message)
        self.assertEqual(([], []), (deleted, recorded))

    async def test_a_punctuation_only_entry_does_not_match_everything(self):
        cog, settings = self._cog(word_filter_words=["..."])
        message, deleted, _ = self._message("a harmless sentence")
        recorded, _ = await self._dispatch(cog, settings, message)
        self.assertEqual(([], []), (deleted, recorded))

    async def test_deletion_can_be_turned_off_while_the_warning_stays(self):
        cog, settings = self._cog(word_filter_delete_message=False)
        message, deleted, _ = self._message("bad")
        recorded, _ = await self._dispatch(cog, settings, message)
        self.assertEqual([], deleted)
        self.assertEqual(1, len(recorded))

    async def test_configuration_is_read_once_and_reused(self):
        """on_message runs per message; four setting reads each is not an option."""
        cog, settings = self._cog()
        import cogs.moderation as moderation
        reader = AsyncMock(return_value=settings)
        with patch.object(moderation, "guild_settings_many", reader), \
             patch.object(moderation, "is_enabled", lambda gid, key: True), \
             patch.object(moderation, "maintenance_blocks", lambda g, u: False), \
             patch.object(moderation.database, "run", AsyncMock(
                 return_value={"warning_id": 1, "total": 1, "tag_count": 1})), \
             patch.object(moderation, "apply_warn_escalation", AsyncMock()):
            for _ in range(5):
                message, _, _ = self._message("a clean message")
                await cog.on_message(message)
        self.assertEqual(1, reader.await_count,
                         "the filter configuration must be cached per guild")


if __name__ == "__main__":
    unittest.main()
