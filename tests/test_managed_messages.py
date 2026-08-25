"""The content builders, once a posted message could be edited again.

The old builders published a draft and discarded the message id, so "add a role
and press update" was not a missing button but a missing capability. These tests
pin the three things that made it one: the message is remembered, the limits
Discord actually enforces are checked before an operator presses Post, and a
rules panel renders as one message rather than one per section.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import dashboard_api
import managed_messages
import settings_cache
from feature_access import refresh_feature_cache


class ManagedMessageRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "dashboard.db")
        database.initialize_database()
        database.register_guild(123, "Test Guild")
        dashboard_api.app.config.update(TESTING=True)
        self.client = dashboard_api.app.test_client()
        self.original_admin_id = dashboard_api.ADMIN_ID
        dashboard_api.ADMIN_ID = "42"
        dashboard_api._rate_limit_events.clear()
        settings_cache.invalidate()
        with self.client.session_transaction() as session:
            session.update({
                "logged_in": True, "user_id": "42",
                "display": {"username": "tester", "avatar": None},
                "csrf_token": "csrf-token", "server_session_id": "server-session",
                "authorized_guild_ids": ["123"],
                "authenticated_at": time.time(),
            })
        self.headers = {"X-CSRF-Token": "csrf-token"}

    def tearDown(self):
        dashboard_api.ADMIN_ID = self.original_admin_id
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def save(self, kind, **changes):
        payload = {"menu_key": "games", "display_name": "Games", "revision": 0,
                   "title": "Pick your games", "body": "Tap a button.",
                   "colour": 0x5865F2, "options": {}, "entries": []}
        payload.update(changes)
        return self.client.post(f"/api/guilds/123/managed/{kind}", json=payload,
                                headers=self.headers)

    def role_menu(self, **changes):
        entries = [{"label": "Valorant", "role_id": "1420070400000000001",
                    "emoji": "🔫"}]
        return self.save("role_menu", entries=entries, **changes)

    # ------------------------------------------------------------- basics

    def test_a_role_menu_round_trips_with_its_entries(self):
        self.assertEqual(201, self.role_menu().status_code)
        listed = self.client.get("/api/guilds/123/managed/role_menu").get_json()
        menu = listed["data"][0]
        self.assertEqual("Games", menu["display_name"])
        self.assertEqual("Valorant", menu["entries"][0]["label"])
        self.assertFalse(menu["posted"], "nothing was published yet")

    def test_a_role_id_crosses_the_wire_as_a_string(self):
        """A 64-bit id through a browser number is rounded, and a rounded id
        matches no role — the same defect the settings route had."""
        self.role_menu()
        menu = self.client.get("/api/guilds/123/managed/role_menu").get_json()["data"][0]
        self.assertEqual("1420070400000000001", menu["entries"][0]["role_id"])
        self.assertGreater(int(menu["entries"][0]["role_id"]), 2 ** 53)

    def test_an_unknown_kind_is_refused(self):
        self.assertEqual(400, self.save("nonsense").status_code)
        self.assertEqual(400,
                         self.client.get("/api/guilds/123/managed/nonsense").status_code)

    def test_a_second_save_needs_the_current_revision(self):
        self.role_menu()
        stale = self.role_menu(revision=0)
        self.assertEqual(409, stale.status_code)
        self.assertEqual(201, self.role_menu(revision=1).status_code)

    # -------------------------------------------------------------- limits

    def test_a_duplicate_label_is_refused(self):
        """Two buttons with one label are two buttons that cannot be told
        apart: the label *is* the `custom_id` a posted button routes by."""
        response = self.save("role_menu", entries=[
            {"label": "Valorant", "role_id": "1420070400000000001", "emoji": ""},
            {"label": "Valorant", "role_id": "1420070400000000002", "emoji": ""},
        ])
        self.assertEqual(400, response.status_code)

    def test_a_row_without_a_role_is_refused(self):
        for entry, why in (
            ({"label": "x", "role_id": "", "emoji": ""}, "no role"),
            ({"label": "", "role_id": "1420070400000000001", "emoji": ""}, "no label"),
            ({"label": "x" * 81, "role_id": "1420070400000000001", "emoji": ""},
             "label past Discord's 80"),
        ):
            with self.subTest(why=why):
                self.assertEqual(400,
                                 self.save("role_menu", entries=[entry]).status_code)

    def test_more_than_25_buttons_is_refused(self):
        entries = [{"label": f"row{index}",
                    "role_id": str(1420070400000000001 + index), "emoji": ""}
                   for index in range(26)]
        self.assertEqual(400, self.save("role_menu", entries=entries).status_code)

    def test_a_role_menu_may_not_carry_options(self):
        """A stored value nothing reads is worse than a refusal."""
        self.assertEqual(400, self.role_menu(options={"variant": "games"}).status_code)

    # --------------------------------------------------------- rules panel

    def rules(self, sections, **changes):
        return self.save("rules", menu_key="rules", display_name="Rules",
                         title=None, body=None,
                         options={"sections": sections, "accept_button": True,
                                  "thumbnail": True, "button_label": None},
                         **changes)

    def test_a_rules_panel_takes_between_one_and_ten_sections(self):
        self.assertEqual(400, self.rules([]).status_code, "none")
        self.assertEqual(201, self.rules([{"title": "A", "body": "one"}]).status_code)
        self.assertEqual(201, self.rules(
            [{"title": f"S{i}", "body": "x"} for i in range(10)],
            revision=1).status_code, "ten is Discord's embeds-per-message limit")
        self.assertEqual(400, self.rules(
            [{"title": f"S{i}", "body": "x"} for i in range(11)],
            revision=2).status_code, "eleven is one more than a message holds")

    def test_the_six_thousand_character_message_total_is_checked(self):
        """Nothing checked it, and exceeding it fails the whole send."""
        sections = [{"title": "S", "body": "x" * 3000} for _ in range(3)]
        self.assertEqual(400, self.rules(sections).status_code)

    def test_a_section_past_four_thousand_and_ninety_six_is_refused(self):
        self.assertEqual(400, self.rules(
            [{"title": "S", "body": "x" * 4097}]).status_code)

    def test_a_section_needs_a_body_but_not_a_title(self):
        self.assertEqual(201, self.rules([{"title": None, "body": "text"}]).status_code)
        self.assertEqual(400, self.rules([{"title": "S", "body": ""}],
                                         revision=1).status_code)

    # ------------------------------------------------------------ publish

    def test_publishing_queues_one_action_and_refuses_an_empty_menu(self):
        self.save("role_menu", entries=[])
        empty = self.client.post(
            "/api/guilds/123/managed/role_menu/games/publish",
            json={"channel_id": "1420070400000000009"}, headers=self.headers)
        self.assertEqual(400, empty.status_code,
                         "a menu with no buttons is a message nobody can use")

        self.role_menu(revision=1)
        queued = self.client.post(
            "/api/guilds/123/managed/role_menu/games/publish",
            json={"channel_id": "1420070400000000009"}, headers=self.headers)
        self.assertEqual(202, queued.status_code)
        claimed = database.claim_control_action()
        self.assertEqual("publish_managed", claimed["action_type"])
        self.assertEqual({"kind": "role_menu", "menu_key": "games",
                          "channel_id": 1420070400000000009}, claimed["payload"])

    def test_publishing_something_that_does_not_exist_is_a_404(self):
        response = self.client.post(
            "/api/guilds/123/managed/role_menu/missing/publish",
            json={"channel_id": "1420070400000000009"}, headers=self.headers)
        self.assertEqual(404, response.status_code)

    # ------------------------------------------------------------- delete

    def test_deleting_a_posted_menu_queues_the_message_removal(self):
        """Otherwise the row goes and the buttons answer nothing forever."""
        self.role_menu()
        database.record_managed_post(123, "role_menu", "games",
                                     1420070400000000009, 1420070400000000010)
        response = self.client.delete("/api/guilds/123/managed/role_menu/games",
                                      json={"revision": 1}, headers=self.headers)
        self.assertEqual(200, response.status_code)
        self.assertIsNone(database.get_managed_message(123, "role_menu", "games"))
        claimed = database.claim_control_action()
        self.assertEqual("delete_managed", claimed["action_type"])
        self.assertEqual(1420070400000000010, claimed["payload"]["message_id"])

    def test_deleting_an_unposted_menu_queues_nothing(self):
        self.role_menu()
        response = self.client.delete("/api/guilds/123/managed/role_menu/games",
                                     json={"revision": 1}, headers=self.headers)
        self.assertEqual(200, response.status_code)
        self.assertIsNone(response.get_json()["data"]["action_id"])
        self.assertIsNone(database.claim_control_action())

    def test_the_delete_needs_the_current_revision(self):
        self.role_menu()
        response = self.client.delete("/api/guilds/123/managed/role_menu/games",
                                     json={"revision": 99}, headers=self.headers)
        self.assertEqual(409, response.status_code)

    # ---------------------------------------------------- audit and CSRF

    def test_a_save_is_audited(self):
        self.role_menu()
        entries = database.get_settings_audit(123, limit=10)
        self.assertTrue(
            any(row["action"] == "managed_message.save"
                and row["target_key"] == "role_menu:games" for row in entries),
            f"nothing recorded the change: {entries}")

    def test_a_mutation_without_the_csrf_header_is_refused(self):
        response = self.client.post("/api/guilds/123/managed/role_menu",
                                    json={"menu_key": "games"})
        self.assertEqual(403, response.status_code)


class RenderTests(unittest.TestCase):
    """One renderer for the bot's commands and the dashboard's Post.

    The two used to disagree: `/rules_group` sent one message carrying every
    section, the guild icon and an accept button, and the dashboard sent one bare
    embed per section with no view at all.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "render.db")
        database.initialize_database()
        database.register_guild(123, "Test Guild")
        settings_cache.invalidate()
        # The gate fails closed while a guild's policy is unknown, so the cache
        # has to be loaded before any of this renders anything at all.
        refresh_feature_cache(123)

        class Icon:
            url = "https://example.invalid/icon.png"

        class Guild:
            id = 123
            icon = Icon()

        self.guild = Guild()

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def stored(self, kind, **changes):
        payload = {"display_name": "Thing", "expected_revision": 0}
        payload.update(changes)
        database.save_managed_message(123, 42, kind, "thing",
                                      payload.pop("display_name"),
                                      payload.pop("expected_revision"), **payload)
        return database.get_managed_message(123, kind, "thing")

    def test_a_rules_panel_is_one_message_carrying_every_section(self):
        row = self.stored("rules", colour=0x112233, options={
            "sections": [{"title": "A", "body": "one"},
                         {"title": "B", "body": "two"},
                         {"title": None, "body": "three"}],
            "accept_button": True, "thumbnail": True})
        embeds, view = managed_messages.render_managed_message(self.guild, row)
        self.assertEqual(3, len(embeds), "one message, three embeds")
        self.assertEqual([0x112233] * 3, [embed.color.value for embed in embeds])
        self.assertIsNotNone(embeds[0].thumbnail.url, "the guild icon leads")
        self.assertIsNone(embeds[1].thumbnail.url, "and only leads")
        self.assertIsNotNone(view, "the accept button is the point of the panel")

    def test_the_accept_button_and_thumbnail_can_be_turned_off(self):
        row = self.stored("rules", options={
            "sections": [{"title": "A", "body": "one"}],
            "accept_button": False, "thumbnail": False})
        embeds, view = managed_messages.render_managed_message(self.guild, row)
        self.assertIsNone(view)
        self.assertIsNone(embeds[0].thumbnail.url)

    def test_a_literal_backslash_n_becomes_a_newline(self):
        """The substitution `/rules_group` has always made: a one-line form
        field cannot hold a real newline."""
        row = self.stored("rules", options={
            "sections": [{"title": "A", "body": "one\\ntwo"}]})
        embeds, _view = managed_messages.render_managed_message(self.guild, row)
        self.assertEqual("one\ntwo", embeds[0].description)

    def test_a_role_menu_renders_this_guild_s_buttons(self):
        row = self.stored("role_menu", title="Games", body="Pick.",
                          entries=[{"label": "Valorant", "role_id": 1420070400000000001,
                                    "emoji": ""}])
        embeds, view = managed_messages.render_managed_message(self.guild, row)
        self.assertEqual(1, len(embeds))
        self.assertEqual(["Valorant"], [child.label for child in view.children])

    def test_an_empty_role_menu_reports_a_reason_rather_than_sending(self):
        row = self.stored("role_menu", title="Games", body="Pick.", entries=[])
        embeds, error = managed_messages.render_managed_message(self.guild, row)
        self.assertIsNone(embeds)
        self.assertEqual("managed_menu_empty", error)

    def test_a_disabled_feature_refuses_at_send_time(self):
        """Checked here rather than at queue time: a feature can be switched off
        while the action waits in the outbox."""
        database.set_feature_state(123, "role_menus", False, 42,
                                   database.get_feature_states(123)["role_menus"]["revision"])
        refresh_feature_cache(123)
        row = self.stored("role_menu", title="Games", body="Pick.",
                          entries=[{"label": "Valorant", "role_id": 1420070400000000001,
                                    "emoji": ""}])
        embeds, error = managed_messages.render_managed_message(self.guild, row)
        self.assertIsNone(embeds)
        self.assertEqual("feature_disabled_or_invalid_panel", error)


class ButtonLabelTests(unittest.TestCase):
    """The operator's button text has to survive to the button.

    It did not: the dashboard collected and validated `accept_label` and
    `render_managed_message` ignored it, because `RuleAcceptView()` took no
    argument. Stored, validated and silently discarded is the worst of the three.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "labels.db")
        database.initialize_database()
        database.register_guild(123, "Test Guild")
        settings_cache.invalidate()
        refresh_feature_cache(123)

        class Guild:
            id = 123
            icon = None

        self.guild = Guild()

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def stored(self, kind, options):
        database.save_managed_message(123, 42, kind, "thing", "Thing", 0,
                                      title="T", body="B", options=options)
        return database.get_managed_message(123, kind, "thing")

    def rules_options(self, **changes):
        options = {"sections": [{"title": "A", "body": "one"}],
                   "accept_button": True}
        options.update(changes)
        return options

    def test_a_stored_label_reaches_the_button(self):
        for kind, options in (
            ("rules", self.rules_options(button_label="Elfogadom")),
            ("ticket", {"button_label": "Segítség kell"}),
            ("airlock", {"button_label": "Beengedés"}),
        ):
            with self.subTest(kind=kind):
                _embeds, view = managed_messages.render_managed_message(
                    self.guild, self.stored(kind, options))
                self.assertEqual([options["button_label"]],
                                 [child.label for child in view.children])

    def test_no_label_falls_back_to_the_shipped_one(self):
        from cogs.utils import t
        for kind, options, key in (
            ("rules", self.rules_options(), "admin.accept_rules_button"),
            ("ticket", {}, "tickets.open_btn"),
            ("airlock", {}, "admin.enter_server_button"),
        ):
            with self.subTest(kind=kind):
                _embeds, view = managed_messages.render_managed_message(
                    self.guild, self.stored(kind, options))
                self.assertEqual([t(key)],
                                 [child.label for child in view.children])

    def test_a_blank_label_falls_back_rather_than_rendering_nothing(self):
        """Absent, blank and set are three states, and only the third wins."""
        _embeds, view = managed_messages.render_managed_message(
            self.guild, self.stored("ticket", {"button_label": "   "}))
        from cogs.utils import t
        self.assertEqual([t("tickets.open_btn")],
                         [child.label for child in view.children])

    def test_an_over_long_label_is_truncated_rather_than_sent(self):
        """discord.py does not check the length; Discord answers 400, and the
        operator would see an opaque outbox code after pressing Post."""
        _embeds, view = managed_messages.render_managed_message(
            self.guild, self.stored("ticket", {"button_label": "x" * 200}))
        self.assertEqual(managed_messages.BUTTON_LABEL_LIMIT,
                         len(view.children[0].label))

    def test_the_custom_ids_never_depend_on_the_label(self):
        """A click routes by `custom_id`. If a label could change one, renaming a
        button would orphan every message already posted."""
        from cogs.admin import EnterServerView, RuleAcceptView
        from cogs.tickets import TicketLauncher
        for build, expected in (
            (RuleAcceptView, "accept_rules_btn"),
            (EnterServerView, "enter_server_btn"),
            (TicketLauncher, "ticket_button"),
        ):
            with self.subTest(view=build.__name__):
                default = {child.custom_id for child in build().children}
                relabelled = {child.custom_id
                              for child in build(label="Anything").children}
                self.assertEqual({expected}, default)
                self.assertEqual(default, relabelled)


class ButtonLabelRouteTests(ManagedMessageRouteTests):
    """The API half of the same contract."""

    def test_a_launcher_accepts_a_button_label(self):
        for kind in ("ticket", "airlock"):
            with self.subTest(kind=kind):
                response = self.save(kind, options={"button_label": "Press me"})
                self.assertEqual(201, response.status_code)

    def test_an_unknown_option_is_still_refused(self):
        for kind in ("ticket", "airlock"):
            with self.subTest(kind=kind):
                self.assertEqual(400, self.save(
                    kind, options={"variant": "games"}).status_code)

    def test_an_over_long_or_multiline_label_is_refused(self):
        for label, why in (("x" * 81, "past Discord's 80"),
                           ("two\nlines", "a newline draws as a space")):
            with self.subTest(why=why):
                self.assertEqual(400, self.save(
                    "ticket", options={"button_label": label}).status_code)

    def test_the_rules_panel_uses_the_same_option_name(self):
        """One concept, one name. `accept_label` was a second spelling for it."""
        self.assertEqual(400, self.save(
            "rules", menu_key="rules", display_name="Rules",
            title=None, body=None,
            options={"sections": [{"title": "A", "body": "one"}],
                     "accept_label": "Old name"}).status_code)


class CreatorDeclarationTests(unittest.TestCase):
    """The client's kind table against the server's kinds.

    The creator is a declaration rather than four functions, so the way it can
    go wrong is the declaration disagreeing with what the API accepts — a field
    the validator refuses is an unexplained rejection, and a kind nobody renders
    is a page an operator cannot reach.
    """

    def setUp(self):
        self.source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        self.html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        block = self.source[self.source.index("const MANAGED_KINDS = {"):]
        self.spec = block[:block.index("\n};")]

    def declared_pages(self):
        """Each kind and the page it owns.

        The page is looked up *within* the kind's block rather than required on
        its first line — a comment above it is normal and should not make a kind
        invisible to this test.
        """
        pages = {}
        starts = [(match.group(1), match.start()) for match
                  in re.finditer(r"^    (\w+): \{$", self.spec, re.MULTILINE)]
        for index, (kind, start) in enumerate(starts):
            end = starts[index + 1][1] if index + 1 < len(starts) else len(self.spec)
            page = re.search(r"page: '([\w-]+)'", self.spec[start:end])
            if page:
                pages[kind] = page.group(1)
        return pages

    def test_every_kind_the_server_has_is_rendered_by_a_page(self):
        pages = self.declared_pages()
        self.assertEqual(set(database.MANAGED_MESSAGE_KINDS), set(pages),
                         "a kind with no page is a row nobody can edit")
        self.assertEqual(len(set(pages.values())), len(pages),
                         "two kinds cannot share one page")

    def test_every_page_exists_in_the_markup(self):
        for kind, page in self.declared_pages().items():
            with self.subTest(kind=kind):
                self.assertIn(f'<section id="{page}" class="page', self.html)
                for part in ("list", "list-card", "editor"):
                    self.assertIn(f'id="{page}-{part}"', self.html)

    def test_each_page_is_gated_on_the_feature_that_owns_its_kind(self):
        """Three copies of that mapping would drift; this pins the two that
        exist. The old Panels page had no gate at all, so it offered a ticket
        launcher on a guild with tickets switched off."""
        import managed_messages
        for kind, page in self.declared_pages().items():
            owner = managed_messages.MANAGED_KIND_FEATURES[kind]
            match = re.search(rf'data-feature="(\w+)" data-page="{page}"', self.html)
            with self.subTest(kind=kind):
                if owner is None:
                    # A plain embed is the bot saying something in a channel;
                    # there is no feature anybody would switch off, and a gate
                    # here would be a toggle with nothing behind it.
                    self.assertIsNone(match, f"{page} is gated but owns no feature")
                    self.assertIn(f'data-page="{page}"', self.html)
                else:
                    self.assertIsNotNone(match, f"{page} has no feature gate")
                    self.assertEqual(owner, match.group(1))

    def test_the_retired_panels_page_is_gone(self):
        for needle in ('id="panels"', 'data-page="panels"', "managed_kind_ticket"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.html + self.source)


class CreatorRoundTripTests(unittest.TestCase):
    """Opening a creator and saving it unchanged must write back what was there.

    Driven through Node, because `unpack` and `pack` are JavaScript and a Python
    re-implementation could stay green while the real pair broke. The POST route
    uses `require_exact_keys`, so a kind that forgets to emit `entries: []` has
    its whole save refused rather than one field ignored.
    """

    def test_every_kind_round_trips_into_the_payload_the_api_demands(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")

        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        start = source.index("const MANAGED_FIELDS = {")
        end = source.index("let managedItems = [];")
        spec = source[start:end]

        # One row per kind, shaped the way `list_managed_messages` shapes it —
        # ids as strings, options as the validator stores them.
        rows = {
            "rules": {
                "kind": "rules", "menu_key": "rules", "display_name": "Rules",
                "channel_id": "1420070400000000001", "message_id": None,
                "title": None, "body": None, "colour": 0x5865F2,
                "options": {"sections": [{"title": "A", "body": "one"},
                                         {"title": None, "body": "two"}],
                            "accept_button": True, "thumbnail": False,
                            "button_label": "Elfogadom"},
                "revision": 3, "entries": [], "posted": False,
            },
            "role_menu": {
                "kind": "role_menu", "menu_key": "games", "display_name": "Games",
                "channel_id": "1420070400000000002", "message_id": None,
                "title": "Pick", "body": "Tap a button.", "colour": 0x248046,
                "options": {}, "revision": 7, "posted": False,
                "entries": [{"label": "Valorant",
                             "role_id": "1420070400000000003", "emoji": "🔫"},
                            {"label": "Minecraft",
                             "role_id": "1420070400000000004", "emoji": ""}],
            },
            "ticket": {
                "kind": "ticket", "menu_key": "ticket", "display_name": "Support",
                "channel_id": "1420070400000000005", "message_id": None,
                "title": "Need help?", "body": "Press below.", "colour": 0x1ABC9C,
                "options": {"button_label": "Segítség"}, "revision": 1,
                "entries": [], "posted": False,
            },
            "airlock": {
                "kind": "airlock", "menu_key": "airlock", "display_name": "Gate",
                "channel_id": "1420070400000000006", "message_id": None,
                "title": "In you go", "body": "Press below.", "colour": 0xE67E22,
                "options": {"button_label": "Beengedés"}, "revision": 2,
                "entries": [], "posted": False,
            },
            "embed": {
                "kind": "embed", "menu_key": "notice", "display_name": "Notice",
                "channel_id": "1420070400000000007", "message_id": None,
                "title": None, "body": None, "colour": 0x9B59B6,
                "options": {"sections": [{"title": "Heads up", "body": "text"}],
                            "image_url": "https://cdn.example/banner.png"},
                "revision": 4, "entries": [], "posted": False,
            },
        }
        self.assertEqual(set(database.MANAGED_MESSAGE_KINDS), set(rows),
                         "a kind with no fixture is a kind this cannot check")

        driver = "\n".join([
            spec,
            f"const ROWS = {json.dumps(rows)};",
            (ROOT / "tests" / "js" / "managed_roundtrip.js").read_text(encoding="utf-8"),
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(driver)
            path = handle.name
        try:
            result = subprocess.run([node, path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class CreatorAcceptedByTheApiTests(ManagedMessageRouteTests):
    """What the creator submits, submitted for real.

    The round-trip test above checks the shape; this checks the *contract* — it
    builds each kind's payload with the client's own `unpack`/`pack` and POSTs it
    through the real route. A field the validator refuses is an unexplained
    rejection an operator meets after filling a form in, and no amount of
    reasoning about two files in two languages catches it.
    """

    def client_payloads(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        spec = source[source.index("const MANAGED_FIELDS = {"):
                      source.index("let managedItems = [];")]
        driver = spec + """
const out = {};
for (const [kind, spec] of Object.entries(MANAGED_KINDS)) {
    const values = spec.unpack(null);
    values.display_name = 'Example';
    values.menu_key = values.menu_key || 'example';
    values.title = values.title || 'Title';
    values.body = values.body || 'Body';
    values.button_label = 'Press me';
    if (values.embeds) values.embeds = [{title: 'A', body: 'one'}];
    if (values.entries) values.entries = [{label: 'Valorant',
        role_id: '1420070400000000003', emoji: ''}];
    out[kind] = {menu_key: values.menu_key, display_name: values.display_name,
                 revision: 0, colour: values.colour, ...spec.pack(values)};
}
console.log(JSON.stringify(out));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(driver)
            path = handle.name
        try:
            result = subprocess.run([node, path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_a_freshly_filled_creator_saves_for_every_kind(self):
        payloads = self.client_payloads()
        self.assertEqual(set(database.MANAGED_MESSAGE_KINDS), set(payloads))
        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                response = self.client.post(
                    f"/api/guilds/123/managed/{kind}", json=payload,
                    headers=self.headers)
                self.assertEqual(201, response.status_code,
                                 response.get_json())


class AdoptTests(unittest.TestCase):
    """Taking over a message the bot already posted.

    The schema-12 migration leaves `message_id` NULL deliberately: a menu that
    was already up keeps working while the dashboard cannot edit *that* message.
    This is the half that was documented and never built. Its refusals matter
    more than its successes — adopting a message the bot cannot edit would
    succeed and then fail on every Update, forever.
    """

    def parse(self, value, guild_id=123):
        return dashboard_api._parse_message_reference(value, guild_id)

    def test_a_message_link_is_accepted(self):
        channel, message = self.parse(
            "https://discord.com/channels/123/1420070400000000001/1420070400000000002")
        self.assertEqual(1420070400000000001, channel)
        self.assertEqual(1420070400000000002, message)

    def test_a_bare_id_is_accepted_with_no_channel(self):
        channel, message = self.parse("1420070400000000002")
        self.assertIsNone(channel)
        self.assertEqual(1420070400000000002, message)

    def test_a_link_from_another_guild_is_refused(self):
        """A link names its own guild, so it is checked rather than trusted."""
        with self.assertRaises(dashboard_api.RequestValidationError):
            self.parse("https://discord.com/channels/999/1420070400000000001/"
                       "1420070400000000002")

    def test_nonsense_is_refused(self):
        for value in ("", "   ", "not a link", "12345", None, 42,
                      "http://discord.com/channels/123/1/2",
                      "https://example.invalid/channels/123/1/2"):
            with self.subTest(value=value):
                with self.assertRaises(dashboard_api.RequestValidationError):
                    self.parse(value)

    def test_the_ids_stay_exact_past_javascript_precision(self):
        """Verified against a live id, written with a made-up one — the only
        property this needs is being above 2**53."""
        _channel, message = self.parse("1420070400000000010")
        self.assertEqual(1420070400000000010, message)
        self.assertGreater(message, 2 ** 53)


class AdoptContentTests(unittest.TestCase):
    """Reading a posted message back into the fields a row holds."""

    class Thumb:
        def __init__(self, url):
            self.url = url

    class Embed:
        def __init__(self, title=None, description=None, colour=None,
                     thumbnail=None, image=None):
            self.title = title
            self.description = description
            self.color = type("C", (), {"value": colour})() if colour else None
            self.thumbnail = AdoptContentTests.Thumb(thumbnail)
            self.image = AdoptContentTests.Thumb(image)

    class Button:
        def __init__(self, custom_id, label):
            self.custom_id = custom_id
            self.label = label

    class Row:
        def __init__(self, children):
            self.children = children

    class Message:
        def __init__(self, embeds, components):
            self.embeds = embeds
            self.components = components

    def test_a_rules_message_becomes_sections(self):
        message = self.Message(
            [self.Embed("Szabályok", "Tartsd be", 0x5865F2, thumbnail="x",
                        image="https://cdn.example/banner.png"),
             self.Embed("Büntetések", "Első alkalommal")],
            [self.Row([self.Button("accept_rules_btn", "Elfogadom")])])
        content = dashboard_api._content_from_message("rules", message)
        options = content["options"]
        self.assertEqual(2, len(options["sections"]))
        self.assertEqual("Szabályok", options["sections"][0]["title"])
        self.assertEqual("Első alkalommal", options["sections"][1]["body"])
        self.assertEqual(0x5865F2, content["colour"])
        self.assertTrue(options["accept_button"])
        self.assertTrue(options["thumbnail"])
        self.assertEqual("Elfogadom", options["button_label"])
        self.assertEqual("https://cdn.example/banner.png", options["image_url"])

    def test_a_rules_verify_banner_survives_being_read(self):
        """Without the image field, adopting one of those messages would strip
        the banner on the first Update — silently."""
        message = self.Message(
            [self.Embed("Szabályzat", "Fogadd el", 0x2ECC71,
                        image="https://cdn.example/b.png")],
            [self.Row([self.Button("accept_rules_btn", "Elfogadom")])])
        content = dashboard_api._content_from_message("rules", message)
        self.assertEqual("https://cdn.example/b.png",
                         content["options"]["image_url"])

    def test_an_http_banner_is_dropped_rather_than_stored(self):
        message = self.Message(
            [self.Embed("T", "B", 0x1, image="http://cdn.example/b.png")], [])
        content = dashboard_api._content_from_message("rules", message)
        self.assertIsNone(content["options"]["image_url"])

    def test_a_missing_accept_button_reads_as_off(self):
        message = self.Message([self.Embed("T", "B", 0x1)], [])
        content = dashboard_api._content_from_message("rules", message)
        self.assertFalse(content["options"]["accept_button"])

    def test_a_shipped_label_is_stored_as_absent(self):
        """So the panel keeps following the language setting instead of being
        pinned to today's translation of it."""
        from cogs.utils import t
        message = self.Message(
            [self.Embed("T", "B", 0x1)],
            [self.Row([self.Button("ticket_button", t("tickets.open_btn"))])])
        content = dashboard_api._content_from_message("ticket", message)
        self.assertEqual({}, content["options"])

    def test_a_changed_label_is_kept(self):
        message = self.Message(
            [self.Embed("T", "B", 0x1)],
            [self.Row([self.Button("ticket_button", "Segítség kell")])])
        content = dashboard_api._content_from_message("ticket", message)
        self.assertEqual("Segítség kell", content["options"]["button_label"])

    def test_a_launcher_takes_its_text_from_the_leading_embed(self):
        message = self.Message([self.Embed("Kell segítség?", "Nyomd meg", 0xABC)], [])
        content = dashboard_api._content_from_message("airlock", message)
        self.assertEqual("Kell segítség?", content["title"])
        self.assertEqual("Nyomd meg", content["body"])
        self.assertEqual(0xABC, content["colour"])

    def test_role_menu_entries_are_never_read_back(self):
        """A button carries no role id — the role is resolved per click — so the
        database is the only place this guild's roles exist. Reading the message
        could only lose them."""
        message = self.Message(
            [self.Embed("Games", "Pick", 0x1)],
            [self.Row([self.Button("role_valorant", "Valorant")])])
        content = dashboard_api._content_from_message("role_menu", message)
        self.assertIsNone(content["entries"])

    def test_every_kind_with_a_fixed_button_declares_its_custom_id(self):
        """A kind missing from the map would read every label as absent."""
        import managed_messages
        # A role menu's buttons are the operator's, and a plain embed has none.
        fixed = set(database.MANAGED_MESSAGE_KINDS) - {"role_menu", "embed"}
        self.assertEqual(fixed, set(dashboard_api.KIND_BUTTON_IDS))
        self.assertEqual(
            fixed,
            {kind for kind, feature in managed_messages.MANAGED_KIND_FEATURES.items()
             if feature} - {"role_menu"})


class BuilderScopeTests(unittest.TestCase):
    """The draft system is gone, and must not come back by halves.

    `dashboard_documents` held drafts that could be posted and never edited. Every
    kind that posts a message is a managed message now, so a route or an action
    type that writes a draft is dead weight with a live endpoint attached.
    """

    def test_the_draft_routes_are_gone(self):
        for name in ("guild_builders", "delete_guild_builder", "publish_builder",
                     "_validate_builder_content"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(dashboard_api, name))

    def test_the_retired_action_types_are_refused(self):
        for action_type in ("publish_rules", "publish_panel", "send_embed"):
            with self.subTest(action_type=action_type):
                with self.assertRaises(ValueError):
                    database.queue_control_action(1, 1, action_type, {})

    def test_the_table_survives_without_a_reader(self):
        """Dropping it is a destructive migration with nothing to gain, which is
        the same call `server_config` got."""
        source = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        self.assertNotIn("list_dashboard_documents", source)
        self.assertIn("dashboard_documents",
                      (ROOT / "database.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
