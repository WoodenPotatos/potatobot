"""Member announcements: guild templates, and telling a leave from a kick from a ban.

The join, leave and boost messages were fixed text, a ban posted *two* messages
(a farewell and a ban notice) because a ban is also a member removal, a kick read
exactly like a leave, and the boost announcement disappeared whenever the economy
was off or the reward had already been claimed. These pin what replaced that.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord

from core import announcement_templates as templates
from core import settings_cache
from core.settings_registry import (FEATURE_DEFINITIONS, SETTING_DEFINITIONS,
                                    validate_setting_value)

GUILD_ID = 123
MEMBER_ID = 9
BOT_ID = 1
MODERATOR_ID = 77


def _announcements():
    # Resolved at call time: the cog-loading test unloads cogs and a
    # module-level import would hold a stale copy (CLAUDE.md, Testing).
    import cogs.announcements
    return cogs.announcements


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeAvatar:
    """Stands in for `discord.Asset`: the bytes come from memory, never a CDN."""

    url = "https://cdn.discordapp.com/a.png"

    def __init__(self, data=b"png-bytes", error=None):
        self.data, self.error = data, error

    def replace(self, **kwargs):
        return self

    async def read(self):
        if self.error is not None:
            raise self.error
        return self.data


def make_member(guild, member_id=MEMBER_ID, name="tester"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=member_id, bot=False, guild=guild, name=name, display_name=name,
        mention=f"<@{member_id}>", created_at=now - timedelta(days=400),
        joined_at=now - timedelta(days=30),
        display_avatar=FakeAvatar(),
        premium_since=None,
    )


def make_guild(*, audit_entries=(), view_audit_log=True, members=None):
    channels = {10: FakeChannel(10), 11: FakeChannel(11), 12: FakeChannel(12),
                13: FakeChannel(13), 14: FakeChannel(14), 15: FakeChannel(15)}

    async def audit_logs(action=None, limit=None):
        for entry in audit_entries:
            if action is None or entry.action is action:
                yield entry

    guild = SimpleNamespace(
        id=GUILD_ID, name="Test Guild", member_count=42, icon=None,
        premium_subscription_count=3, premium_tier=1,
        channels=channels,
        get_channel=channels.get,
        get_member=lambda member_id: (members or {}).get(member_id),
        audit_logs=audit_logs,
        me=SimpleNamespace(guild_permissions=SimpleNamespace(
            view_audit_log=view_audit_log)),
    )
    return guild


def audit_entry(kind, target_id, *, moderator_id=MODERATOR_ID, reason="spam",
                age_seconds=1):
    return SimpleNamespace(
        action=(discord.AuditLogAction.kick if kind == "kick"
                else discord.AuditLogAction.ban),
        target=SimpleNamespace(id=target_id), user_id=moderator_id,
        reason=reason,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


class TemplateRenderingTests(unittest.TestCase):
    def render(self, stored, values=None, **kwargs):
        return templates.render_announcement(
            "join", stored, values or {"user": "<@9>"},
            shipped_title="Shipped title", shipped_description="Shipped text",
            **kwargs)

    def test_an_attribute_walk_stays_literal(self):
        """`str.format` would have resolved this; the whitelist never does."""
        self.assertEqual("{user.__class__} {unknown}",
                         templates.substitute("{user.__class__} {unknown}",
                                              {"user": "<@9>"}))

    def test_a_substituted_value_is_not_expanded_again(self):
        """A member named `{reason}` must not pull the reason into the text."""
        self.assertEqual("{reason} / spam", templates.substitute(
            "{user_name} / {reason}", {"user_name": "{reason}", "reason": "spam"}))

    def test_newline_is_a_line_break(self):
        self.assertEqual("a\nb", templates.substitute("a{newline}b", {}))

    def test_empty_and_blank_fields_fall_back_to_the_shipped_text(self):
        for stored in ({}, {"title": "", "texts": []},
                       {"title": "   ", "texts": ["", "  "]}):
            with self.subTest(stored=stored):
                embed = self.render(stored).embed
                self.assertEqual("Shipped title", embed.title)
                self.assertEqual("Shipped text", embed.description)

    def test_a_set_field_overrides_and_fills_placeholders(self):
        embed = self.render({"title": "Hi {user}", "texts": ["Welcome {user}"]}).embed
        self.assertEqual("Hi <@9>", embed.title)
        self.assertEqual("Welcome <@9>", embed.description)

    def test_one_variant_is_chosen_from_the_non_blank_lines(self):
        seen = []

        def choose(options):
            seen.extend(options)
            return options[-1]

        embed = self.render({"texts": ["one", "", "two"]}, choose=choose).embed
        self.assertEqual(["one", "two"], seen)
        self.assertEqual("two", embed.description)

    def test_colour_falls_back_unless_it_is_hex(self):
        self.assertEqual(templates.DEFAULT_COLORS["join"],
                         self.render({"color": "red"}).embed.colour.value)
        self.assertEqual(0x5865F2,
                         self.render({"color": "#5865F2"}).embed.colour.value)

    def test_expanded_text_is_clamped_to_discords_limits(self):
        values = {"user": "x" * 300}
        embed = self.render({"title": "{user}"}, values).embed
        self.assertEqual(templates.TITLE_LIMIT, len(embed.title))

    def test_thumbnail_follows_its_choice(self):
        member = SimpleNamespace(display_avatar=SimpleNamespace(url="https://a/b.png"))
        guild = SimpleNamespace(icon=SimpleNamespace(url="https://a/icon.png"))
        def thumb(choice):
            embed = self.render({"thumbnail": choice}, member=member, guild=guild).embed
            return embed.thumbnail.url
        self.assertEqual("https://a/b.png", thumb("avatar"))
        self.assertEqual("https://a/icon.png", thumb("server"))
        self.assertIsNone(thumb("none"))

    def test_content_is_none_when_unset(self):
        self.assertIsNone(self.render({}).content)
        self.assertEqual("Hello <@9>", self.render({"content": "Hello {user}"}).content)

    def test_names_are_escaped_for_markdown_and_mentions(self):
        guild = SimpleNamespace(name="**Guild**", member_count=5)
        member = make_member(guild, name="@everyone **bold**")
        values = templates.placeholder_values(member, guild)
        self.assertNotIn("@everyone", values["user_name"])
        self.assertIn("\\*\\*bold\\*\\*", values["user_name"])
        self.assertIn("\\*\\*Guild\\*\\*", values["server"])

    def test_the_id_is_written_as_text(self):
        """A mention reads as a bare id once the member has left; the name and
        id written as text do not."""
        guild = SimpleNamespace(name="Guild", member_count=5)
        values = templates.placeholder_values(make_member(guild), guild)
        self.assertEqual(str(MEMBER_ID), values["user_id"])
        self.assertIn("user_id", templates.COMMON_PLACEHOLDERS)

    def test_the_shipped_removal_texts_name_the_member_without_a_mention(self):
        import json
        for language in ("hu", "en"):
            catalog = json.loads((ROOT / "locales" / f"{language}.json")
                                 .read_text(encoding="utf-8"))["announcements"]
            for event in ("leave", "kick", "ban"):
                with self.subTest(language=language, event=event):
                    text = catalog[f"{event}_desc"]
                    for name in ("{display_name}", "{user_name}", "{user_id}"):
                        self.assertIn(name, text)

    def test_only_the_member_can_be_pinged(self):
        member = SimpleNamespace(id=MEMBER_ID)
        mentions = templates.announcement_mentions(member)
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual([member], mentions.users)

    def test_every_event_lists_placeholders_its_shipped_text_can_fill(self):
        """A shipped text naming a placeholder its event cannot fill would show
        the literal braces, because `t()` returns the unformatted template."""
        import json
        import re
        for language in ("hu", "en"):
            catalog = json.loads((ROOT / "locales" / f"{language}.json")
                                 .read_text(encoding="utf-8"))["announcements"]
            for event in templates.ANNOUNCEMENT_EVENTS:
                allowed = set(templates.EVENT_PLACEHOLDERS[event]) | {"coin"}
                for part in ("title", "desc"):
                    with self.subTest(language=language, event=event, part=part):
                        used = set(re.findall(r"\{([a-z_]+)\}",
                                              catalog[f"{event}_{part}"]))
                        self.assertLessEqual(used, allowed)


class ModeratorHandoffTests(unittest.TestCase):
    def test_a_recorded_moderator_is_taken_once(self):
        templates.record_moderator_action(GUILD_ID, MEMBER_ID, "kick", MODERATOR_ID)
        self.assertEqual(MODERATOR_ID,
                         templates.take_moderator_action(GUILD_ID, MEMBER_ID, "kick"))
        self.assertIsNone(templates.take_moderator_action(GUILD_ID, MEMBER_ID, "kick"))

    def test_a_stale_handoff_is_ignored(self):
        templates.record_moderator_action(GUILD_ID, MEMBER_ID, "ban", MODERATOR_ID)
        later = time.monotonic() + templates.MODERATOR_HANDOFF_SECONDS + 1
        with patch.object(templates.time, "monotonic", return_value=later):
            self.assertIsNone(templates.take_moderator_action(GUILD_ID, MEMBER_ID, "ban"))

    def test_the_commands_record_before_they_act(self):
        """Recorded after the call, the removal could settle first and credit
        the bot."""
        source = (ROOT / "cogs" / "moderation.py").read_text(encoding="utf-8")
        for kind, call in (("kick", "await member.kick("), ("ban", "await member.ban(")):
            with self.subTest(kind=kind):
                record = source.index(f'record_moderator_action(ctx.guild.id, member.id, "{kind}"')
                self.assertLess(record, source.index(call, record))


class RegistryTests(unittest.TestCase):
    def test_every_event_has_its_template_settings_with_empty_defaults(self):
        for event in templates.ANNOUNCEMENT_EVENTS:
            for field in templates.TEMPLATE_FIELDS:
                key = templates.template_setting_key(event, field)
                with self.subTest(key=key):
                    definition = SETTING_DEFINITIONS[key]
                    self.assertEqual(templates.EVENT_FEATURES[event],
                                     definition.owner_feature)
                    self.assertEqual("member_announcements", definition.category)
                    if field != "thumbnail":
                        # Empty is "the shipped text", so an upgrade changes
                        # nothing until somebody edits it.
                        self.assertIn(definition.default, ("", []))
            self.assertIn(templates.EVENT_CHANNEL_SETTINGS[event], SETTING_DEFINITIONS)

    def test_variants_are_five_multi_line_boxes(self):
        """One box split on newlines could not hold a variant with a line
        break in it; each is its own box now, and five at most."""
        for event in templates.ANNOUNCEMENT_EVENTS:
            texts = SETTING_DEFINITIONS[templates.template_setting_key(event, "texts")]
            content = SETTING_DEFINITIONS[templates.template_setting_key(event, "content")]
            with self.subTest(event=event):
                self.assertTrue(texts.multiline)
                self.assertTrue(content.multiline)
                self.assertEqual(5, texts.max_items)
        definition = SETTING_DEFINITIONS["join_message_texts"]
        self.assertEqual(["a\nb"], validate_setting_value(definition, ["a\nb"]))
        with self.assertRaises(ValueError):
            validate_setting_value(definition, ["x"] * 6)

    def test_each_event_toggle_is_a_child_of_the_master(self):
        for event, feature in templates.EVENT_FEATURES.items():
            with self.subTest(feature=feature):
                definition = FEATURE_DEFINITIONS[feature]
                self.assertEqual("member_announcements", definition.parent)
                self.assertIn("member_announcements", definition.dependencies)
        self.assertIn("economy", FEATURE_DEFINITIONS["announce_boost_reward"].dependencies)
        for feature in ("announce_kick", "announce_ban"):
            self.assertIn("view_audit_log",
                          FEATURE_DEFINITIONS[feature].required_discord_permissions)

    def test_colour_and_image_are_refused_in_a_shape_discord_would_reject(self):
        color = SETTING_DEFINITIONS["join_message_color"]
        image = SETTING_DEFINITIONS["join_message_image_url"]
        for definition, good in ((color, "#5865f2"), (image, "https://x.test/a.gif")):
            self.assertEqual(good, validate_setting_value(definition, good))
            self.assertEqual("", validate_setting_value(definition, ""))
        for definition, bad in ((color, "red"), (color, "#12345"),
                                (image, "http://x.test/a.gif"),
                                (image, "https://"), (image, "https://a b")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_setting_value(definition, bad)


class AnnouncementCogTestCase(unittest.IsolatedAsyncioTestCase):
    """Drives the cog with every event enabled unless a test says otherwise."""

    settings = {}
    disabled = frozenset()

    def setUp(self):
        settings_cache.invalidate()
        self.module = _announcements()
        self.values = {"join_channel": 10, "leave_channel": 11,
                       "kick_channel": 12, "ban_channel": 13,
                       "booster_channel": 14, "boost_reward_channel": None,
                       "boost_reward_amount": 500}
        self.values.update(self.settings)
        self.mod_log = []

        def setting(guild_id, key):
            if key in self.values:
                return self.values[key]
            return SETTING_DEFINITIONS[key].default

        self.mod_log_files = []

        async def send_moderation_log(guild, embed, what="", skip_channel=None,
                                      **kwargs):
            self.mod_log.append(embed)
            if kwargs.get("file") is not None:
                self.mod_log_files.append(kwargs["file"])
            return True

        self.patches = [
            patch.object(self.module, "guild_setting_sync", setting),
            patch.object(self.module, "is_enabled",
                         lambda guild_id, key: key not in self.disabled),
            patch.object(self.module, "send_moderation_log", send_moderation_log),
            patch.object(self.module, "REMOVAL_SETTLE_SECONDS", 0),
        ]
        for active in self.patches:
            active.start()
        self.cog = self.module.Announcements(SimpleNamespace(user=SimpleNamespace(id=BOT_ID)))

    def tearDown(self):
        for active in self.patches:
            active.stop()
        settings_cache.invalidate()

    async def settle(self):
        await asyncio.gather(*list(self.cog._tasks))

    def titles(self, guild):
        return {channel_id: [sent["embed"].title for sent in channel.sent]
                for channel_id, channel in guild.channels.items() if channel.sent}

    def t(self, key):
        from cogs.utils import t
        return t(key)


class RemovalClassificationTests(AnnouncementCogTestCase):
    async def test_a_plain_removal_is_a_leave(self):
        guild = make_guild()
        await self.cog.on_member_remove(make_member(guild))
        await self.settle()
        self.assertEqual({11: [self.t("announcements.leave_title")]}, self.titles(guild))

    async def test_a_ban_posts_once_and_never_as_a_leave(self):
        """It posted a farewell *and* a ban notice, whichever event came first."""
        for order in ("remove_first", "ban_first"):
            with self.subTest(order=order):
                guild = make_guild(audit_entries=[audit_entry("ban", MEMBER_ID)])
                member = make_member(guild)
                if order == "remove_first":
                    await self.cog.on_member_remove(member)
                    await self.cog.on_member_ban(guild, member)
                else:
                    await self.cog.on_member_ban(guild, member)
                    await self.cog.on_member_remove(member)
                await self.settle()
                self.assertEqual({13: [self.t("announcements.ban_title")]},
                                 self.titles(guild))

    async def test_a_kick_is_told_apart_by_the_audit_log(self):
        guild = make_guild(audit_entries=[audit_entry("kick", MEMBER_ID)])
        await self.cog.on_member_remove(make_member(guild))
        await self.settle()
        self.assertEqual({12: [self.t("announcements.kick_title")]}, self.titles(guild))

    async def test_an_old_kick_of_the_same_member_is_not_this_removal(self):
        guild = make_guild(audit_entries=[
            audit_entry("kick", MEMBER_ID, age_seconds=3600)])
        await self.cog.on_member_remove(make_member(guild))
        await self.settle()
        self.assertEqual({11: [self.t("announcements.leave_title")]}, self.titles(guild))

    async def test_a_kick_of_someone_else_is_not_this_removal(self):
        guild = make_guild(audit_entries=[audit_entry("kick", 555)])
        await self.cog.on_member_remove(make_member(guild))
        await self.settle()
        self.assertEqual([11], list(self.titles(guild)))

    async def test_without_the_audit_log_a_removal_reads_as_a_leave(self):
        guild = make_guild(audit_entries=[audit_entry("kick", MEMBER_ID)],
                           view_audit_log=False)
        await self.cog.on_member_remove(make_member(guild))
        await self.settle()
        self.assertEqual([11], list(self.titles(guild)))

    async def test_the_audit_event_is_used_before_reading_the_log(self):
        guild = make_guild(view_audit_log=False)
        entry = audit_entry("kick", MEMBER_ID)
        entry.guild = guild
        await self.cog.on_audit_log_entry_create(entry)
        await self.cog.on_member_remove(make_member(guild))
        await self.settle()
        self.assertEqual([12], list(self.titles(guild)))

    async def test_our_own_kick_names_the_moderator_not_the_bot(self):
        moderator = SimpleNamespace(id=MODERATOR_ID, mention=f"<@{MODERATOR_ID}>")
        guild = make_guild(audit_entries=[audit_entry("kick", MEMBER_ID,
                                                      moderator_id=BOT_ID)],
                           members={MODERATOR_ID: moderator})
        templates.record_moderator_action(GUILD_ID, MEMBER_ID, "kick", MODERATOR_ID)
        await self.cog.on_member_remove(make_member(guild))
        await self.settle()
        description = guild.channels[12].sent[0]["embed"].description
        self.assertIn(f"<@{MODERATOR_ID}>", description)

    async def test_a_ban_outside_the_guild_is_still_announced(self):
        """A ban by id has no member removal at all."""
        guild = make_guild()
        await self.cog.on_member_ban(guild, make_member(guild))
        await self.settle()
        self.assertEqual([13], list(self.titles(guild)))


class AvatarAttachmentTests(AnnouncementCogTestCase):
    async def test_the_avatar_is_attached_not_linked(self):
        """A linked avatar vanished once the member changed it."""
        guild = make_guild()
        await self.cog.on_member_join(make_member(guild))
        sent = guild.channels[10].sent[0]
        self.assertEqual("attachment://avatar.png", sent["embed"].thumbnail.url)
        self.assertEqual("avatar.png", sent["file"].filename)
        self.assertEqual(b"png-bytes", sent["file"].fp.read())

    async def test_a_failed_download_falls_back_to_the_link(self):
        guild = make_guild()
        member = make_member(guild)
        member.display_avatar = FakeAvatar(error=asyncio.TimeoutError())
        await self.cog.on_member_join(member)
        sent = guild.channels[10].sent[0]
        self.assertNotIn("file", sent)
        self.assertEqual(FakeAvatar.url, sent["embed"].thumbnail.url)

    async def test_no_thumbnail_means_no_download(self):
        self.values["join_message_thumbnail"] = "none"
        guild = make_guild()
        member = make_member(guild)
        member.display_avatar = FakeAvatar(error=AssertionError("downloaded"))
        await self.cog.on_member_join(member)
        self.assertNotIn("file", guild.channels[10].sent[0])

    async def test_nothing_is_written_to_disk(self):
        source = (ROOT / "cogs" / "announcements.py").read_text(encoding="utf-8")
        self.assertIn("io.BytesIO(data)", source)
        for writer in ("open(", "write_bytes", "NamedTemporaryFile", ".save("):
            self.assertNotIn(writer, source)


class RemovalRoutingTests(AnnouncementCogTestCase):
    settings = {"ban_channel": None}

    async def test_a_ban_with_no_channel_goes_to_the_moderation_log(self):
        guild = make_guild()
        await self.cog.on_member_ban(guild, make_member(guild))
        await self.settle()
        self.assertEqual({}, self.titles(guild))
        self.assertEqual([self.t("announcements.ban_title")],
                         [embed.title for embed in self.mod_log])
        # The attachment travels to the log too.
        self.assertEqual("avatar.png", self.mod_log_files[0].filename)


class DisabledBanTests(AnnouncementCogTestCase):
    disabled = frozenset({"announce_ban"})

    async def test_a_switched_off_ban_posts_nothing_rather_than_a_leave(self):
        guild = make_guild()
        member = make_member(guild)
        await self.cog.on_member_remove(member)
        await self.cog.on_member_ban(guild, member)
        await self.settle()
        self.assertEqual({}, self.titles(guild))


class MasterOffTests(AnnouncementCogTestCase):
    disabled = frozenset({"member_announcements"})

    async def test_the_master_switch_silences_every_event(self):
        guild = make_guild()
        member = make_member(guild)
        await self.cog.on_member_join(member)
        await self.cog.on_member_remove(member)
        await self.settle()
        self.assertEqual({}, self.titles(guild))


class JoinTests(AnnouncementCogTestCase):
    settings = {"join_message_title": "Hello {display_name}",
                "join_message_content": "{user} @everyone"}

    async def test_a_guild_template_is_posted_with_only_the_member_pingable(self):
        guild = make_guild()
        await self.cog.on_member_join(make_member(guild))
        sent = guild.channels[10].sent[0]
        self.assertEqual("Hello tester", sent["embed"].title)
        self.assertEqual("<@9> @everyone", sent["content"])
        self.assertFalse(sent["allowed_mentions"].everyone)


class BoostTests(AnnouncementCogTestCase):
    disabled = frozenset({"economy"})

    def boost_message(self, member):
        return SimpleNamespace(type=discord.MessageType.premium_guild_subscription,
                               guild=member.guild, author=member)

    async def test_a_boost_is_announced_with_the_economy_off(self):
        """It used to vanish whenever the economy was off."""
        guild = make_guild()
        # A real `discord.Member` as far as `isinstance` is concerned.
        member = MagicMock(spec=discord.Member)
        for name, value in vars(make_member(guild)).items():
            setattr(member, name, value)
        await self.cog.on_message(self.boost_message(member))
        self.assertEqual({14: [self.t("announcements.boost_title")]}, self.titles(guild))

    async def test_one_boost_seen_twice_is_announced_once(self):
        guild = make_guild()
        member = make_member(guild)
        before = SimpleNamespace(premium_since=None)
        after = make_member(guild)
        after.premium_since = datetime.now(timezone.utc)
        await self.cog.on_member_update(before, after)
        await self.cog._boosted(member)
        self.assertEqual(1, len(guild.channels[14].sent))

    async def test_an_ordinary_message_is_ignored_cheaply(self):
        message = SimpleNamespace(type=discord.MessageType.default, guild=None,
                                  author=None)
        await self.cog.on_message(message)


class BoostRewardTests(AnnouncementCogTestCase):
    async def test_the_reward_is_paid_once_and_announced_with_the_boost(self):
        guild = make_guild()
        member = make_member(guild)
        claim = AsyncMock(return_value={"stats": None})
        with (patch.object(self.module.database, "run_write", claim),
              patch.object(self.module, "apply_database_result", AsyncMock())):
            await self.cog._boosted(member)
        args = claim.await_args.args
        self.assertEqual((GUILD_ID, MEMBER_ID, "server_boost", 500, 30), args[1:])
        # The reward has no channel of its own here, so it follows the boost.
        self.assertEqual([self.t("announcements.boost_title"),
                          self.t("announcements.boost_reward_title")],
                         self.titles(guild)[14])

    async def test_an_already_claimed_reward_is_not_announced(self):
        guild = make_guild()
        with (patch.object(self.module.database, "run_write",
                           AsyncMock(return_value=None)),
              patch.object(self.module, "apply_database_result", AsyncMock())):
            await self.cog._boosted(make_member(guild))
        self.assertEqual([self.t("announcements.boost_title")], self.titles(guild)[14])


class ZeroRewardTests(AnnouncementCogTestCase):
    settings = {"boost_reward_amount": 0}

    async def test_a_zero_amount_claims_nothing(self):
        claim = AsyncMock()
        with patch.object(self.module.database, "run_write", claim):
            await self.cog._boosted(make_member(make_guild()))
        claim.assert_not_awaited()


class TestSendTests(AnnouncementCogTestCase):
    async def test_a_test_posts_the_saved_template_marked_as_a_test(self):
        guild = make_guild()
        actor = make_member(guild, member_id=MODERATOR_ID)
        self.assertIsNone(await self.module.send_test_announcement(guild, actor, "kick"))
        embed = guild.channels[12].sent[0]["embed"]
        self.assertEqual(self.t("announcements.test_footer"), embed.footer.text)

    async def test_the_worker_refuses_a_switched_off_event(self):
        from core import control_actions
        self.disabled = frozenset({"announce_kick"})
        guild = make_guild()
        actor = make_member(guild, member_id=MODERATOR_ID)
        code = await control_actions.execute_test_announcement(
            guild, actor, {"payload": {"event": "kick"}})
        self.assertEqual("feature_disabled_or_unsupported", code)
        self.assertEqual({}, self.titles(guild))

    async def test_the_worker_refuses_an_unknown_event(self):
        from core import control_actions
        guild = make_guild()
        code = await control_actions.execute_test_announcement(
            guild, make_member(guild), {"payload": {"event": "nope"}})
        self.assertEqual("feature_disabled_or_unsupported", code)


class TestSendRouteTests(unittest.TestCase):
    def setUp(self):
        from core import database
        import dashboard_api
        self.database, self.api = database, dashboard_api
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "dashboard.db")
        database.initialize_database()
        database.register_guild(GUILD_ID, "Test Guild")
        dashboard_api.app.config.update(TESTING=True)
        self.client = dashboard_api.app.test_client()
        self.original_admin_id = dashboard_api.ADMIN_ID
        dashboard_api.ADMIN_ID = "42"
        dashboard_api._rate_limit_events.clear()
        with self.client.session_transaction() as session:
            session.update({
                "logged_in": True, "user_id": "42",
                "display": {"username": "tester", "avatar": None},
                "csrf_token": "csrf-token", "server_session_id": "server-session",
                "authorized_guild_ids": [str(GUILD_ID)],
                "authenticated_at": time.time(),
            })
        self.headers = {"X-CSRF-Token": "csrf-token"}

    def tearDown(self):
        self.api.ADMIN_ID = self.original_admin_id
        self.database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_a_known_event_is_queued(self):
        response = self.client.post(f"/api/guilds/{GUILD_ID}/announcements/ban/test",
                                    json={}, headers=self.headers)
        self.assertEqual(202, response.status_code)
        action = self.database.claim_control_action()
        self.assertEqual("test_announcement", action["action_type"])
        self.assertEqual({"event": "ban"}, action["payload"])

    def test_an_unknown_event_is_refused_before_the_queue(self):
        response = self.client.post(f"/api/guilds/{GUILD_ID}/announcements/raid/test",
                                    json={}, headers=self.headers)
        self.assertEqual(400, response.status_code)
        self.assertIsNone(self.database.claim_control_action())

    def test_the_registry_carries_its_declared_order(self):
        """Flask sorts this payload's keys, so without `order` every settings
        page rendered alphabetically by key."""
        from core.settings_registry import SETTING_DEFINITIONS as declared
        data = self.client.get("/api/settings/registry").get_json()["data"]
        by_order = sorted(data, key=lambda key: data[key]["order"])
        self.assertEqual([key for key in declared if key in data], by_order)
        self.assertLess(data["booster_channel"]["order"],
                        data["boost_message_title"]["order"])

    def test_the_registry_hands_the_client_each_events_placeholders(self):
        data = self.client.get("/api/settings/registry").get_json()
        self.assertEqual(list(templates.EVENT_PLACEHOLDERS["kick"]),
                         data["announcement_placeholders"]["kick"])


class AnnouncementToolsHarnessTests(unittest.TestCase):
    """The dashboard's preview, placeholder list and Send test, through jsdom.

    The fixture is the real registry's declarations and the real feature-state
    payload, so the harness cannot pass against a shape the server never sends.
    """

    def test_the_editor_previews_and_refuses_to_test_unsaved_edits(self):
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        from core import database
        import dashboard_api
        with tempfile.TemporaryDirectory() as temp_dir:
            original_path, original_admin = database.DB_PATH, dashboard_api.ADMIN_ID
            database.DB_PATH = os.path.join(temp_dir, "fixture.db")
            dashboard_api.ADMIN_ID = "42"
            settings_cache.invalidate()
            try:
                # Every payload comes from the real route, so the stub cannot
                # drift from what the server sends.
                database.initialize_database()
                database.register_guild(1, "Test Guild")
                dashboard_api.app.config.update(TESTING=True)
                dashboard_api._rate_limit_events.clear()
                client = dashboard_api.app.test_client()
                with client.session_transaction() as session:
                    session.update({
                        "logged_in": True, "user_id": "42",
                        "display": {"username": "tester", "avatar": None},
                        "csrf_token": "csrf-token",
                        "server_session_id": "server-session",
                        "authorized_guild_ids": ["1"],
                        "authenticated_at": time.time(),
                    })
                changes = [{"key": key, "value": value, "revision": 0}
                           for key, value in (
                               ("join_message_title", "Hi {user}"),
                               ("join_message_texts", ["Line one", "Line two"]),
                               ("join_message_color", "#123456"))]
                saved = client.patch("/api/guilds/1/settings", json={"changes": changes},
                                     headers={"X-CSRF-Token": "csrf-token"})
                self.assertEqual(200, saved.status_code, saved.get_json())
                registry = client.get("/api/settings/registry").get_json()
                locale = client.get("/api/locale?lang=en").get_json()
                fixture = {
                    "serverTexts": locale["server"]["texts"],
                    "registry": registry["data"],
                    "features": client.get("/api/guilds/1/features").get_json()["data"],
                    "settings": client.get("/api/guilds/1/settings").get_json()["data"],
                    "placeholders": registry["announcement_placeholders"],
                }
            finally:
                database.DB_PATH = original_path
                dashboard_api.ADMIN_ID = original_admin
                settings_cache.invalidate()
            fixture_path = os.path.join(temp_dir, "fixture.json")
            with open(fixture_path, "w", encoding="utf-8") as handle:
                json.dump(fixture, handle)
            script = ROOT / "tests" / "js" / "announcement_tools.js"
            result = subprocess.run([node, str(script), str(ROOT), fixture_path],
                                    capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, f"{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
