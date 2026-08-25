import json
import os
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlparse

import dashboard_api
import settings_cache
import database


class DashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "dashboard.db")
        database.initialize_database()
        database.register_guild(123, "Test Guild")
        dashboard_api.app.config.update(TESTING=True)
        self.client = dashboard_api.app.test_client()
        self.original_oauth = (
            dashboard_api.CLIENT_ID,
            dashboard_api.CLIENT_SECRET,
            dashboard_api.REDIRECT_URI,
        )
        dashboard_api.CLIENT_ID = "client-id"
        dashboard_api.CLIENT_SECRET = "client-secret"
        dashboard_api.REDIRECT_URI = "http://127.0.0.1:5000/api/callback"
        # Host status is re-derived from this per request, not read from the cookie.
        self.original_admin_id = dashboard_api.ADMIN_ID
        dashboard_api.ADMIN_ID = "42"
        # These are process-global, so without a reset a request-heavy test
        # exhausts the rate limit for every test that runs after it.
        dashboard_api._rate_limit_events.clear()
        dashboard_api._oauth_tokens.clear()
        dashboard_api._permission_cache._entries.clear()
        # So is the settings cache, and a settings PATCH here writes into it.
        # Left behind, this guild's values answer for a later test that expected
        # to resolve through `config` — the same discipline `database.DB_PATH`
        # needs, for the same reason.
        settings_cache.invalidate()

    def tearDown(self):
        (
            dashboard_api.CLIENT_ID,
            dashboard_api.CLIENT_SECRET,
            dashboard_api.REDIRECT_URI,
        ) = self.original_oauth
        dashboard_api.ADMIN_ID = self.original_admin_id
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def authenticate(self, csrf_token="csrf-token", user_id="42"):
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["user_id"] = user_id
            session["display"] = {"username": "tester", "avatar": None}
            session["csrf_token"] = csrf_token
            session["server_session_id"] = "server-session"
            session["authorized_guild_ids"] = ["123"]
            # The absolute-lifetime gate expires a session with no recorded
            # login instant, so a fabricated one has to carry it too.
            session["authenticated_at"] = time.time()

    def default_banner(self) -> dict:
        """The guild's default gacha banner out of the banner list.

        The endpoint returns every banner a guild has, because a guild may run
        several; the tests here exercise the default one.
        """
        banners = self.client.get("/api/guilds/123/gacha").get_json()["data"]
        return next(banner for banner in banners if banner["is_default"])

    def test_login_uses_state_and_exact_redirect_uri(self):
        response = self.client.get("/api/auth/login")
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.location).query)
        self.assertEqual(query["redirect_uri"], [dashboard_api.REDIRECT_URI])
        self.assertEqual(query["scope"], ["identify guilds"])
        self.assertTrue(query["state"][0])
        with self.client.session_transaction() as session:
            self.assertEqual(session["oauth_state"], query["state"][0])

    def test_callback_rejects_missing_or_mismatched_state_before_network(self):
        response = self.client.get("/api/callback?code=unused&state=wrong")
        self.assertEqual(response.status_code, 400)

    def test_mutation_requires_csrf(self):
        self.authenticate()
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "shop_price_premium", "value": 1, "revision": 0}]},
        )
        self.assertEqual(response.status_code, 403)

    def test_typed_settings_reject_unknown_or_negative_values(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        unknown = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "unknown", "value": 1, "revision": 0}]},
            headers=headers,
        )
        negative = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "shop_price_premium", "value": -1, "revision": 0}]},
            headers=headers,
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(negative.status_code, 400)

    def test_logout_is_post_only_and_clears_session(self):
        self.authenticate()
        self.assertIn(self.client.get("/api/auth/logout").status_code, {404, 405})
        response = self.client.post(
            "/api/auth/logout", json={}, headers={"X-CSRF-Token": "csrf-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/api/auth/status").get_json()["logged_in"])

    def test_guild_feature_endpoint_checks_scope_and_revision(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        forbidden = self.client.get("/api/guilds/999/features")
        self.assertEqual(forbidden.status_code, 401)

        response = self.client.post(
            "/api/guilds/123/features",
            json={"feature_key": "social_twitch", "enabled": False, "revision": 0},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        stale = self.client.post(
            "/api/guilds/123/features",
            json={"feature_key": "social_twitch", "enabled": True, "revision": 0},
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409)

    def test_host_can_create_realm_and_guild_admin_can_request_membership(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        created = self.client.post(
            "/api/realms", json={"name": "Trusted Guilds"}, headers=headers
        )
        self.assertEqual(created.status_code, 201)
        realm_id = created.get_json()["data"]["realm_id"]
        requested = self.client.post(
            f"/api/realms/{realm_id}/memberships",
            json={"guild_id": 123},
            headers=headers,
        )
        self.assertEqual(requested.status_code, 200)

    def test_guild_scope_endpoint_preserves_optimistic_revision(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        changed = self.client.post(
            "/api/guilds/123/data-scopes",
            json={
                "category": "profile", "scope_type": "instance",
                "realm_id": None, "revision": 0,
            },
            headers=headers,
        )
        self.assertEqual(changed.status_code, 200)
        stale = self.client.post(
            "/api/guilds/123/data-scopes",
            json={
                "category": "profile", "scope_type": "guild",
                "realm_id": None, "revision": 0,
            },
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409)

    def test_typed_settings_and_gacha_use_revision_checks(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        registry = self.client.get("/api/settings/registry").get_json()["data"]
        self.assertIn("join_channel", registry)
        self.assertNotIn("DISCORD_TOKEN", registry)
        changed = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "join_channel", "value": 456, "revision": 0}]},
            headers=headers,
        )
        self.assertEqual(changed.status_code, 200)
        stale = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "join_channel", "value": 789, "revision": 0}]},
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409)

        banner = self.default_banner()
        banner["config"]["soft_pity_start"] = 76
        saved = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "config": banner["config"], "revision": 0},
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200)
        invalid = dict(banner["config"])
        invalid["tiers"] = {"3": 1, "4": 1, "5": 1}
        rejected = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "config": invalid, "revision": 1},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 400)

    # ---------------------------------------------------------- validation

    def _headers(self):
        self.authenticate()
        return {"X-CSRF-Token": "csrf-token"}

    def test_malformed_field_types_return_400_not_500(self):
        """A wrong value type used to reach int() or a dict lookup in the model
        layer and escape as an unhandled 500."""
        headers = self._headers()
        cases = [
            ("post", "/api/guilds/123/features",
             {"feature_key": "economy", "enabled": True, "revision": {}}),
            ("post", "/api/guilds/123/features",
             {"feature_key": [], "enabled": True, "revision": 0}),
            ("post", "/api/guilds/123/features",
             {"feature_key": "economy", "enabled": "yes", "revision": 0}),
            ("patch", "/api/guilds/123/settings", {"changes": "not-a-list"}),
            ("patch", "/api/guilds/123/settings", {"changes": [{"key": {}, "value": 1, "revision": 0}]}),
            ("patch", "/api/guilds/123/gacha",
             {"enabled": True, "config": "not-an-object", "revision": 0}),
            ("patch", "/api/guilds/123/gacha",
             {"enabled": True, "config": {}, "revision": None}),
            ("post", "/api/guilds/123/data-scopes",
             {"category": {}, "scope_type": "guild", "realm_id": None, "revision": 0}),
        ]
        for method, path, payload in cases:
            with self.subTest(path=path, payload=payload):
                response = getattr(self.client, method)(path, json=payload, headers=headers)
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assertTrue(response.get_json()["message"])

    def test_rejections_explain_themselves(self):
        headers = self._headers()
        generic = dashboard_api.t("dashboard.invalid_request")
        response = self.client.post(
            "/api/guilds/123/shop-items",
            json={"item_key": "premium", "template_type": "fixed_role", "enabled": True,
                  "price": 100, "config": {"role_id": 5}, "hu": {"name": "a", "description": "b"}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"],
                         dashboard_api.t("dashboard.errors.shop_key_reserved"))
        self.assertNotEqual(response.get_json()["message"], generic)

    def test_builtin_shop_keys_cannot_be_taken_over(self):
        headers = self._headers()
        for key in sorted(database.BUILTIN_SHOP_KEYS):
            with self.subTest(item_key=key):
                response = self.client.post(
                    "/api/guilds/123/shop-items",
                    json={"item_key": key, "template_type": "vault", "enabled": True,
                          "price": 100, "config": {"amount": 1000},
                          "hu": {"name": "a", "description": "b"}},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400)

    def test_embed_fields_and_colour_are_validated_before_queueing(self):
        headers = self._headers()
        rejected = [
            {"fields": ["not-a-dict"]},
            {"fields": [{"name": "a", "unexpected": 1}]},
            {"color": "red"},
            {"color": 0x1000000},
            {"color": True},
        ]
        for content in rejected:
            with self.subTest(content=content):
                response = self.client.post(
                    "/api/guilds/123/builders",
                    json={"document_type": "embed", "name": "draft",
                          "content": content, "revision": 0},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        accepted = self.client.post(
            "/api/guilds/123/builders",
            json={"document_type": "embed", "name": "draft",
                  "content": {"color": 0xF5B041, "fields": [{"name": "a", "value": "b"}]},
                  "revision": 0},
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_data(as_text=True))

    def test_fulfillment_identifier_is_length_bounded(self):
        headers = self._headers()
        response = self.client.post(
            "/api/guilds/123/fulfillment/1",
            json={"discord_item_id": "1" * 5000},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"],
                         dashboard_api.t("dashboard.errors.discord_item_id_too_long"))

    def test_shop_item_audit_commits_with_the_item(self):
        headers = self._headers()
        created = self.client.post(
            "/api/guilds/123/shop-items",
            json={"item_key": "vip_role", "template_type": "vault", "enabled": True,
                  "price": 100, "config": {"amount": 1000},
                  "hu": {"name": "Vip", "description": "leiras"}},
            headers=headers,
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        with database.get_connection() as conn:
            audited = conn.execute(
                "SELECT COUNT(*) FROM settings_audit WHERE guild_id = 123 "
                "AND action = 'shop_item.create' AND target_key = 'vip_role'"
            ).fetchone()[0]
        self.assertEqual(audited, 1)

    def test_security_headers_include_the_full_csp(self):
        response = self.client.get("/api/locale")
        policy = response.headers["Content-Security-Policy"]
        for directive in ("connect-src 'self'", "base-uri 'none'",
                          "form-action 'self'", "object-src 'none'",
                          "frame-ancestors 'none'"):
            self.assertIn(directive, policy)

    # ------------------------------------------------- edit / disable flows

    def _create_item(self, headers, key="vip_role", price=100):
        return self.client.post(
            "/api/guilds/123/shop-items",
            json={"item_key": key, "template_type": "vault", "enabled": True,
                  "price": price, "config": {"amount": 1000},
                  "hu": {"name": "Vip", "description": "leiras"}},
            headers=headers,
        )

    def test_shop_item_can_be_edited_disabled_and_deleted(self):
        headers = self._headers()
        self.assertEqual(self._create_item(headers).status_code, 201)

        stored = self.client.get("/api/guilds/123/shop-items").get_json()["data"][0]
        self.assertEqual(stored["revision"], 1)

        disabled = self.client.patch(
            "/api/guilds/123/shop-items/vip_role",
            json={"template_type": "vault", "enabled": False, "price": 250,
                  "config": {"amount": 2000},
                  "hu": {"name": "Vip 2", "description": "uj"}, "revision": 1},
            headers=headers,
        )
        self.assertEqual(disabled.status_code, 200, disabled.get_data(as_text=True))

        updated = self.client.get("/api/guilds/123/shop-items").get_json()["data"][0]
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["price"], 250)
        self.assertEqual(updated["name"], "Vip 2")
        self.assertEqual(updated["revision"], 2)

        stale = self.client.patch(
            "/api/guilds/123/shop-items/vip_role",
            json={"template_type": "vault", "enabled": True, "price": 300,
                  "config": {"amount": 2000},
                  "hu": {"name": "x", "description": "y"}, "revision": 1},
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409)

        removed = self.client.delete(
            "/api/guilds/123/shop-items/vip_role", json={"revision": 2}, headers=headers,
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/guilds/123/shop-items").get_json()["data"], [])

    def test_shop_item_edit_cannot_widen_an_approved_template(self):
        headers = self._headers()
        self._create_item(headers)
        response = self.client.patch(
            "/api/guilds/123/shop-items/vip_role",
            json={"template_type": "arbitrary_code", "enabled": True, "price": 1,
                  "config": {}, "hu": {"name": "a", "description": "b"}, "revision": 1},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_shop_item_key_is_immutable_and_builtins_are_not_addressable(self):
        headers = self._headers()
        self._create_item(headers)
        renamed = self.client.patch(
            "/api/guilds/123/shop-items/vip_role",
            json={"item_key": "other", "template_type": "vault", "enabled": True,
                  "price": 1, "config": {"amount": 1},
                  "hu": {"name": "a", "description": "b"}, "revision": 1},
            headers=headers,
        )
        self.assertEqual(renamed.status_code, 400)
        builtin = self.client.delete(
            "/api/guilds/123/shop-items/premium", json={"revision": 1}, headers=headers,
        )
        self.assertEqual(builtin.status_code, 400)

    def test_missing_shop_item_returns_404(self):
        headers = self._headers()
        response = self.client.delete(
            "/api/guilds/123/shop-items/nope", json={"revision": 1}, headers=headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_builder_draft_can_be_deleted_with_its_revision(self):
        headers = self._headers()
        created = self.client.post(
            "/api/guilds/123/builders",
            json={"document_type": "embed", "name": "draft",
                  "content": {"title": "hello"}, "revision": 0},
            headers=headers,
        )
        self.assertEqual(created.status_code, 201)
        document = self.client.get("/api/guilds/123/builders").get_json()["data"][0]

        stale = self.client.delete(
            f"/api/guilds/123/builders/{document['document_id']}",
            json={"revision": document["revision"] + 5}, headers=headers,
        )
        self.assertEqual(stale.status_code, 409)

        removed = self.client.delete(
            f"/api/guilds/123/builders/{document['document_id']}",
            json={"revision": document["revision"]}, headers=headers,
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/guilds/123/builders").get_json()["data"], [])

    def test_action_status_is_readable_and_guild_scoped(self):
        headers = self._headers()
        action_id = database.queue_control_action(
            123, 42, "send_embed", {"document_id": 1, "channel_id": 2}
        )
        response = self.client.get(f"/api/guilds/123/actions/{action_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "pending")

        database.register_guild(456, "Other Guild")
        with self.client.session_transaction() as session:
            session["authorized_guild_ids"] = ["123", "456"]
        other = self.client.get(f"/api/guilds/456/actions/{action_id}")
        self.assertEqual(other.status_code, 404)
        self.assertTrue(headers)

    def test_settled_actions_are_pruned_and_live_ones_are_kept(self):
        # The worker claims the oldest pending row, so settle that one and leave
        # the newer one queued.
        done = database.queue_control_action(123, 42, "send_embed", {})
        pending = database.queue_control_action(123, 42, "send_embed", {})
        claimed = database.claim_control_action()
        self.assertEqual(claimed["action_id"], done)
        database.finish_control_action(done, True)
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE control_actions SET completed_at = '2000-01-01T00:00:00+00:00' "
                "WHERE action_id = ?", (done,),
            )
        self.assertEqual(database.prune_control_actions(30), 1)
        with database.get_connection() as conn:
            remaining = [
                row[0] for row in conn.execute("SELECT action_id FROM control_actions")
            ]
        self.assertEqual(remaining, [pending])

    def test_permission_cache_and_oauth_tokens_stay_bounded(self):
        cache = dashboard_api.TtlCache(ttl=60, max_entries=8)
        for index in range(200):
            cache.put(f"session-{index}", ["123"])
        self.assertLessEqual(len(cache._entries), 8)

        cache.put("kept", ["123"])
        self.assertEqual(cache.get("kept"), ["123"])
        cache.forget("kept")
        self.assertIsNone(cache.get("kept"))

        expired = dashboard_api.TtlCache(ttl=0, max_entries=8)
        expired.put("stale", ["123"])
        self.assertIsNone(expired.get("stale"))

    def test_logout_drops_all_server_held_session_state(self):
        headers = self._headers()
        dashboard_api._oauth_tokens["server-session"] = {
            "access_token": "a", "refresh_token": "r", "expires_at": 2 ** 31,
        }
        dashboard_api._permission_cache.put("server-session", ["123"])
        self.client.post("/api/auth/logout", json={}, headers=headers)
        self.assertNotIn("server-session", dashboard_api._oauth_tokens)
        self.assertIsNone(dashboard_api._permission_cache.get("server-session"))

    def test_host_status_is_rederived_not_read_from_the_cookie(self):
        self.authenticate(user_id="42")
        self.assertTrue(self.client.get("/api/guilds").status_code == 200)
        # The configured host changes; the existing cookie must lose its authority.
        dashboard_api.ADMIN_ID = "999"
        with dashboard_api.app.test_request_context():
            pass
        self.authenticate(user_id="42")
        with self.client.session_transaction() as session:
            session["authorized_guild_ids"] = []
        response = self.client.post(
            "/api/guilds/123/features",
            json={"feature_key": "economy", "enabled": False, "revision": 0},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_disabled_shop_rows_count_against_the_cap(self):
        headers = self._headers()
        for index in range(dashboard_api.SHOP_ITEM_LIMIT):
            created = self._create_item(headers, key=f"item_{index}")
            self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
            # Disable it, so only the total row count can enforce the cap.
            disabled = self.client.patch(
                f"/api/guilds/123/shop-items/item_{index}",
                json={"template_type": "vault", "enabled": False, "price": 100,
                      "config": {"amount": 1000},
                      "hu": {"name": "a", "description": "b"}, "revision": 1},
                headers=headers,
            )
            self.assertEqual(disabled.status_code, 200)

        overflow = self._create_item(headers, key="one_too_many")
        self.assertEqual(overflow.status_code, 400)
        self.assertEqual(
            overflow.get_json()["message"],
            dashboard_api.t("dashboard.errors.shop_item_limit",
                            limit=dashboard_api.SHOP_ITEM_LIMIT),
        )

    def test_model_layer_rejections_name_their_reason(self):
        """database.py validators carry a reason code that maps to a locale key,
        so gacha and settings errors are no longer one generic message."""
        headers = self._headers()
        generic = dashboard_api.t("dashboard.invalid_request")
        banner = self.default_banner()

        cases = [
            ({"tiers": {"3": 1, "4": 1, "5": 1}}, "gacha_tier_total"),
            ({"tiers": {"3": 20000, "4": 40000, "5": 40000}}, "gacha_soft_pity_overflow"),
            ({"soft_pity_multiplier": 99}, "gacha_multiplier_range"),
            ({"duplicate_percent": 500}, "gacha_duplicate_percent_range"),
        ]
        for override, reason in cases:
            with self.subTest(reason=reason):
                config = dict(banner["config"])
                config.update(override)
                response = self.client.patch(
                    "/api/guilds/123/gacha",
                    json={"enabled": True, "config": config, "revision": 0},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400)
                message = response.get_json()["message"]
                self.assertEqual(message, dashboard_api.t(f"dashboard.errors.{reason}"))
                self.assertNotEqual(message, generic)

    def test_every_model_reason_code_has_a_locale_key(self):
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "database.py"), encoding="utf-8") as handle:
            source = handle.read()
        reasons = sorted(set(re.findall(r'ValidationError\("([a-z_]+)"', source)))
        self.assertTrue(reasons)
        missing = [
            reason for reason in reasons
            if dashboard_api.t(f"dashboard.errors.{reason}").startswith("[")
        ]
        self.assertEqual([], missing)

    def test_every_setting_label_resolves_in_the_hungarian_catalog(self):
        """Setting forms render `SettingDefinition.locale_key` directly, so a
        registry entry without a translation shows a raw key to the operator.
        Adding a built-in shop item creates a price setting automatically, which
        is exactly the case that would slip through unnoticed."""
        from settings_registry import SETTING_DEFINITIONS

        missing = sorted(
            definition.key for definition in SETTING_DEFINITIONS.values()
            if dashboard_api.t(definition.locale_key).startswith("[")
        )
        self.assertEqual([], missing)

    def test_item_catalog_requires_a_session_and_lists_the_shared_items(self):
        import item_catalog

        self.assertEqual(self.client.get("/api/item-catalog").status_code, 401)
        self.authenticate()
        payload = self.client.get("/api/item-catalog").get_json()["data"]
        self.assertEqual(
            {entry["key"] for entry in payload}, set(item_catalog.ITEM_DEFINITIONS)
        )
        # Identity and defaults only; nothing guild-specific or secret.
        self.assertEqual(
            set(payload[0]),
            {"key", "effect", "value", "default_price", "sold_in_shop", "gacha_kind"},
        )

    def test_a_reward_can_be_added_to_an_already_saved_banner(self):
        """A guild that saved a banner keeps its own config, so a newly shipped
        default reward can only reach it if the dashboard can add rows."""
        headers = self._headers()
        banner = self.default_banner()
        config = banner["config"]
        config["rewards"]["4"] = [
            entry for entry in config["rewards"]["4"] if entry["key"] != "med_vault"
        ]
        saved = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "config": config, "revision": 0},
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200)
        stored = self.default_banner()
        self.assertNotIn(
            "med_vault", [entry["key"] for entry in stored["config"]["rewards"]["4"]]
        )

        stored["config"]["rewards"]["4"].append(
            {"key": "med_vault", "kind": "vault", "amount": 100000,
             "weight": 1, "enabled": True}
        )
        added = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "config": stored["config"],
                  "revision": stored["revision"]},
            headers=headers,
        )
        self.assertEqual(added.status_code, 200)
        final = self.default_banner()
        self.assertIn(
            "med_vault", [entry["key"] for entry in final["config"]["rewards"]["4"]]
        )
        # A stale revision must still lose, so adding a row is not a way around
        # the optimistic check.
        conflicted = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "config": final["config"], "revision": 0},
            headers=headers,
        )
        self.assertEqual(conflicted.status_code, 409)

    def test_a_banner_cannot_redefine_a_shared_vault_or_empty_a_tier(self):
        headers = self._headers()
        banner = self.default_banner()

        mismatched = json.loads(json.dumps(banner["config"]))
        mismatched["rewards"]["5"] = [
            {"key": "big_vault", "kind": "vault", "amount": 1, "weight": 1}
        ]
        response = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "config": mismatched, "revision": 0},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["message"],
            dashboard_api.t("dashboard.errors.gacha_vault_amount_mismatch",
                            item="big_vault", amount=500000),
        )

        emptied = json.loads(json.dumps(banner["config"]))
        for entry in emptied["rewards"]["3"]:
            entry["enabled"] = False
        self.assertEqual(
            self.client.patch(
                "/api/guilds/123/gacha",
                json={"enabled": True, "config": emptied, "revision": 0},
                headers=headers,
            ).status_code,
            400,
        )

    def test_custom_consumable_items_are_validated_against_the_catalog(self):
        import item_catalog

        headers = self._headers()
        for item_key in sorted(item_catalog.INVENTORY_ITEM_KEYS):
            with self.subTest(item_key=item_key):
                response = self.client.post(
                    "/api/guilds/123/shop-items",
                    json={
                        "item_key": f"custom_{item_key}", "template_type": "consumable",
                        "enabled": True, "price": 100,
                        "config": {"item_key": item_key},
                        "hu": {"name": "Teszt", "description": "Teszt"},
                    },
                    headers=headers,
                )
                self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

        rejected = self.client.post(
            "/api/guilds/123/shop-items",
            json={
                "item_key": "custom_unknown", "template_type": "consumable",
                "enabled": True, "price": 100,
                "config": {"item_key": "big_vault"},
                "hu": {"name": "Teszt", "description": "Teszt"},
            },
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 400)

    def test_a_setting_save_no_longer_writes_the_legacy_file(self):
        """There is nothing left to reconcile, because nothing diverges.

        `set_guild_settings` committed to SQLite and only then wrote
        `config.json`, so an OSError on that write left the two permanently
        apart and startup had to replay every committed setting to repair it.
        The file is a read-only fallback now: SQLite is the only authority, and a
        save has one destination.
        """
        from unittest.mock import patch

        import cogs.utils

        writes = []
        with patch.object(cogs.utils, "save_config",
                          lambda data: writes.append(data)):
            self.authenticate(user_id="42")
            response = self.client.patch(
                "/api/guilds/123/settings",
                json={"changes": [{"key": "shop_price_premium",
                                   "value": 777, "revision": 0}]},
                headers={"X-CSRF-Token": "csrf-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([], writes, "a settings save must not write config.json")
        # And the row it *does* own is written.
        self.assertEqual(
            777, database.get_guild_settings(123)["shop_price_premium"]["value"])

    def test_only_the_host_may_erase_a_member(self):
        """Erasure spans the whole installation, so a single guild's administrator
        cannot authorize it however many guilds they manage."""
        self.authenticate(user_id="7")          # a guild admin, not the host
        response = self.client.post(
            "/api/guilds/123/privacy/erasures",
            json={"user_id": "555", "confirm": True},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(response.status_code, 401)
        with database.get_connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM control_actions").fetchone()[0], 0
            )

    def test_erasure_requires_the_csrf_header(self):
        self.authenticate()
        response = self.client.post(
            "/api/guilds/123/privacy/erasures",
            json={"user_id": "555", "confirm": True},
        )
        self.assertEqual(response.status_code, 403)

    def test_the_host_queues_an_erasure_for_the_bot_to_execute(self):
        """Only the bot can withdraw a Discord grant, so the route enqueues."""
        self.authenticate()
        response = self.client.post(
            "/api/guilds/123/privacy/erasures",
            json={"user_id": "555", "confirm": True},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(response.status_code, 200)
        action_id = response.get_json()["data"]["action_id"]
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT action_type, actor_id, payload_json, status "
                "FROM control_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        self.assertEqual(row[0], "erase_member")
        self.assertEqual(row[1], 42)
        self.assertEqual(json.loads(row[2]), {"user_id": 555})
        self.assertEqual(row[3], "pending")

    def test_erasure_rejects_an_unconfirmed_or_malformed_subject(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        for payload in (
            {"user_id": "555", "confirm": False},
            {"user_id": "not-a-snowflake", "confirm": True},
            {"user_id": "0", "confirm": True},
            {"user_id": "-1", "confirm": True},
            {"user_id": "555"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/guilds/123/privacy/erasures", json=payload, headers=headers
                )
                self.assertEqual(response.status_code, 400)
        with database.get_connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM control_actions").fetchone()[0], 0
            )

    def test_one_guilds_price_edit_cannot_change_another_guilds_price(self):
        """Before schema 8 these tables had no guild dimension, so any guild's
        PATCH rewrote the whole installation's prices."""
        from unittest.mock import patch

        database.register_guild(456, "Other Guild")
        # Calls the surviving half directly. The mirror wrapper that used to
        # call it is gone; these rows are not the mirror.
        dashboard_api._mirror_price_and_reward_tables(
            123, {"shop_price_premium": 4242})

        self.assertEqual(
            database.get_shop_price(123, "premium", 0), 4242,
        )
        # The other guild and the installation default are untouched.
        self.assertEqual(
            database.get_shop_price(456, "premium", 0),
            database.SHOP_DEFAULTS["premium"],
        )
        self.assertEqual(
            database.get_shop_price(0, "premium", 0),
            database.SHOP_DEFAULTS["premium"],
        )

    def test_a_reward_override_keeps_the_sibling_column(self):
        """Patching only the coin value must not blank the XP value."""
        dashboard_api._mirror_price_and_reward_tables(
            123, {"reward_daily_normal_coin": 12345})

        default_coin, default_xp = database.REWARD_DEFAULTS["daily_normal"]
        self.assertEqual(
            database.get_reward(123, "daily_normal", 0, 0), (12345, default_xp)
        )
        self.assertEqual(
            database.get_reward(456, "daily_normal", 0, 0),
            (default_coin, default_xp),
        )

    def test_revoked_oauth_grant_ends_the_session(self):
        """Authorization loss: the refresh token no longer works, so a mutation
        must stop being authorized rather than ride the cookie."""
        from unittest.mock import patch

        self.authenticate(user_id="7")          # not the host
        dashboard_api._oauth_tokens["server-session"] = {
            "access_token": "expired", "refresh_token": "revoked", "expires_at": 0,
        }
        with patch.object(dashboard_api.requests, "post") as post:
            post.return_value.raise_for_status.side_effect = \
                dashboard_api.requests.RequestException("invalid_grant")
            response = self.client.post(
                "/api/guilds/123/features",
                json={"feature_key": "economy", "enabled": False, "revision": 0},
                headers={"X-CSRF-Token": "csrf-token"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("server-session", dashboard_api._oauth_tokens)
        self.assertFalse(self.client.get("/api/auth/status").get_json()["logged_in"])

    def test_discord_outage_refuses_mutations_without_dropping_the_session(self):
        """An outage is not an authorization failure, so answer 503 and keep the
        session; the operator can retry once Discord recovers."""
        from unittest.mock import patch

        self.authenticate(user_id="7")
        dashboard_api._oauth_tokens["server-session"] = {
            "access_token": "live", "refresh_token": "r", "expires_at": 2 ** 31,
        }
        with patch.object(dashboard_api.requests, "get",
                          side_effect=dashboard_api.requests.RequestException("timeout")):
            response = self.client.post(
                "/api/guilds/123/features",
                json={"feature_key": "economy", "enabled": False, "revision": 0},
                headers={"X-CSRF-Token": "csrf-token"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertTrue(self.client.get("/api/auth/status").get_json()["logged_in"])

    def test_permission_snapshot_is_reused_within_its_window(self):
        """A burst of saves must not mean one blocking Discord call each."""
        from unittest.mock import patch

        self.authenticate(user_id="7")
        dashboard_api._oauth_tokens["server-session"] = {
            "access_token": "live", "refresh_token": "r", "expires_at": 2 ** 31,
        }
        payload = [{"id": "123", "owner": True, "permissions": "8"}]
        with patch.object(dashboard_api.requests, "get") as get:
            get.return_value.json.return_value = payload
            get.return_value.raise_for_status.return_value = None
            for _ in range(4):
                self.client.post(
                    "/api/guilds/123/features",
                    json={"feature_key": "economy", "enabled": False, "revision": 0},
                    headers={"X-CSRF-Token": "csrf-token"},
                )
            self.assertEqual(get.call_count, 1, "permission snapshot was not cached")

    def test_revision_conflicts_are_typed_not_message_matched(self):
        """Renaming a database error message must not turn a 409 into a 500."""
        self.assertTrue(issubclass(database.RevisionConflictError,
                                   database.DatabaseOperationError))
        source = (
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "dashboard_api.py")
        )
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn('"revision conflict" in str', text)

    def test_session_cookie_expires_after_ten_idle_minutes(self):
        """The idle window is the cookie's own lifetime, refreshed per request."""
        self.assertEqual(
            dashboard_api.SESSION_IDLE_TIMEOUT.total_seconds(), 600
        )
        self.assertEqual(
            dashboard_api.app.config["PERMANENT_SESSION_LIFETIME"],
            dashboard_api.SESSION_IDLE_TIMEOUT,
        )
        # Without this Flask only sets the cookie when the session changes, so
        # reading a page would not slide the window forward.
        self.assertTrue(dashboard_api.app.config["SESSION_REFRESH_EACH_REQUEST"])

    def test_absolute_lifetime_ends_a_session_that_is_kept_warm(self):
        """A sliding window alone would let a session live indefinitely."""
        self.authenticate()
        with self.client.session_transaction() as session:
            session["authenticated_at"] = (
                time.time() - dashboard_api.SESSION_LIFETIME.total_seconds() - 1
            )
        response = self.client.get("/api/guilds/123/settings")
        self.assertEqual(response.status_code, 401)
        with self.client.session_transaction() as session:
            self.assertNotIn("logged_in", session)

    def test_session_without_a_recorded_login_instant_is_expired(self):
        """A cookie issued before the cap existed must not be trusted forever."""
        self.authenticate()
        with self.client.session_transaction() as session:
            del session["authenticated_at"]
        self.assertEqual(
            self.client.get("/api/guilds/123/settings").status_code, 401
        )

    def test_changelog_requires_a_session_and_arrives_parsed(self):
        """The front end may not parse markdown, so the API has to."""
        self.assertEqual(self.client.get("/api/changelog").status_code, 401)
        self.authenticate()
        dashboard_api._changelog_cache._entries.clear()
        response = self.client.get("/api/changelog")
        self.assertEqual(response.status_code, 200)
        releases = response.get_json()["data"]
        self.assertTrue(releases)
        self.assertTrue(all(release["version"] for release in releases))
        self.assertTrue(all(release["entries"] for release in releases))

    def test_changelog_rejoins_a_bullet_wrapped_across_source_lines(self):
        parsed = dashboard_api._parse_changelog(
            "# Changelog\n\n## 1.2.3 - 2026-01-01\n\n"
            "- First entry that continues\n  onto a second line.\n"
            "- Second entry.\n"
        )
        self.assertEqual(1, len(parsed))
        self.assertEqual("1.2.3", parsed[0]["version"])
        self.assertEqual("2026-01-01", parsed[0]["label"])
        self.assertEqual(
            ["First entry that continues onto a second line.", "Second entry."],
            parsed[0]["entries"],
        )

    def test_banner_routes_create_edit_and_delete_under_revision_checks(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        created = self.client.post(
            "/api/guilds/123/gacha/banners",
            json={"banner_key": "summer", "display_name": "Summer"},
            headers=headers,
        )
        self.assertEqual(created.status_code, 201)
        # A new banner starts disabled so a half-filled table is never pullable.
        self.assertFalse(created.get_json()["data"]["enabled"])

        banners = self.client.get("/api/guilds/123/gacha").get_json()["data"]
        summer = next(item for item in banners if item["banner_key"] == "summer")
        self.assertEqual("Summer", summer["display_name"])
        self.assertFalse(summer["is_default"])

        conflict = self.client.delete(
            "/api/guilds/123/gacha/banners/summer",
            json={"revision": 99}, headers=headers,
        )
        self.assertEqual(conflict.status_code, 409)
        removed = self.client.delete(
            "/api/guilds/123/gacha/banners/summer",
            json={"revision": summer["revision"]}, headers=headers,
        )
        self.assertEqual(removed.status_code, 200)

    def test_banner_routes_reject_an_unknown_key_and_a_blank_name(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        self.assertEqual(400, self.client.post(
            "/api/guilds/123/gacha/banners",
            json={"banner_key": "Summer Banner!", "display_name": "x"},
            headers=headers,
        ).status_code)
        self.assertEqual(400, self.client.post(
            "/api/guilds/123/gacha/banners",
            json={"banner_key": "summer", "display_name": "  "},
            headers=headers,
        ).status_code)
        # Saving a banner that was never created must be refused, not created.
        banner = self.default_banner()
        self.assertEqual(400, self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "config": banner["config"], "revision": 0,
                  "banner_key": "never_created"},
            headers=headers,
        ).status_code)

    def test_work_response_routes_round_trip_and_stay_typed(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        # A guild with no rows of its own still sees the installation defaults,
        # tagged so the interface can show them without offering to edit them.
        seeded = self.client.get("/api/guilds/123/work-responses").get_json()["data"]
        self.assertEqual(["normal", "free", "high"], seeded["tiers"])
        self.assertTrue(seeded["responses"])
        self.assertEqual({"default"},
                         {row["scope"] for row in seeded["responses"]})

        created = self.client.post(
            "/api/guilds/123/work-responses",
            json={"tier": "high", "message": "Paid {earnings}", "weight": 4},
            headers=headers,
        )
        self.assertEqual(created.status_code, 201)
        response_id = created.get_json()["data"]["response_id"]

        # A wrongly typed field must be a 400, not an unhandled 500 from int().
        self.assertEqual(400, self.client.post(
            "/api/guilds/123/work-responses",
            json={"tier": "high", "message": "x", "weight": "many"},
            headers=headers,
        ).status_code)
        self.assertEqual(400, self.client.post(
            "/api/guilds/123/work-responses",
            json={"tier": 7, "message": "x"}, headers=headers,
        ).status_code)

        updated = self.client.patch(
            f"/api/guilds/123/work-responses/{response_id}",
            json={"tier": "high", "message": "Edited", "weight": 2,
                  "enabled": False, "revision": 1},
            headers=headers,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(409, self.client.patch(
            f"/api/guilds/123/work-responses/{response_id}",
            json={"tier": "high", "message": "Again", "weight": 2,
                  "enabled": True, "revision": 1},
            headers=headers,
        ).status_code)
        self.assertEqual(404, self.client.delete(
            "/api/guilds/123/work-responses/999999",
            json={"revision": 1}, headers=headers,
        ).status_code)
        self.assertEqual(200, self.client.delete(
            f"/api/guilds/123/work-responses/{response_id}",
            json={"revision": 2}, headers=headers,
        ).status_code)

    def test_permission_report_reports_its_own_unavailability(self):
        """Without the in-process bot it cannot read overwrites or hierarchy, so
        it must say so rather than return a clean result it never checked."""
        self.authenticate()
        self.assertIsNone(dashboard_api._dashboard_bot)
        response = self.client.get("/api/guilds/123/permissions")
        self.assertEqual(response.status_code, 503)

    def test_new_routes_require_authorization_and_csrf(self):
        for path in ("/api/guilds/123/work-responses",
                     "/api/guilds/123/permissions"):
            with self.subTest(path=path):
                self.assertEqual(401, self.client.get(path).status_code)
        # A mutation is stopped at the CSRF gate first, which runs before the
        # route's own authorization check, so an anonymous POST never reaches it.
        for path, body in (
            ("/api/guilds/123/gacha/banners",
             {"banner_key": "x", "display_name": "x"}),
            ("/api/guilds/123/work-responses", {"tier": "normal", "message": "x"}),
        ):
            with self.subTest(path=path):
                self.assertEqual(403, self.client.post(path, json=body).status_code)
        self.authenticate()
        # Authorized, but still without the session-bound token.
        self.assertEqual(403, self.client.post(
            "/api/guilds/123/work-responses",
            json={"tier": "normal", "message": "x"},
        ).status_code)

    def test_login_records_the_absolute_lifetime_start(self):
        """The cap is measured from login, so the callback has to record it."""
        with open(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "dashboard_api.py",
            ),
            encoding="utf-8",
        ) as handle:
            text = handle.read()
        self.assertIn('session["authenticated_at"] = time.time()', text)


if __name__ == "__main__":
    unittest.main()


class SnowflakeWireFormatTests(unittest.TestCase):
    """A Discord id must survive the round trip through the browser.

    A snowflake is 64-bit and a JavaScript number holds 53 bits exactly, so
    `Number("1420070400000000001")` is ...200. Sending ids as JSON numbers meant
    every channel and role saved from the dashboard was written back rounded: the
    id then matched no channel, the selector showed it as unavailable, and the
    real setting was lost. Ids therefore cross the wire as strings, the way
    Discord's own API sends them, and become integers again on save.
    """

    # Above 2**53, and deliberately not a multiple of the float spacing there,
    # so a rounding regression changes the value rather than getting lucky.
    REAL_ID = 1420070400000000001

    def setUp(self):
        from settings_registry import SETTING_DEFINITIONS
        self.definitions = SETTING_DEFINITIONS

    def test_id_is_beyond_javascript_precision(self):
        # Guards the premise: if this ever fails the test below proves nothing.
        self.assertGreater(self.REAL_ID, 2 ** 53)

    def test_wire_value_sends_snowflakes_as_strings(self):
        single = self.definitions["join_channel"]
        listed = self.definitions["premium_roles"]
        self.assertEqual(dashboard_api._wire_value(single, self.REAL_ID),
                         str(self.REAL_ID))
        self.assertEqual(dashboard_api._wire_value(listed, [self.REAL_ID]),
                         [str(self.REAL_ID)])
        self.assertIsNone(dashboard_api._wire_value(single, None))

    def test_non_snowflake_settings_are_untouched(self):
        integer = self.definitions["work_payout_min"]
        self.assertEqual(dashboard_api._wire_value(integer, 25), 25)

    def test_string_snowflake_is_accepted_and_normalised(self):
        from settings_registry import validate_setting_value
        single = self.definitions["join_channel"]
        listed = self.definitions["premium_roles"]
        self.assertEqual(validate_setting_value(single, str(self.REAL_ID)),
                         self.REAL_ID)
        self.assertEqual(validate_setting_value(listed, [str(self.REAL_ID)]),
                         [self.REAL_ID])

    def test_round_trip_preserves_the_exact_id(self):
        from settings_registry import validate_setting_value
        definition = self.definitions["premium_roles"]
        wired = dashboard_api._wire_value(definition, [self.REAL_ID])
        # What the browser would hand back untouched, now that it never
        # converts an id to a number.
        restored = validate_setting_value(definition, json.loads(json.dumps(wired)))
        self.assertEqual(restored, [self.REAL_ID])

    def test_rubbish_is_still_rejected(self):
        from settings_registry import validate_setting_value
        single = self.definitions["join_channel"]
        for bad in ("12a", "", "-5", True, -1, 0, 1.5, "0x10"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_setting_value(single, bad)
