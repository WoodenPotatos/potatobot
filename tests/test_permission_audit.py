"""The permission diagnostic shared by `/checkperms` and the dashboard.

`build_report` is deliberately a pure function of a guild plus two maps, which is
what lets these tests drive it with stand-ins instead of a live Discord guild.
"""

import json
import unittest
from pathlib import Path

import discord

import permission_audit
from settings_registry import FEATURE_DEFINITIONS

ROOT = Path(__file__).resolve().parents[1]


def permissions(**granted) -> discord.Permissions:
    return discord.Permissions(**granted)


class FakeRole:
    def __init__(self, role_id, name, position, managed=False, default=False):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position


class FakeChannel:
    def __init__(self, channel_id, name, channel_type, effective,
                 member_effective=None):
        self.id = channel_id
        self.name = name
        self.type = channel_type
        self._effective = effective
        # What `@everyone` resolves to here. Defaults to the bot's own set so a
        # test that only cares about the bot stays as short as it was.
        self._member_effective = (effective if member_effective is None
                                  else member_effective)

    def permissions_for(self, member):
        # Keyed by role name so a test can give each role its own answer, which
        # is what the gated-channel rule needs to distinguish.
        by_role = getattr(self, "_by_role", None) or {}
        name = getattr(member, "name", None)
        if name in by_role:
            return by_role[name]
        if getattr(member, "is_default", lambda: False)():
            return self._member_effective
        return self._effective

    def for_roles(self, mapping):
        self._by_role = mapping
        return self


class FakeMember:
    def __init__(self, guild_permissions, top_role):
        self.guild_permissions = guild_permissions
        self.top_role = top_role


class FakeGuild:
    def __init__(self, me, channels=(), roles=()):
        self.me = me
        self._channels = {channel.id: channel for channel in channels}
        self._roles = {role.id: role for role in roles}
        # Every guild has one; the member half of the audit resolves against it.
        self.default_role = next(
            (role for role in roles if role.is_default()),
            FakeRole(1, "@everyone", 0, default=True),
        )
        self.roles = list(roles) or [self.default_role]

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


def report_for(guild, features=None, settings=None):
    return permission_audit.build_report(
        guild, features or {}, settings or {}
    )


def codes(report):
    return sorted(finding.code for finding in report.findings)


class PermissionAuditTests(unittest.TestCase):
    def setUp(self):
        self.bot_role = FakeRole(1, "PotatoBot", 10)

    def test_a_disabled_feature_is_reported_as_state_not_as_a_problem(self):
        """Otherwise every installation opens on a wall of findings for features
        it deliberately does not run."""
        guild = FakeGuild(FakeMember(permissions(), self.bot_role))
        report = report_for(guild, {key: {"enabled": False}
                                    for key in FEATURE_DEFINITIONS})
        self.assertEqual([], report.blocking)
        moderation = next(entry for entry in report.features
                          if entry["key"] == "moderation")
        self.assertFalse(moderation["enabled"])
        self.assertIn("ban_members", moderation["missing"])

    def test_an_enabled_feature_missing_a_permission_blocks(self):
        guild = FakeGuild(FakeMember(permissions(), self.bot_role))
        report = report_for(guild, {"moderation": {"enabled": True}})
        blocking = [finding for finding in report.blocking
                    if finding.feature == "moderation"]
        self.assertEqual(1, len(blocking))
        self.assertIn("ban_members", blocking[0].permissions)

    def test_a_channel_overwrite_is_caught_where_guild_permissions_pass(self):
        """This is the case the old check could not see: `send_messages` held
        guild-wide, denied on the one channel the setting names."""
        granted = permissions(
            view_channel=True, send_messages=True, embed_links=True,
            read_message_history=True,
        )
        denied = permissions(view_channel=True, embed_links=True)
        guild = FakeGuild(
            FakeMember(granted, self.bot_role),
            channels=[FakeChannel(500, "bot-log", "text", denied)],
        )
        report = report_for(
            guild, {"general": {"enabled": True}}, {"bot_log_channel": 500}
        )
        finding = next(item for item in report.findings
                       if item.code == "channel_missing_permission")
        self.assertEqual("bot-log", finding.identifier)
        self.assertEqual(("send_messages",), finding.permissions)
        self.assertEqual(permission_audit.SEVERITY_BLOCKING, finding.severity)

    def test_a_deleted_channel_is_a_warning_rather_than_a_block(self):
        guild = FakeGuild(FakeMember(permissions(), self.bot_role))
        report = report_for(guild, {}, {"bot_log_channel": 404})
        finding = next(item for item in report.findings
                       if item.code == "channel_missing")
        self.assertEqual(permission_audit.SEVERITY_DEGRADED, finding.severity)
        self.assertEqual("404", finding.identifier)

    def test_a_channel_owned_by_a_disabled_feature_is_not_checked(self):
        denied = permissions()
        guild = FakeGuild(
            FakeMember(permissions(), self.bot_role),
            channels=[FakeChannel(700, "tickets", "text", denied)],
        )
        report = report_for(
            guild, {"tickets": {"enabled": False}}, {"ticket_logs": 700}
        )
        self.assertNotIn("channel_missing_permission", codes(report))

    def test_a_voice_lobby_is_judged_by_what_it_actually_needs(self):
        """A lobby is not just a voice channel the bot posts in.

        `cogs/voicemod.py` creates the room, moves the member into it and sets
        per-member overwrites on it, so the setting declares those permissions
        itself. The blunt voice kind default — view and connect — would pass a
        guild where every room creation is about to fail.
        """
        guild = FakeGuild(
            FakeMember(permissions(connect=True), self.bot_role),
            channels=[FakeChannel(800, "Lobby", "voice",
                                  permissions(view_channel=True, connect=True))],
        )
        report = report_for(
            guild, {"temporary_voice": {"enabled": True}},
            {"temporary_voice_lobbies": [800]},
        )
        finding = next(item for item in report.findings
                       if item.code == "channel_missing_permission")
        self.assertEqual(
            ("manage_channels", "manage_roles", "move_members"),
            finding.permissions,
        )

    def test_a_setting_without_its_own_declaration_falls_back_to_the_kind(self):
        guild = FakeGuild(
            FakeMember(permissions(), self.bot_role),
            channels=[FakeChannel(801, "logs", "text", permissions(view_channel=True))],
        )
        report = report_for(guild, {"general": {"enabled": True}},
                            {"bot_log_channel": 801})
        finding = next(item for item in report.findings
                       if item.code == "channel_missing_permission")
        self.assertEqual(("send_messages", "embed_links"), finding.permissions)

    def test_a_member_permission_denied_by_overwrites_is_reported(self):
        """The half no diagnostic covered.

        The bot can post here perfectly well; the members the channel exists for
        cannot run a slash command in it, and only asking `@everyone` finds that.
        """
        everyone = FakeRole(1, "@everyone", 0, default=True)
        guild = FakeGuild(
            FakeMember(permissions(), self.bot_role),
            channels=[FakeChannel(
                802, "kaszino", "text",
                effective=permissions(view_channel=True, send_messages=True,
                                      embed_links=True),
                member_effective=permissions(view_channel=True, send_messages=True),
            )],
            roles=[everyone],
        )
        report = report_for(guild, {"economy": {"enabled": True}},
                            {"economy_channels": [802]})
        finding = next(item for item in report.findings
                       if item.code == "channel_member_missing_permission")
        self.assertEqual(("use_application_commands",), finding.permissions)
        # The bot is fine, so this must not be reported as blocking.
        self.assertEqual(permission_audit.SEVERITY_DEGRADED, finding.severity)
        self.assertNotIn("channel_missing_permission", codes(report))

    def test_the_member_check_resolves_against_the_configured_member_role(self):
        """An airlock guild denies `@everyone` on purpose.

        Measured against the private deployment, checking `@everyone` reported
        nine channels as broken and not one of them was: access is granted
        through the member role, which is what `permissions_for` resolves.
        """
        everyone = FakeRole(1, "@everyone", 0, default=True)
        member_role = FakeRole(2, "Tag", 5)
        channel = FakeChannel(
            900, "kaszino", "text",
            effective=permissions(view_channel=True, send_messages=True,
                                  embed_links=True),
        ).for_roles({
            "@everyone": permissions(),                       # denied by design
            "Tag": permissions(view_channel=True, send_messages=True,
                               use_application_commands=True),
        })
        guild = FakeGuild(FakeMember(permissions(), self.bot_role),
                          channels=[channel], roles=[everyone, member_role])
        report = report_for(guild, {"economy": {"enabled": True}},
                            {"economy_channels": [900], "member_role": 2})
        self.assertNotIn("channel_member_missing_permission", codes(report))

    def test_a_channel_gated_behind_an_opt_in_role_is_not_a_finding(self):
        """A per-game chat is invisible to the member role on purpose."""
        everyone = FakeRole(1, "@everyone", 0, default=True)
        member_role = FakeRole(2, "Tag", 5)
        game_role = FakeRole(3, "Valorant", 6)
        channel = FakeChannel(
            901, "game-chat", "text", effective=permissions(view_channel=True),
        ).for_roles({
            "@everyone": permissions(),
            "Tag": permissions(),                             # cannot see it
            "Valorant": permissions(view_channel=True),       # opt-in unlocks it
        })
        guild = FakeGuild(FakeMember(permissions(), self.bot_role),
                          channels=[channel],
                          roles=[everyone, member_role, game_role])
        report = report_for(guild, {"lfg": {"enabled": True}},
                            {"other_games_channel": 901, "member_role": 2})
        self.assertNotIn("channel_member_missing_permission", codes(report))

    def test_a_channel_no_role_can_see_is_a_finding(self):
        """The case an operator actually wants: announcements nobody receives."""
        everyone = FakeRole(1, "@everyone", 0, default=True)
        member_role = FakeRole(2, "Tag", 5)
        channel = FakeChannel(
            902, "joins", "text", effective=permissions(view_channel=True),
        ).for_roles({"@everyone": permissions(), "Tag": permissions()})
        guild = FakeGuild(FakeMember(permissions(), self.bot_role),
                          channels=[channel], roles=[everyone, member_role])
        report = report_for(guild, {"member_announcements": {"enabled": True}},
                            {"join_channel": 902, "member_role": 2})
        finding = next(item for item in report.findings
                       if item.code == "channel_member_missing_permission")
        self.assertEqual(("view_channel",), finding.permissions)

    def test_a_role_above_the_bot_is_reported_although_manage_roles_is_held(self):
        """No permission flag reports this; only the hierarchy does."""
        above = FakeRole(2, "Premium", 50)
        guild = FakeGuild(
            FakeMember(permissions(manage_roles=True), self.bot_role),
            roles=[above],
        )
        report = report_for(
            guild, {"shop": {"enabled": True}}, {"premium_role": 2}
        )
        finding = next(item for item in report.findings
                       if item.code == "role_above_bot")
        self.assertEqual("Premium", finding.identifier)
        self.assertEqual(permission_audit.SEVERITY_BLOCKING, finding.severity)

    def test_a_role_below_the_bot_with_manage_roles_is_clean(self):
        below = FakeRole(2, "Premium", 5)
        guild = FakeGuild(
            FakeMember(permissions(manage_roles=True), self.bot_role),
            roles=[below],
        )
        report = report_for(
            guild, {"shop": {"enabled": True}}, {"premium_role": 2}
        )
        self.assertNotIn("role_above_bot", codes(report))

    def test_an_integration_role_cannot_be_granted_by_anyone(self):
        managed = FakeRole(3, "Booster", 2, managed=True)
        guild = FakeGuild(
            FakeMember(permissions(manage_roles=True), self.bot_role),
            roles=[managed],
        )
        report = report_for(
            guild, {"onboarding": {"enabled": True}}, {"autoroles": [3]}
        )
        self.assertIn("role_unmanageable", codes(report))

    def test_administrator_is_reported_as_a_warning_not_a_pass(self):
        guild = FakeGuild(
            FakeMember(permissions(administrator=True), self.bot_role)
        )
        report = report_for(guild, {key: {"enabled": True}
                                    for key in FEATURE_DEFINITIONS})
        self.assertTrue(report.administrator)
        self.assertEqual([], report.blocking)
        self.assertIn("administrator_granted", codes(report))

    def test_missing_setting_rows_fall_back_to_registry_defaults(self):
        resolved = permission_audit.resolved_settings({})
        self.assertIsNone(resolved["bot_log_channel"])
        self.assertEqual([], resolved["autoroles"])
        stored = {"bot_log_channel": {"value": 12, "revision": 3}}
        self.assertEqual(12, permission_audit.resolved_settings(stored)["bot_log_channel"])


class PermissionFindingLocalizationTests(unittest.TestCase):
    """Both surfaces render a finding from its code, so an unlabeled code shows
    the operator a raw key on the one page meant to explain a failure."""

    CATALOG = json.loads((ROOT / "locales" / "hu.json").read_text(encoding="utf-8"))
    # Every code `permission_audit` can emit. Keep this in step with the module.
    CODES = (
        "feature_missing_permission", "channel_missing_permission",
        "channel_missing", "role_missing", "role_above_bot",
        "role_unmanageable", "administrator_granted",
    )

    def test_the_declared_codes_are_the_codes_the_module_emits(self):
        source = (ROOT / "permission_audit.py").read_text(encoding="utf-8")
        for code in self.CODES:
            with self.subTest(code=code):
                self.assertIn(f'code="{code}"', source)

    def test_every_code_has_bot_and_dashboard_text(self):
        admin = self.CATALOG["admin"]
        dashboard = self.CATALOG["dashboard"]
        for code in self.CODES:
            with self.subTest(code=code):
                self.assertTrue(admin.get(f"permissions_finding_{code}"))
                self.assertTrue(dashboard.get(f"permission_finding_{code}"))

    def test_every_severity_has_text_on_both_surfaces(self):
        for severity in (permission_audit.SEVERITY_BLOCKING,
                         permission_audit.SEVERITY_DEGRADED):
            with self.subTest(severity=severity):
                self.assertTrue(
                    self.CATALOG["admin"][f"permissions_severity_{severity}"]
                )
                self.assertTrue(
                    self.CATALOG["dashboard"][f"permissions_severity_{severity}"]
                )

    def test_every_permission_the_report_can_name_is_localized(self):
        used = {
            name
            for definition in FEATURE_DEFINITIONS.values()
            for name in definition.required_discord_permissions
        }
        used |= {
            name
            for required in permission_audit.CHANNEL_KIND_REQUIREMENTS.values()
            for name in required
        }
        for catalog in ("admin", "dashboard"):
            names = (self.CATALOG[catalog]["permission_names"]
                     if catalog == "dashboard"
                     else self.CATALOG["admin"]["permission_names"])
            with self.subTest(catalog=catalog):
                self.assertEqual([], sorted(used - set(names)))

    def test_every_channel_kind_the_registry_offers_has_requirements(self):
        from settings_registry import SETTING_DEFINITIONS, SettingValueType

        offered = {
            kind
            for definition in SETTING_DEFINITIONS.values()
            if definition.value_type in {SettingValueType.CHANNEL,
                                         SettingValueType.CHANNEL_LIST}
            for kind in definition.channel_types
        }
        self.assertEqual(
            [], sorted(offered - set(permission_audit.CHANNEL_KIND_REQUIREMENTS))
        )


if __name__ == "__main__":
    unittest.main()
