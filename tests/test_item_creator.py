"""The item creator's declaration, held to the server that validates it.

The old creator had no test of any kind: no check that its six templates matched
the server's, no round trip, and no verification that a payload it built was one
the API would accept. That gap is why it was still asking operators to type raw
JSON long after the managed-message builders had been brought under a
declaration. These are the three checks that pattern uses.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import dashboard_api
import settings_cache
import database
import item_catalog


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dashboard" / "script.js"


def declared_templates():
    """The template keys the client declares, read out of the source."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("const SHOP_TEMPLATES = {")
    end = source.index("\n};", start)
    return set(re.findall(r"^    ([a-z_]+): \{$", source[start:end], re.M))


class TemplateDeclarationTests(unittest.TestCase):
    def test_the_premise_holds(self):
        self.assertGreaterEqual(len(declared_templates()), 6)

    def test_the_client_declares_exactly_the_templates_the_server_allows(self):
        """A template the server accepts but the client cannot build is a
        feature nobody can reach; the reverse is a form that always 400s."""
        self.assertEqual(set(dashboard_api.SAFE_SHOP_TEMPLATES), declared_templates())

    def test_no_raw_json_field_survives(self):
        """The textarea is what made this creator unusable: four of six
        templates expected an undocumented shape typed correctly first time."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("shopItemConfigFromCatalog", source)
        self.assertNotIn("CATALOG_TEMPLATE_EFFECTS", source)
        html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('name="config"', html)

    def test_every_declared_field_has_a_descriptor(self):
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("const SHOP_TEMPLATE_FIELDS = {")
        end = source.index("\n};", start)
        descriptors = set(re.findall(r"^    ([a-z_]+): \{", source[start:end], re.M))
        # Scoped to the declaration, or this also reads MANAGED_KINDS' fields.
        block_start = source.index("const SHOP_TEMPLATES = {")
        block_end = source.index("\n};", block_start)
        used = set(re.findall(r"fields: \[([^\]]*)\]",
                              source[block_start:block_end]))
        named = {name.strip().strip("'\"")
                 for group in used for name in group.split(",") if name.strip()}
        self.assertEqual(set(), named - descriptors,
                         "a template names a field with no descriptor")

    def test_every_field_label_and_option_label_exists(self):
        """A missing label renders as a bracketed key on the only form that
        explains what an item does."""
        source = SCRIPT.read_text(encoding="utf-8")
        labels = set(re.findall(r"label: '(dashboard\.[a-z_]+)'", source))
        for language in ("hu", "en"):
            catalog = json.loads(
                (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8"))
            for label in labels:
                node = catalog
                for part in label.split("."):
                    self.assertIn(part, node, f"{language}: missing {label}")
                    node = node[part]
                self.assertTrue(node, f"{language}: {label} is empty")
            templates = catalog["dashboard"]["item_templates"]
            self.assertEqual(set(dashboard_api.SAFE_SHOP_TEMPLATES), set(templates))
            effects = catalog["dashboard"]["item_effects"]
            self.assertEqual(
                {effect.value for effect in item_catalog.ItemEffect}, set(effects),
                f"{language}: an item effect has no label")


class TemplateRoundTripTests(unittest.TestCase):
    """`unpack` then `pack` must return what was stored, byte for byte.

    Run through Node, because the pair is JavaScript: a Python
    re-implementation could stay green while the real one broke.
    """

    def test_every_template_round_trips(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        script = Path(__file__).parent / "js" / "item_template_roundtrip.js"
        result = subprocess.run([node, str(script), str(SCRIPT)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, f"{result.stdout}\n{result.stderr}")
        self.assertIn("ok", result.stdout)


class DashboardItemTestCase(unittest.TestCase):
    """A host session against a temporary database.

    Host, deliberately: a non-host request is refreshed against Discord by
    `recheck_mutation_guild_permissions`, which has no OAuth token in a test and
    answers 401. Host authority is re-derived from ADMIN_DISCORD_ID per request
    and needs no Discord call.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "items.db")
        database.initialize_database()
        database.register_guild(123, "Guild")
        dashboard_api.app.config.update(TESTING=True)
        self.client = dashboard_api.app.test_client()
        self.original_admin_id = dashboard_api.ADMIN_ID
        dashboard_api.ADMIN_ID = "42"
        # Process-global, so without a reset a request-heavy test exhausts the
        # rate limit for every test that runs after it.
        dashboard_api._rate_limit_events.clear()
        dashboard_api._oauth_tokens.clear()
        dashboard_api._permission_cache._entries.clear()
        # And so is the settings cache. A test here writes `shop_hidden_items`,
        # and left behind it answers for the next test — the same discipline
        # `database.DB_PATH` needs, and the failure only shows in a full run.
        settings_cache.invalidate()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["user_id"] = "42"
            session["display"] = {"username": "tester", "avatar": None}
            session["csrf_token"] = "csrf-token"
            session["server_session_id"] = "server-session"
            session["authorized_guild_ids"] = ["123"]
            # The absolute-lifetime gate expires a session with no recorded
            # login instant, so a fabricated one has to carry it too.
            session["authenticated_at"] = time.time()

    def tearDown(self):
        dashboard_api.ADMIN_ID = self.original_admin_id
        database.DB_PATH = self.original_path
        settings_cache.invalidate()
        self.temp_dir.cleanup()

    def create_item(self, payload):
        response = self.client.post(
            "/api/guilds/123/shop-items", json=payload,
            headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))


class TemplateFieldsFollowTheChoiceTests(unittest.TestCase):
    """Section three must follow the template chosen above it.

    It did not: the redraw listened inside `.field` while the wrapper's class is
    `input-group`, so every template kept asking for a role — pick "vault" and it
    still said select a role. The declaration was right and only the wiring was
    wrong, which is why nothing else could see it.
    """

    def test_every_template_asks_for_its_own_fields(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        if not (ROOT / "node_modules" / "jsdom").is_dir():
            self.skipTest("jsdom is not installed; run `npm install`")
        script = ROOT / "tests" / "js" / "item_editor_templates.js"
        result = subprocess.run([node, str(script), str(ROOT)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode,
                         f"{result.stdout}\n{result.stderr}")

    def test_the_item_page_groups_by_section_and_shows_the_room_left(self):
        """The other half of the per-section cap: the interface has to be able
        to say which shelf is full before a save is refused, or the API's rule
        arrives as an unexplained rejection."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        if not (ROOT / "node_modules" / "jsdom").is_dir():
            self.skipTest("jsdom is not installed; run `npm install`")
        script = ROOT / "tests" / "js" / "shop_sections.js"
        result = subprocess.run([node, str(script), str(ROOT)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode,
                         f"{result.stdout}\n{result.stderr}")


class ItemListEndpointTests(DashboardItemTestCase):
    """The merged list. Nothing could assemble it before: the catalog route
    serves no names, and `/api/locale` serves only the dashboard namespace."""

    def get(self, lang="en"):
        response = self.client.get(f"/api/guilds/123/items?lang={lang}")
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        return response.get_json()

    def test_every_built_in_item_appears_with_a_name_and_a_description(self):
        payload = self.get()
        builtin = {item["item_key"]: item for item in payload["data"]
                   if item["source"] == "builtin"}
        self.assertEqual(set(item_catalog.ITEM_DEFINITIONS), set(builtin))
        for key, item in builtin.items():
            self.assertTrue(item["name"], f"{key} has no name")
            self.assertNotEqual(key, item["name"], f"{key} shows its raw key")
            self.assertTrue(item["description"], f"{key} has no description")

    def test_it_says_where_each_item_comes_from(self):
        builtin = {item["item_key"]: item for item in self.get()["data"]
                   if item["source"] == "builtin"}
        # A gacha-only item is a real state the old table could not express.
        self.assertTrue(builtin["loaded_die"]["in_shop"])
        self.assertTrue(builtin["loaded_die"]["in_gacha"])
        self.assertTrue(builtin["premium"]["in_shop"])
        self.assertFalse(builtin["premium"]["in_gacha"])

    def test_a_built_in_is_not_editable_but_names_its_price_setting(self):
        builtin = {item["item_key"]: item for item in self.get()["data"]
                   if item["source"] == "builtin"}
        self.assertFalse(builtin["loaded_die"]["editable"])
        self.assertEqual("shop_price_loaded_die", builtin["loaded_die"]["price_setting"])

    def test_the_language_follows_the_request(self):
        english = {item["item_key"]: item["name"] for item in self.get("en")["data"]}
        hungarian = {item["item_key"]: item["name"] for item in self.get("hu")["data"]}
        self.assertNotEqual(english["loaded_die"], hungarian["loaded_die"])

    def test_a_custom_item_carries_what_an_edit_needs(self):
        self.create_item({
            "item_key": "vip", "template_type": "coin_bundle", "category": None, "enabled": True,
            "price": 500, "config": {"amount": 100, "repeatable": False},
            "text": {"name": "VIP", "description": "leírás"},
        })
        custom = [item for item in self.get()["data"] if item["source"] == "custom"]
        self.assertEqual(1, len(custom))
        item = custom[0]
        self.assertTrue(item["editable"])
        # The PATCH route reuses the creation validator, so a partial body is
        # refused: the row has to carry everything a save must send back.
        for field in ("config", "name", "description", "revision", "price",
                      "enabled"):
            self.assertIn(field, item)

    def test_the_room_left_per_section_is_reported(self):
        """The interface has to be able to say "Casino 6/25" before a save is
        refused. A rule the API enforces but the form cannot express turns into
        an unexplained rejection, so the room travels with the items."""
        import item_catalog

        payload = self.get()
        self.assertEqual(0, payload["custom_count"])
        sections = {entry["id"]: entry for entry in payload["categories"]}
        # Every section, always, and in the declared order: an operator must be
        # able to file an item onto an empty shelf.
        self.assertEqual([c.value for c in item_catalog.SHOP_CATEGORY_ORDER],
                         [entry["id"] for entry in payload["categories"]])
        for category in item_catalog.SHOP_CATEGORY_ORDER:
            entry = sections[category.value]
            builtin = len(item_catalog.shop_items_in(category.value))
            self.assertEqual(builtin, entry["builtin"])
            self.assertEqual(0, entry["custom"])
            self.assertEqual(builtin, entry["used"])
            self.assertEqual(item_catalog.SELECT_OPTION_LIMIT, entry["limit"])
            self.assertEqual(item_catalog.custom_item_capacity(category.value),
                             entry["remaining"])
            # Labelled, because the browser cannot read the `shop` namespace.
            self.assertTrue(entry["label"])
            self.assertFalse(entry["label"].startswith("["))

    def test_a_builtin_reports_its_section_and_whether_it_is_hidden(self):
        payload = self.get()
        rows = {entry["item_key"]: entry for entry in payload["data"]}
        self.assertEqual("protection", rows["small_vault"]["category"])
        self.assertEqual("casino", rows["loaded_die"]["category"])
        self.assertEqual("perks", rows["streak_freeze"]["category"])
        self.assertEqual("heist", rows["lockpick"]["category"])
        self.assertFalse(rows["rent_sound"]["hidden"])

    def test_hiding_a_builtin_frees_a_slot_and_keeps_the_row_visible(self):
        """A hidden item must still be listed, or an operator could never find
        it to un-hide it."""
        import item_catalog

        before = {entry["id"]: entry for entry in self.get()["categories"]}
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "shop_hidden_items",
                               "value": ["rent_sound"], "revision": 0}]},
            headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))

        payload = self.get()
        after = {entry["id"]: entry for entry in payload["categories"]}
        self.assertEqual(before["rentals"]["remaining"] + 1,
                         after["rentals"]["remaining"])
        self.assertEqual(before["rentals"]["builtin"] - 1,
                         after["rentals"]["builtin"])
        rows = {entry["item_key"]: entry for entry in payload["data"]}
        self.assertTrue(rows["rent_sound"]["hidden"])
        # And it stays a reserved key, so nothing can shadow it.
        import database as db
        self.assertIn("rent_sound", db.BUILTIN_SHOP_KEYS)


class CustomItemTextTests(DashboardItemTestCase):
    """A custom item is written once, in whatever language the guild speaks.

    This briefly took two languages, on the reasoning that every built-in has
    both. Wrong analogy: a built-in ships to every installation and must read in
    each, while a custom item lives in one guild's database and is read only by
    that guild's members. A server with two main languages would use English for
    both rather than keep two columns, so the second field was work with no
    reader.
    """

    ITEM = {"item_key": "vip", "template_type": "coin_bundle", "category": None, "enabled": True,
            "price": 500, "config": {"amount": 100, "repeatable": False}}

    def test_the_text_is_returned_as_written(self):
        self.create_item({**self.ITEM,
                          "text": {"name": "Aranytálca", "description": "magyar"}})
        item = database.get_shop_item_definitions(123)[0]
        self.assertEqual("Aranytálca", item["name"])
        self.assertEqual("magyar", item["description"])

    def test_a_language_argument_changes_nothing(self):
        """Accepted and ignored, so callers need not change if a guild language
        is ever added."""
        self.create_item({**self.ITEM,
                          "text": {"name": "Aranytálca", "description": "magyar"}})
        for language in ("hu", "en", "de", None):
            item = database.get_shop_item_definitions(123, language)[0]
            self.assertEqual("Aranytálca", item["name"])

    def test_a_second_language_is_refused_rather_than_stored(self):
        """`require_exact_keys` refuses an extra key, so a payload written
        against the old two-field shape fails loudly instead of half-saving."""
        response = self.client.post(
            "/api/guilds/123/shop-items",
            json={**self.ITEM, "text": {"name": "A", "description": "B"},
                  "en": {"name": "Golden platter", "description": "english"}},
            headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(400, response.status_code)

    def test_the_text_is_required(self):
        for text in ({"name": "", "description": "x"},
                     {"name": "x", "description": ""},
                     {"name": "x"}):
            response = self.client.post(
                "/api/guilds/123/shop-items",
                json={**self.ITEM, "text": text},
                headers={"X-CSRF-Token": "csrf-token"})
            self.assertEqual(400, response.status_code, text)


if __name__ == "__main__":
    unittest.main()


class FreeFormVaultTests(DashboardItemTestCase):
    """A custom vault protects the amount that was typed.

    The field used to be a picker over the three built-in vaults and stored
    whichever one's amount was chosen, so "create a new vault" could only ever
    hand out an existing one — which is exactly how it read to the operator. The
    server has always accepted any positive reserve; only the client forbade it.
    """

    def make(self, key, amount, price=1000):
        return {"item_key": key, "template_type": "vault", "category": None,
                "enabled": True, "price": price, "config": {"amount": amount},
                "text": {"name": key, "description": "d"}}

    def test_a_reserve_the_catalog_does_not_have_is_accepted(self):
        self.create_item(self.make("vault_extra", 300000))
        stored = database.get_shop_item_definitions(123)[0]
        self.assertEqual({"amount": 300000}, stored["config"])
        # Not one of the three built-in reserves, which is the whole point.
        import item_catalog
        self.assertNotIn(300000, [definition.value for definition
                                  in item_catalog.VAULT_ITEMS.values()])

    def test_buying_it_protects_exactly_that_amount(self):
        self.create_item(self.make("vault_extra", 300000, price=500))
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (7, 900000)")
            conn.commit()
        result = database.purchase_custom_shop_item(123, 7, "vault_extra")
        self.assertTrue(result["purchased"])
        with database.get_connection() as conn:
            reserve = conn.execute(
                "SELECT protected_reserve FROM users WHERE user_id = 7"
            ).fetchone()[0]
        self.assertEqual(300000, reserve)

    def test_a_smaller_reserve_does_not_downgrade_a_larger_one(self):
        """A vault replaces a lower one; it must not work in reverse."""
        self.create_item(self.make("vault_extra", 300000, price=500))
        self.create_item(self.make("vault_tiny", 25000, price=100))
        with database.get_connection() as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (7, 900000)")
            conn.commit()
        database.purchase_custom_shop_item(123, 7, "vault_extra")
        database.purchase_custom_shop_item(123, 7, "vault_tiny")
        with database.get_connection() as conn:
            reserve = conn.execute(
                "SELECT protected_reserve FROM users WHERE user_id = 7"
            ).fetchone()[0]
        self.assertEqual(300000, reserve)

    def test_zero_and_negative_reserves_are_still_refused(self):
        for amount in (0, -1):
            response = self.client.post(
                "/api/guilds/123/shop-items",
                json=self.make(f"vault_{abs(amount)}x", amount),
                headers={"X-CSRF-Token": "csrf-token"})
            self.assertEqual(400, response.status_code, amount)

    def test_a_gacha_vault_reward_still_awards_the_catalog_reserve(self):
        """Free-form reserves are a *shop* affordance. A banner rewarding a
        catalog vault key must still award that vault's own amount, or the same
        key would mean two different things depending on how it arrived."""
        config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        for entry in config["rewards"]["4"]:
            if entry["key"] == "small_vault":
                entry["amount"] = 999
        with self.assertRaises(database.ValidationError):
            database.set_gacha_banner(123, 42, True, config, 0)


class MechanicPayloadTests(DashboardItemTestCase):
    """The item page has to be able to offer a legal number and only a legal one.

    The bounds travel with the value, because the alternative is the client
    carrying its own copy of `MECHANIC_PARAMETERS` — which is how an interface
    starts rejecting what the API accepts, or offering what it does not.
    """

    def rows(self):
        payload = self.client.get("/api/guilds/123/items?lang=en").get_json()
        return {entry["item_key"]: entry for entry in payload["data"]}

    def test_a_configurable_item_carries_its_bounds_and_its_unit(self):
        import item_catalog

        rows = self.rows()
        for key, parameter in item_catalog.MECHANIC_PARAMETERS.items():
            with self.subTest(item=key):
                mechanic = rows[key]["mechanic"]
                self.assertEqual(parameter.minimum, mechanic["minimum"])
                self.assertEqual(parameter.maximum, mechanic["maximum"])
                self.assertEqual(parameter.unit, mechanic["unit"])
                self.assertEqual(item_catalog.ITEM_DEFINITIONS[key].value,
                                 mechanic["shipped"])
                self.assertEqual(mechanic["shipped"], mechanic["value"])

    def test_an_item_with_no_number_carries_no_mechanic(self):
        rows = self.rows()
        for key in ("loaded_die", "premium", "small_vault", "rent_sound"):
            with self.subTest(item=key):
                self.assertIsNone(rows[key]["mechanic"])

    def test_an_override_is_reflected_in_the_value_but_not_the_shipped(self):
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "shop_item_values",
                               "value": {"parachute": 180}, "revision": 0}]},
            headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(200, response.status_code,
                         response.get_data(as_text=True))
        mechanic = self.rows()["parachute"]["mechanic"]
        self.assertEqual(180, mechanic["value"])
        # `shipped` is what "reset to normal" means, so it must not move.
        self.assertEqual(195, mechanic["shipped"])


class CustomItemAsGachaRewardTests(DashboardItemTestCase):
    """A custom item can be a banner reward, and the page says whether it is.

    The mechanics were never the problem: a banner has always accepted any reward
    key and `_grant_gacha_reward_locked` has always granted it correctly — a
    custom vault really does set the reserve it configures. Three interface gaps
    made it look impossible. The reward picker offered the built-in catalog only,
    so a custom item never appeared and, because a kind with built-in options
    renders a select rather than a text field, its key could not be typed either.
    The member saw `[gacha.rewards.<key>]`, because the locale family covers only
    the shipped rewards. And the item page reported `in_gacha` as a flat False for
    every custom item, so "is my item in the gacha?" was unanswerable.
    """

    def make_vault(self, key="vault_extra", amount=300000, enabled=False):
        return {"item_key": key, "template_type": "vault", "category": None,
                "enabled": enabled, "price": 1, "config": {"amount": amount},
                "text": {"name": "Extra vault", "description": "d"}}

    def make_bundle(self, key="big_bundle", amount=5000, enabled=True):
        # Not repeatable: a repeatable bundle may not pay more than it costs,
        # which is a real anti-inflation guard rather than a fixture detail.
        return {"item_key": key, "template_type": "coin_bundle",
                "category": None, "enabled": enabled, "price": 100,
                "config": {"amount": amount, "repeatable": False},
                "text": {"name": "Big bundle", "description": "d"}}

    def offered(self):
        payload = self.client.get("/api/guilds/123/gacha").get_json()["data"]
        return {entry["key"]: entry for entry in payload["custom_rewards"]}

    def test_an_eligible_custom_item_is_offered_with_its_own_amount(self):
        self.create_item(self.make_vault())
        self.create_item(self.make_bundle())
        offered = self.offered()
        self.assertEqual("vault", offered["vault_extra"]["kind"])
        self.assertEqual(300000, offered["vault_extra"]["amount"])
        self.assertEqual("coins", offered["big_bundle"]["kind"])
        self.assertEqual(5000, offered["big_bundle"]["amount"])
        # Named, because an operator chose the name and does not know the key.
        self.assertEqual("Extra vault", offered["vault_extra"]["name"])

    def test_a_shop_disabled_item_is_still_offered(self):
        """The whole point: in the gacha for a while, never in the shop.
        `enabled` is a shop switch and has no gacha meaning."""
        self.create_item(self.make_vault(enabled=False))
        offered = self.offered()
        self.assertIn("vault_extra", offered)
        self.assertFalse(offered["vault_extra"]["sold_in_shop"])

    def test_a_voucher_and_a_timed_role_are_offered_as_vouchers(self):
        """Both defer their Discord call to redemption, which is how the gacha
        has always handled anything it cannot do inside a pull's transaction —
        premium is a voucher for exactly that reason."""
        self.create_item({
            "item_key": "an_emoji", "template_type": "fulfillment_voucher",
            "category": None, "enabled": True, "price": 100,
            "config": {"asset_type": "emoji", "duration_days": 30},
            "text": {"name": "Emoji", "description": "d"}})
        self.create_item({
            "item_key": "vip_month", "template_type": "timed_role",
            "category": None, "enabled": True, "price": 100,
            "config": {"role_id": 1420070400000000002, "duration_days": 30},
            "text": {"name": "VIP month", "description": "d"}})
        offered = self.offered()
        self.assertEqual("voucher", offered["an_emoji"]["kind"])
        self.assertEqual("voucher", offered["vip_month"]["kind"])
        # A voucher's "amount" is its duration in days, not an `amount` field.
        self.assertEqual(30, offered["an_emoji"]["amount"])
        self.assertEqual(30, offered["vip_month"]["amount"])

    def test_a_permanent_role_is_not_offered(self):
        """Two independent reasons, either sufficient. A reward row requires
        `amount > 0` and a permanent grant has no duration to put there, so it
        would need a magic value. And a permanent role won by chance cannot be
        taken back by any existing pass, so a mis-configured banner would be
        unrecoverable without hand-editing the database — a 3650-day timed role
        is the practical equivalent and *is* revocable."""
        self.create_item({
            "item_key": "vip_forever", "template_type": "fixed_role",
            "category": None, "enabled": True, "price": 100,
            "config": {"role_id": 1420070400000000002},
            "text": {"name": "VIP", "description": "d"}})
        self.assertEqual({}, self.offered())

    def test_a_consumable_is_still_not_offered(self):
        """The grant would create a `user_inventory` row under a key nothing
        consumes."""
        self.create_item({
            "item_key": "a_die", "template_type": "consumable",
            "category": None, "enabled": True, "price": 100,
            "config": {"item_key": "loaded_die"},
            "text": {"name": "Die", "description": "d"}})
        self.assertEqual({}, self.offered())

    def test_the_item_page_says_whether_the_gacha_can_award_it(self):
        self.create_item(self.make_vault())
        self.create_item(self.make_bundle())
        config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        config["rewards"]["5"].append(
            {"key": "vault_extra", "kind": "vault", "amount": 300000,
             "weight": 1})
        database.set_gacha_banner(123, 42, True, config, 0)

        rows = {entry["item_key"]: entry for entry
                in self.client.get("/api/guilds/123/items?lang=en").get_json()["data"]}
        self.assertTrue(rows["vault_extra"]["in_gacha"])
        self.assertFalse(rows["vault_extra"]["in_shop"])
        # And one that is only sold reads the other way round.
        self.assertFalse(rows["big_bundle"]["in_gacha"])
        self.assertTrue(rows["big_bundle"]["in_shop"])

    def test_a_disabled_reward_row_does_not_count_as_obtainable(self):
        self.create_item(self.make_vault())
        config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        config["rewards"]["5"].append(
            {"key": "vault_extra", "kind": "vault", "amount": 300000,
             "weight": 1, "enabled": False})
        database.set_gacha_banner(123, 42, True, config, 0)
        rows = {entry["item_key"]: entry for entry
                in self.client.get("/api/guilds/123/items?lang=en").get_json()["data"]}
        self.assertFalse(rows["vault_extra"]["in_gacha"])


class CustomRewardLabelTests(unittest.TestCase):
    """A custom reward is named, not shown as a bracketed locale key."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "labels.db")
        database.initialize_database()
        database.register_guild(1, "Guild")
        database.create_shop_item_definition(1, 42, {
            "item_key": "vault_extra", "template_type": "vault",
            "category": None, "enabled": False, "price": 1,
            "config": {"amount": 300000},
            "text": {"name": "Extra vault", "description": "d"}})

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_a_custom_key_reads_as_the_operators_own_name(self):
        from cogs.gacha import gacha_reward_label

        self.assertEqual("Extra vault",
                         gacha_reward_label("vault_extra", guild_id=1))

    def test_a_shipped_key_is_never_renamed_by_a_guild(self):
        from cogs.gacha import gacha_reward_label

        label = gacha_reward_label("big_vault", guild_id=1)
        self.assertFalse(label.startswith("["))
        self.assertNotEqual("big_vault", label)

    def test_a_key_with_no_item_and_no_locale_reads_as_itself(self):
        """A banner saved before the item was deleted. The key beats a bracketed
        key, which is what a member used to see."""
        from cogs.gacha import gacha_reward_label

        self.assertEqual("deleted_thing",
                         gacha_reward_label("deleted_thing", guild_id=1))
