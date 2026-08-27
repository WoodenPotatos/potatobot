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
            "item_key": "vip", "template_type": "coin_bundle", "enabled": True,
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

    def test_the_custom_cap_is_reported_so_the_button_can_disable(self):
        payload = self.get()
        self.assertEqual(dashboard_api.SHOP_ITEM_LIMIT, payload["limit"])
        self.assertEqual(0, payload["custom_count"])


class CustomItemTextTests(DashboardItemTestCase):
    """A custom item is written once, in whatever language the guild speaks.

    This briefly took two languages, on the reasoning that every built-in has
    both. Wrong analogy: a built-in ships to every installation and must read in
    each, while a custom item lives in one guild's database and is read only by
    that guild's members. A server with two main languages would use English for
    both rather than keep two columns, so the second field was work with no
    reader.
    """

    ITEM = {"item_key": "vip", "template_type": "coin_bundle", "enabled": True,
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
