import json
import os
import tempfile
import threading
import time
import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from urllib.parse import parse_qs, urlparse

import dashboard_api
from core import settings_cache
from core import database
from core import permission_audit


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
        banners = self.client.get("/api/guilds/123/gacha").get_json()["data"]["banners"]
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

    def test_login_sends_a_pkce_challenge_matching_the_stored_verifier(self):
        """Discord verifies `code_challenge` against the `code_verifier` this
        server sends back at the token exchange -- the two have to be the
        S256 pair, not independently generated values."""
        import base64
        import hashlib

        response = self.client.get("/api/auth/login")
        query = parse_qs(urlparse(response.location).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        with self.client.session_transaction() as session:
            verifier = session["oauth_code_verifier"]
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        self.assertEqual(query["code_challenge"], [expected])

    def test_callback_rejects_missing_or_mismatched_state_before_network(self):
        response = self.client.get("/api/callback?code=unused&state=wrong")
        self.assertEqual(response.status_code, 400)

    def test_callback_rejects_a_valid_state_with_no_stored_verifier(self):
        """A code without its matching verifier is exactly as suspect as a
        mismatched state, and refused the same way -- checked before any
        network call, not merely a 400 for some other reason downstream."""
        with self.client.session_transaction() as session:
            session["oauth_state"] = "expected"
        with patch.object(dashboard_api.requests, "post") as post:
            response = self.client.get("/api/callback?code=x&state=expected")
        post.assert_not_called()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_data(as_text=True),
                         dashboard_api.t("dashboard.oauth_invalid_state"))
        with self.client.session_transaction() as session:
            self.assertNotIn("oauth_state", session)

    def test_callback_sends_the_verifier_in_the_token_exchange(self):
        with self.client.session_transaction() as session:
            session["oauth_state"] = "expected"
            session["oauth_code_verifier"] = "the-verifier"
        with patch.object(dashboard_api.requests, "post") as post, \
                patch.object(dashboard_api.requests, "get") as get:
            post.return_value = MagicMock(
                status_code=200, json=lambda: {"access_token": "tok"})
            get.return_value = MagicMock(
                status_code=200, json=lambda: {"id": "42", "username": "tester",
                                               "avatar": None})
            self.client.get("/api/callback?code=abc&state=expected")
        self.assertEqual("the-verifier", post.call_args.kwargs["data"]["code_verifier"])

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
            json={"enabled": True, "starts_at": None, "ends_at": None, "config": banner["config"], "revision": 0},
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200)
        invalid = dict(banner["config"])
        invalid["tiers"] = {"3": 1, "4": 1, "5": 1}
        rejected = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True, "starts_at": None, "ends_at": None, "config": invalid, "revision": 1},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 400)

    def test_gacha_schedule_round_trips_and_a_reversed_one_is_refused(self):
        self.authenticate()
        headers = {"X-CSRF-Token": "csrf-token"}
        banner = self.default_banner()

        saved = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True,
                  "starts_at": "2026-01-01T00:00:00+00:00",
                  "ends_at": "2026-12-31T00:00:00+00:00",
                  "config": banner["config"], "revision": banner["revision"]},
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200)
        reloaded = self.default_banner()
        self.assertEqual("2026-01-01T00:00:00+00:00", reloaded["starts_at"])
        self.assertEqual("2026-12-31T00:00:00+00:00", reloaded["ends_at"])
        self.assertIn(reloaded["status"], ("scheduled", "live", "expired"))

        reversed_order = self.client.patch(
            "/api/guilds/123/gacha",
            json={"enabled": True,
                  "starts_at": "2026-12-31T00:00:00+00:00",
                  "ends_at": "2026-01-01T00:00:00+00:00",
                  "config": banner["config"], "revision": reloaded["revision"]},
            headers=headers,
        )
        self.assertEqual(reversed_order.status_code, 400)
        self.assertEqual(
            reversed_order.get_json()["message"],
            dashboard_api.t("dashboard.errors.gacha_banner_schedule_order"),
        )

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
             {"enabled": True, "starts_at": None, "ends_at": None, "config": "not-an-object", "revision": 0}),
            ("patch", "/api/guilds/123/gacha",
             {"enabled": True, "starts_at": None, "ends_at": None, "config": {}, "revision": None}),
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
            json={"item_key": "premium", "template_type": "fixed_role", "category": None, "enabled": True,
                  "price": 100, "config": {"role_id": 5}, "text": {"name": "a", "description": "b"}},
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
                    json={"item_key": key, "template_type": "vault", "category": None, "enabled": True,
                          "price": 100, "config": {"amount": 1000},
                          "text": {"name": "a", "description": "b"}},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400)

    def _authenticate_guild_admin(self):
        """A non-host with live authority over guild 123.

        The permission cache is seeded so `recheck_mutation_guild_permissions`
        answers from it rather than calling Discord — the same shape the logout
        and recheck tests use.
        """
        self.authenticate(user_id="999")
        with self.client.session_transaction() as session:
            session["authorized_guild_ids"] = ["123"]
        dashboard_api._oauth_tokens["server-session"] = {
            "access_token": "a", "refresh_token": "r", "expires_at": 2 ** 31,
        }
        dashboard_api._permission_cache.put("server-session", ["123"])

    def test_an_instance_setting_is_refused_for_a_non_host(self):
        """An instance setting has no guild dimension, so a guild admin saving
        one changes the whole installation and files the audit row under their
        own guild, where the guilds it affected cannot see it.

        Unreachable while the profile is `private`, which refuses every non-host
        login — which is exactly why it is gated here rather than later: the
        change that lets guild admins in is the change that would expose it.
        """
        self._authenticate_guild_admin()
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "maintenance", "value": True,
                               "revision": 0}]},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(400, response.status_code, response.get_data(as_text=True))
        self.assertNotIn("maintenance", database.get_instance_settings())

    def test_the_host_may_still_change_an_instance_setting(self):
        """Guards the premise: a gate that refuses everyone is not a gate."""
        self.authenticate()  # user_id 42 == ADMIN_ID
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "maintenance", "value": True,
                               "revision": 0}]},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        self.assertTrue(database.get_instance_settings()["maintenance"]["value"])

    def test_one_instance_key_refuses_the_whole_batch(self):
        """`set_guild_settings` is a single transaction, so a partial apply
        would be worse than a refusal."""
        self._authenticate_guild_admin()
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [
                {"key": "join_channel", "value": "1420070400000000001",
                 "revision": 0},
                {"key": "command_prefix", "value": "!", "revision": 0},
            ]},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(400, response.status_code)
        stored = database.get_guild_settings(123)
        self.assertNotIn("join_channel", stored,
                         "the guild-scoped half must not have been applied")

    def test_a_guild_setting_is_still_allowed_for_a_non_host(self):
        self._authenticate_guild_admin()
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "join_channel",
                               "value": "1420070400000000001", "revision": 0}]},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))

    def test_a_guild_read_refreshes_permissions_too(self):
        """The idle window slides on every request, so a demoted admin who kept
        clicking held read access until the twelve-hour cap."""
        self._authenticate_guild_admin()
        dashboard_api._permission_cache._entries.clear()
        calls = []

        def revoked(session_id):
            calls.append(session_id)
            return []            # Discord says: no guilds you may manage.

        original = dashboard_api._refresh_authorized_guilds
        dashboard_api._refresh_authorized_guilds = revoked
        try:
            response = self.client.get("/api/guilds/123/settings")
        finally:
            dashboard_api._refresh_authorized_guilds = original
        self.assertEqual(["server-session"], calls,
                         "a guild read must consult Discord")
        # 401, not 403: the client treats it as the session ending, which is the
        # right outcome for authority that has just been taken away.
        self.assertEqual(401, response.status_code)

    def test_a_guild_read_survives_discord_being_unreachable(self):
        """A read falls back to the session's snapshot. Refusing would trade a
        thirty-second stale window for the dashboard being unreadable."""
        self._authenticate_guild_admin()
        dashboard_api._permission_cache._entries.clear()

        def unreachable(session_id):
            raise requests.RequestException("discord is down")

        original = dashboard_api._refresh_authorized_guilds
        dashboard_api._refresh_authorized_guilds = unreachable
        try:
            read = self.client.get("/api/guilds/123/settings")
            write = self.client.patch(
                "/api/guilds/123/settings",
                json={"changes": [{"key": "join_channel",
                                   "value": "1420070400000000001",
                                   "revision": 0}]},
                headers={"X-CSRF-Token": "csrf-token"},
            )
        finally:
            dashboard_api._refresh_authorized_guilds = original
        self.assertEqual(200, read.status_code, "a read must still be served")
        self.assertEqual(503, write.status_code,
                         "a write must still refuse on a stale snapshot")

    def _rejected_with(self, status):
        """A refresh that raises the way `requests` does for one HTTP status."""
        response = requests.Response()
        response.status_code = status

        def rejected(session_id):
            raise requests.HTTPError(response=response)

        return rejected

    def test_a_rate_limited_refresh_does_not_end_the_session(self):
        """429 is Discord asking us to wait, not saying the grant is gone.

        Every 4xx used to end the session, so a rate limit logged a real
        administrator out — twelve times in one evening. The four reads
        `loadGuild()` fires at once each made their own Discord call, Discord
        rate-limited them, and the 429 read as a revocation: the dashboard
        appeared for a second and then said the session had expired, which no
        amount of logging back in could fix.

        A 429 must degrade exactly as an unreachable Discord does: the read is
        served from the session's snapshot, and only a write refuses.
        """
        self._authenticate_guild_admin()
        dashboard_api._permission_cache._entries.clear()
        original = dashboard_api._refresh_authorized_guilds
        dashboard_api._refresh_authorized_guilds = self._rejected_with(429)
        try:
            read = self.client.get("/api/guilds/123/settings")
            dashboard_api._permission_cache._entries.clear()
            write = self.client.patch(
                "/api/guilds/123/settings",
                json={"changes": [{"key": "join_channel",
                                   "value": "1420070400000000001",
                                   "revision": 0}]},
                headers={"X-CSRF-Token": "csrf-token"},
            )
        finally:
            dashboard_api._refresh_authorized_guilds = original
        self.assertEqual(200, read.status_code,
                         "a rate-limited refresh must still serve the read")
        self.assertEqual(503, write.status_code,
                         "a write must refuse rather than act on a stale snapshot")
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("logged_in"),
                            "a rate limit must not log the operator out")

    def test_a_revoked_grant_still_ends_the_session(self):
        """The other half: 401 and 403 are definite answers and must still end
        it, or narrowing the rule would keep a revoked session alive until the
        twelve-hour cap."""
        for status in (401, 403):
            with self.subTest(status=status):
                self._authenticate_guild_admin()
                dashboard_api._permission_cache._entries.clear()
                original = dashboard_api._refresh_authorized_guilds
                dashboard_api._refresh_authorized_guilds = self._rejected_with(status)
                try:
                    response = self.client.get("/api/guilds/123/settings")
                finally:
                    dashboard_api._refresh_authorized_guilds = original
                self.assertEqual(401, response.status_code)
                with self.client.session_transaction() as session:
                    self.assertIsNone(session.get("logged_in"))

    def test_concurrent_guild_reads_make_one_discord_call(self):
        """The burst that earned the 429 in the first place.

        `loadGuild()` fires four `/api/guilds/…` reads at once and a fresh
        session's permission cache is empty, so every one of them called Discord.
        The refresh is single-flight per session now: the first caller asks and
        stores, and the rest find the answer already there.

        Driven at the helper rather than through the test client, which is not
        safe to share across threads — one client per thread would be testing the
        harness rather than the lock.
        """
        dashboard_api._permission_cache.forget("burst-session")
        calls = []
        barrier = threading.Barrier(4)

        def refresh(session_id):
            calls.append(session_id)
            # Long enough that the others are certainly inside the critical
            # section's queue, so a missing lock reliably shows as four calls.
            time.sleep(0.05)
            return ["123"]

        def one_request():
            barrier.wait()
            with dashboard_api._session_refresh_lock("burst-session"):
                cached = dashboard_api._permission_cache.get("burst-session")
                if cached is not None:
                    return
                dashboard_api._permission_cache.put(
                    "burst-session", refresh("burst-session"))

        threads = [threading.Thread(target=one_request) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(calls),
                         f"four concurrent reads made {len(calls)} Discord calls")
        self.assertEqual(["123"],
                         dashboard_api._permission_cache.get("burst-session"))

    def test_the_route_refreshes_inside_the_lock(self):
        """The test above proves the lock works; this proves the route uses it.

        Driving four real requests would mean four test clients sharing a cookie,
        which is not safe and would test the harness. So the structure is
        asserted instead: every call to `_refresh_authorized_guilds` inside the
        before-request hook must sit under `with _session_refresh_lock(...)`.
        Read from the syntax tree, so a comment mentioning the lock does not read
        as the lock being taken.
        """
        import ast

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "dashboard_api.py"), encoding="utf-8") as handle:
            source = handle.read()
        hook = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
            and node.name == "recheck_mutation_guild_permissions"
        )

        def guarded(node, inside):
            """Every `_refresh_authorized_guilds(...)` reached from here."""
            found = []
            if isinstance(node, ast.With):
                inside = inside or any(
                    isinstance(item.context_expr, ast.Call)
                    and getattr(item.context_expr.func, "id", "")
                    == "_session_refresh_lock"
                    for item in node.items
                )
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "_refresh_authorized_guilds"):
                found.append(inside)
            for child in ast.iter_child_nodes(node):
                found.extend(guarded(child, inside))
            return found

        calls = guarded(hook, False)
        self.assertTrue(calls, "the hook no longer refreshes at all")
        self.assertTrue(
            all(calls),
            "a permission refresh runs outside the single-flight lock, so a "
            "burst of reads will call Discord once each and be rate-limited",
        )

    def test_the_refresh_lock_is_dropped_with_the_session(self):
        """It is per-session state like the token and the cache, so it has to be
        forgotten with them or the map grows one entry per login."""
        dashboard_api._session_refresh_lock("ending-session")
        self.assertIn("ending-session", dashboard_api._refresh_locks)
        dashboard_api._forget_session("ending-session")
        self.assertNotIn("ending-session", dashboard_api._refresh_locks)

    def test_a_session_route_is_not_gated_on_a_permission_refresh(self):
        """It has to keep working while a refresh cannot."""
        self._authenticate_guild_admin()
        dashboard_api._permission_cache._entries.clear()

        def unreachable(session_id):
            raise AssertionError("a session route must not consult Discord")

        original = dashboard_api._refresh_authorized_guilds
        dashboard_api._refresh_authorized_guilds = unreachable
        try:
            self.assertEqual(200, self.client.get("/api/session/touch").status_code)
        finally:
            dashboard_api._refresh_authorized_guilds = original

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

    def _manual_grant_payload(self, **overrides):
        payload = {"user_id": "7", "asset_type": "emoji",
                   "discord_item_id": "998877", "duration_days": 30}
        payload.update(overrides)
        return payload

    def test_manual_grant_creates_an_active_entitlement(self):
        headers = self._headers()
        response = self.client.post(
            "/api/guilds/123/entitlements",
            json=self._manual_grant_payload(),
            headers=headers,
        )
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        listed = self.client.get("/api/guilds/123/entitlements", headers=headers)
        rows = listed.get_json()["data"]
        self.assertEqual(1, len(rows))
        self.assertEqual("manual", rows[0]["source_type"])
        self.assertEqual("998877", rows[0]["discord_item_id"])
        self.assertEqual("42", rows[0]["granted_by"],
                         "the acting staff member's id, as a wire-format string")

    def test_manual_grant_of_the_same_asset_twice_is_refused(self):
        headers = self._headers()
        self.client.post("/api/guilds/123/entitlements",
                         json=self._manual_grant_payload(), headers=headers)
        second = self.client.post(
            "/api/guilds/123/entitlements",
            json=self._manual_grant_payload(user_id="8"), headers=headers)
        self.assertEqual(409, second.status_code)

    def test_manual_grant_rejects_an_unknown_asset_type(self):
        headers = self._headers()
        response = self.client.post(
            "/api/guilds/123/entitlements",
            json=self._manual_grant_payload(asset_type="role"), headers=headers)
        self.assertEqual(400, response.status_code)
        self.assertEqual(response.get_json()["message"],
                         dashboard_api.t("dashboard.errors.manual_grant_asset_type_invalid"))

    def test_manual_grant_rejects_a_duration_outside_the_bound(self):
        headers = self._headers()
        for duration in (0, 3651):
            with self.subTest(duration=duration):
                response = self.client.post(
                    "/api/guilds/123/entitlements",
                    json=self._manual_grant_payload(duration_days=duration),
                    headers=headers)
                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    response.get_json()["message"],
                    dashboard_api.t("dashboard.errors.manual_grant_duration_invalid"))

    def test_manual_grant_rejects_a_non_snowflake_user_id(self):
        headers = self._headers()
        response = self.client.post(
            "/api/guilds/123/entitlements",
            json=self._manual_grant_payload(user_id="not-a-user"), headers=headers)
        self.assertEqual(400, response.status_code)
        self.assertEqual(response.get_json()["message"],
                         dashboard_api.t("dashboard.errors.manual_grant_user_id_invalid"))

    def test_shop_item_audit_commits_with_the_item(self):
        headers = self._headers()
        created = self.client.post(
            "/api/guilds/123/shop-items",
            json={"item_key": "vip_role", "template_type": "vault", "category": None, "enabled": True,
                  "price": 100, "config": {"amount": 1000},
                  "text": {"name": "Vip", "description": "leiras"}},
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
            json={"item_key": key, "template_type": "vault", "category": None, "enabled": True,
                  "price": price, "config": {"amount": 1000},
                  "text": {"name": "Vip", "description": "leiras"}},
            headers=headers,
        )

    def test_shop_item_can_be_edited_disabled_and_deleted(self):
        headers = self._headers()
        self.assertEqual(self._create_item(headers).status_code, 201)

        stored = self.client.get("/api/guilds/123/shop-items").get_json()["data"][0]
        self.assertEqual(stored["revision"], 1)

        disabled = self.client.patch(
            "/api/guilds/123/shop-items/vip_role",
            json={"template_type": "vault", "category": None, "enabled": False, "price": 250,
                  "config": {"amount": 2000},
                  "text": {"name": "Vip 2", "description": "uj"}, "revision": 1},
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
            json={"template_type": "vault", "category": None, "enabled": True, "price": 300,
                  "config": {"amount": 2000},
                  "text": {"name": "x", "description": "y"}, "revision": 1},
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
            json={"template_type": "arbitrary_code", "category": None, "enabled": True, "price": 1,
                  "config": {}, "text": {"name": "a", "description": "b"}, "revision": 1},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_shop_item_key_is_immutable_and_builtins_are_not_addressable(self):
        headers = self._headers()
        self._create_item(headers)
        renamed = self.client.patch(
            "/api/guilds/123/shop-items/vip_role",
            json={"item_key": "other", "template_type": "vault", "category": None, "enabled": True,
                  "price": 1, "config": {"amount": 1},
                  "text": {"name": "a", "description": "b"}, "revision": 1},
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

    def test_action_status_is_readable_and_guild_scoped(self):
        headers = self._headers()
        action_id = database.queue_control_action(
            123, 42, "publish_managed",
            {"kind": "embed", "menu_key": "notice", "channel_id": 2}
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
        done = database.queue_control_action(123, 42, "publish_managed", {})
        pending = database.queue_control_action(123, 42, "publish_managed", {})
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
        """The cap is per section now, so this fills the one the payload lands
        in. Every stored row counts, not just the enabled ones: counting only
        enabled rows let a guild accumulate disabled definitions and then
        re-enable past what a Discord select menu can display."""
        from core import item_catalog

        headers = self._headers()
        # `_create_item` posts a vault, which resolves to the protection shelf.
        section = item_catalog.resolve_custom_category("vault", {"amount": 1000})
        capacity = item_catalog.custom_item_capacity(section)
        for index in range(capacity):
            created = self._create_item(headers, key=f"item_{index}")
            self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
            # Disable it, so only the total row count can enforce the cap.
            disabled = self.client.patch(
                f"/api/guilds/123/shop-items/item_{index}",
                json={"template_type": "vault", "category": None, "enabled": False, "price": 100,
                      "config": {"amount": 1000},
                      "text": {"name": "a", "description": "b"}, "revision": 1},
                headers=headers,
            )
            self.assertEqual(disabled.status_code, 200)

        overflow = self._create_item(headers, key="one_too_many")
        self.assertEqual(overflow.status_code, 400)
        # The message names the shelf the operator reads, not the raw id.
        self.assertEqual(
            overflow.get_json()["message"],
            dashboard_api.t(
                "dashboard.errors.shop_item_limit", limit=capacity,
                category=dashboard_api.t(f"shop.categories.{section}.name")),
        )
        # And another shelf is untouched, which is the whole point of sections.
        elsewhere = self.client.post(
            "/api/guilds/123/shop-items",
            json={"item_key": "on_another_shelf", "template_type": "coin_bundle",
                  "category": "perks", "enabled": True, "price": 100,
                  "config": {"amount": 10, "repeatable": False},
                  "text": {"name": "a", "description": "b"}},
            headers=headers,
        )
        self.assertEqual(elsewhere.status_code, 201,
                         elsewhere.get_data(as_text=True))

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
                    json={"enabled": True, "starts_at": None, "ends_at": None, "config": config, "revision": 0},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400)
                message = response.get_json()["message"]
                self.assertEqual(message, dashboard_api.t(f"dashboard.errors.{reason}"))
                self.assertNotEqual(message, generic)

    def test_every_model_reason_code_has_a_locale_key(self):
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "core", "database.py"), encoding="utf-8") as handle:
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
        from core.settings_registry import SETTING_DEFINITIONS

        missing = sorted(
            definition.key for definition in SETTING_DEFINITIONS.values()
            if dashboard_api.t(definition.locale_key).startswith("[")
        )
        self.assertEqual([], missing)

    def test_item_catalog_requires_a_session_and_lists_the_shared_items(self):
        from core import item_catalog

        self.assertEqual(self.client.get("/api/item-catalog").status_code, 401)
        self.authenticate()
        payload = self.client.get("/api/item-catalog").get_json()["data"]
        self.assertEqual(
            {entry["key"] for entry in payload}, set(item_catalog.ITEM_DEFINITIONS)
        )
        # Identity and defaults only; nothing guild-specific or secret. The
        # category belongs here: it is part of what an item *is*, and a payload
        # that omits a field of the definition is how the dashboard ends up
        # unable to express something.
        self.assertEqual(
            set(payload[0]),
            {"key", "effect", "value", "default_price", "sold_in_shop",
             "gacha_kind", "category"},
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
            json={"enabled": True, "starts_at": None, "ends_at": None, "config": config, "revision": 0},
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
            json={"enabled": True, "starts_at": None, "ends_at": None, "config": stored["config"],
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
            json={"enabled": True, "starts_at": None, "ends_at": None, "config": final["config"], "revision": 0},
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
            json={"enabled": True, "starts_at": None, "ends_at": None, "config": mismatched, "revision": 0},
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
                json={"enabled": True, "starts_at": None, "ends_at": None, "config": emptied, "revision": 0},
                headers=headers,
            ).status_code,
            400,
        )

    def test_custom_consumable_items_are_validated_against_the_catalog(self):
        from core import item_catalog

        headers = self._headers()
        for item_key in sorted(item_catalog.INVENTORY_ITEM_KEYS):
            with self.subTest(item_key=item_key):
                response = self.client.post(
                    "/api/guilds/123/shop-items",
                    json={
                        "item_key": f"custom_{item_key}", "template_type": "consumable", "category": None,
                        "enabled": True, "price": 100,
                        "config": {"item_key": item_key},
                        "text": {"name": "Teszt", "description": "Teszt"},
                    },
                    headers=headers,
                )
                self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
                # Removed again before the next one: this is about the
                # validator accepting every catalog consumable, not about how
                # many custom items fit in a Discord menu, and the cap is
                # derived — it shrinks whenever a built-in is added.
                self.client.delete(
                    f"/api/guilds/123/shop-items/custom_{item_key}",
                    json={"revision": 1}, headers=headers,
                )

        rejected = self.client.post(
            "/api/guilds/123/shop-items",
            json={
                "item_key": "custom_unknown", "template_type": "consumable", "category": None,
                "enabled": True, "price": 100,
                "config": {"item_key": "big_vault"},
                "text": {"name": "Teszt", "description": "Teszt"},
            },
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 400)

    def test_a_setting_save_has_one_destination(self):
        """There is nothing left to reconcile, because nothing diverges.

        `config.json` and its mirror are both gone (2026-09-21); SQLite is the
        only authority a setting save writes to.
        """
        self.authenticate(user_id="42")
        response = self.client.patch(
            "/api/guilds/123/settings",
            json={"changes": [{"key": "shop_price_premium",
                               "value": 777, "revision": 0}]},
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(response.status_code, 200)
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
        """The front end may not parse markdown, so the API has to.

        The remote fetch is patched to fail here, so this exercises exactly
        the local-fallback path -- a real network call has no place in this
        suite, and the remote path has its own tests below.
        """
        self.assertEqual(self.client.get("/api/changelog").status_code, 401)
        self.authenticate()
        dashboard_api._changelog_cache._entries.clear()
        with patch.object(dashboard_api, "_fetch_remote_changelog", return_value=None):
            response = self.client.get("/api/changelog")
        self.assertEqual(response.status_code, 200)
        releases = response.get_json()["data"]
        self.assertTrue(releases)
        self.assertTrue(all(release["version"] for release in releases))
        self.assertTrue(all(release["entries"] for release in releases))

    def test_changelog_prefers_the_remote_copy_when_it_is_reachable(self):
        """A successful fetch is the one actually served, not a coincidence
        of both sources agreeing."""
        self.authenticate()
        dashboard_api._changelog_cache._entries.clear()
        remote_text = "# Changelog\n\n## 9.9.9 - 2099-01-01\n\n- Only in the remote copy.\n"
        with patch.object(dashboard_api, "_fetch_remote_changelog",
                          return_value=remote_text):
            response = self.client.get("/api/changelog")
        self.assertEqual(response.status_code, 200)
        releases = response.get_json()["data"]
        self.assertEqual(1, len(releases))
        self.assertEqual("9.9.9", releases[0]["version"])
        self.assertEqual(["Only in the remote copy."], releases[0]["entries"])

    def test_changelog_falls_back_to_the_local_file_when_both_fail(self):
        with patch.object(dashboard_api, "_fetch_remote_changelog", return_value=None), \
                patch.object(dashboard_api, "CHANGELOG_PATH", "/no/such/file.md"):
            self.authenticate()
            dashboard_api._changelog_cache._entries.clear()
            response = self.client.get("/api/changelog")
        self.assertEqual(response.status_code, 503)

    def test_fetch_remote_changelog_returns_none_on_any_request_failure(self):
        with patch.object(dashboard_api.requests, "get",
                          side_effect=requests.exceptions.Timeout("slow")):
            self.assertIsNone(dashboard_api._fetch_remote_changelog())

        response = SimpleNamespace(
            raise_for_status=MagicMock(
                side_effect=requests.exceptions.HTTPError("404")))
        with patch.object(dashboard_api.requests, "get", return_value=response):
            self.assertIsNone(dashboard_api._fetch_remote_changelog())

    def test_fetch_remote_changelog_returns_the_response_text_on_success(self):
        response = SimpleNamespace(raise_for_status=MagicMock(), text="ok text")
        with patch.object(dashboard_api.requests, "get", return_value=response):
            self.assertEqual("ok text", dashboard_api._fetch_remote_changelog())

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

        banners = self.client.get("/api/guilds/123/gacha").get_json()["data"]["banners"]
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
            json={"enabled": True, "starts_at": None, "ends_at": None, "config": banner["config"], "revision": 0,
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
            ("/api/guilds/123/entitlements",
             {"user_id": "7", "asset_type": "emoji", "discord_item_id": "1",
              "duration_days": 30}),
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
        from core.settings_registry import SETTING_DEFINITIONS
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
        from core.settings_registry import validate_setting_value
        single = self.definitions["join_channel"]
        listed = self.definitions["premium_roles"]
        self.assertEqual(validate_setting_value(single, str(self.REAL_ID)),
                         self.REAL_ID)
        self.assertEqual(validate_setting_value(listed, [str(self.REAL_ID)]),
                         [self.REAL_ID])

    def test_round_trip_preserves_the_exact_id(self):
        from core.settings_registry import validate_setting_value
        definition = self.definitions["premium_roles"]
        wired = dashboard_api._wire_value(definition, [self.REAL_ID])
        # What the browser would hand back untouched, now that it never
        # converts an id to a number.
        restored = validate_setting_value(definition, json.loads(json.dumps(wired)))
        self.assertEqual(restored, [self.REAL_ID])

    def test_rubbish_is_still_rejected(self):
        from core.settings_registry import validate_setting_value
        single = self.definitions["join_channel"]
        for bad in ("12a", "", "-5", True, -1, 0, 1.5, "0x10"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_setting_value(single, bad)


class AuditHardeningTests(unittest.TestCase):
    """The dashboard items from the 2026-09-11 audit, each pinned.

    Reuses the security suite's fixture by reference rather than by inheritance,
    so discovery does not run every parent test a second time under this name.
    """

    setUp = DashboardSecurityTests.setUp
    tearDown = DashboardSecurityTests.tearDown
    authenticate = DashboardSecurityTests.authenticate

    def test_a_csrf_less_burst_is_rate_limited_not_just_refused(self):
        """The limiter runs before the CSRF check, so refusals are counted."""
        statuses = [
            self.client.post("/api/guilds/123/features", json={}).status_code
            for _ in range(61)
        ]
        self.assertEqual(403, statuses[0])
        self.assertIn(429, statuses)

    def test_a_bad_oauth_state_does_not_end_a_live_session(self):
        self.authenticate()
        refused = self.client.get("/api/callback?code=x&state=wrong")
        self.assertEqual(400, refused.status_code)
        status = self.client.get("/api/auth/status").get_json()
        self.assertTrue(status["logged_in"])

    def test_a_bad_oauth_state_still_clears_an_anonymous_session(self):
        with self.client.session_transaction() as session:
            session["oauth_state"] = "expected"
        self.assertEqual(
            400, self.client.get("/api/callback?code=x&state=wrong").status_code)
        with self.client.session_transaction() as session:
            self.assertNotIn("oauth_state", session)
            self.assertNotIn("logged_in", session)

    def test_an_unknown_path_answers_in_the_json_envelope(self):
        response = self.client.get("/api/definitely-not-a-route")
        self.assertEqual(404, response.status_code)
        body = response.get_json()
        self.assertEqual("error", body["status"])
        self.assertTrue(body["message"])

    def test_a_wrong_method_answers_in_the_json_envelope(self):
        self.authenticate()
        response = self.client.post("/api/changelog", json={},
                                    headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(405, response.status_code)
        self.assertEqual("error", response.get_json()["status"])

    def test_headers_carry_permissions_policy_and_a_narrow_img_src(self):
        response = self.client.get("/api/locale")
        self.assertIn("camera=()", response.headers["Permissions-Policy"])
        policy = response.headers["Content-Security-Policy"]
        self.assertIn("img-src 'self' data: https://cdn.discordapp.com", policy)
        self.assertNotIn("img-src 'self' https:", policy)

    def test_data_scope_writes_are_host_only(self):
        self.authenticate(user_id="7")  # a guild administrator, not the host
        # A non-host guild read goes through the live permission refresh; seed
        # the cache so it answers from there rather than calling Discord.
        dashboard_api._permission_cache.put("server-session", ["123"])
        headers = {"X-CSRF-Token": "csrf-token"}
        body = {"category": "profile", "scope_type": "instance",
                "realm_id": None, "revision": 0}
        self.assertEqual(401, self.client.post(
            "/api/guilds/123/data-scopes", json=body, headers=headers).status_code)
        # Reading stays with the guild's own administrator.
        self.assertEqual(200, self.client.get("/api/guilds/123/data-scopes").status_code)

    def test_realm_join_is_host_only_and_validates_the_guild_id(self):
        headers = {"X-CSRF-Token": "csrf-token"}
        self.authenticate()
        realm_id = self.client.post("/api/realms", json={"name": "Trusted Guilds"},
                                    headers=headers).get_json()["data"]["realm_id"]
        # A list where a snowflake belongs used to be `int([...])`: a 500.
        self.assertEqual(400, self.client.post(
            f"/api/realms/{realm_id}/memberships", json={"guild_id": [123]},
            headers=headers).status_code)
        self.assertEqual(200, self.client.post(
            f"/api/realms/{realm_id}/memberships", json={"guild_id": "123"},
            headers=headers).status_code)
        self.authenticate(user_id="7")
        self.assertEqual(401, self.client.post(
            f"/api/realms/{realm_id}/memberships", json={"guild_id": 123},
            headers=headers).status_code)

    def test_every_validation_reason_the_api_can_raise_has_words(self):
        """A `RequestValidationError` key that no catalog holds reaches the
        operator as `[dashboard.errors.something]`.

        The locale audit's literal scanner reads `t("...")` calls and never saw
        these, because the key is a constructor argument that only becomes a
        `t()` call inside `invalid_request_response`. That is how
        `instance_setting_host_only` shipped under `dashboard.hints` while the
        route raised `dashboard.errors.instance_setting_host_only`, and every
        refused instance write rendered as a bracketed key.
        """
        source = (dashboard_api.__file__)
        text = open(source, encoding="utf-8").read()
        keys = set(re.findall(
            r'RequestValidationError\(\s*"(dashboard\.[a-z_.]+)"', text))
        self.assertGreater(len(keys), 40, "the scan found too few keys to be real")
        unresolved = sorted(k for k in keys if dashboard_api.t(k).startswith("["))
        self.assertEqual([], unresolved)

    def test_adopting_by_bare_id_needs_a_link_in_a_large_guild(self):
        self.authenticate()
        guild = SimpleNamespace(
            id=123, me=SimpleNamespace(id=1),
            text_channels=[MagicMock() for _ in range(
                dashboard_api.ADOPT_BARE_ID_CHANNEL_LIMIT + 1)])
        fake_bot = SimpleNamespace(get_guild=lambda gid: guild, loop=None)
        with patch.object(dashboard_api, "_dashboard_bot", fake_bot):
            response = self.client.post(
                "/api/guilds/123/managed/rules/adopt",
                json={"message": "123456789012345678", "menu_key": "rules",
                      "display_name": "Rules"},
                headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            dashboard_api.t("dashboard.errors.managed_adopt_link_required"),
            response.get_json()["message"])

    def test_permission_repair_requires_confirmation(self):
        self.authenticate()
        response = self.client.post(
            "/api/guilds/123/permissions/repair",
            json={"channel_id": "500", "subject": "bot_log_channel", "confirm": False},
            headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(dashboard_api.t("dashboard.errors.confirmation_required"),
                         response.get_json()["message"])

    def test_permission_repair_refuses_a_finding_that_is_no_longer_present(self):
        """The client's own copy of the report is not trusted -- the finding
        has to still be there the moment the repair is queued."""
        self.authenticate()
        guild = SimpleNamespace(me=SimpleNamespace(id=1))
        fake_bot = SimpleNamespace(get_guild=lambda gid: guild)
        with patch.object(dashboard_api, "_dashboard_bot", fake_bot), \
                patch.object(permission_audit, "build_report",
                            return_value=SimpleNamespace(findings=[])):
            response = self.client.post(
                "/api/guilds/123/permissions/repair",
                json={"channel_id": "500", "subject": "bot_log_channel", "confirm": True},
                headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(dashboard_api.t("dashboard.errors.permission_finding_stale"),
                         response.get_json()["message"])

    def test_permission_repair_refuses_a_finding_for_a_different_channel(self):
        """A live finding existing somewhere is not enough -- it has to be
        for the exact channel and setting the request named, or a stale
        click could repair the wrong thing."""
        self.authenticate()
        guild = SimpleNamespace(me=SimpleNamespace(id=1))
        fake_bot = SimpleNamespace(get_guild=lambda gid: guild)
        other_channel = permission_audit.Finding(
            code="channel_missing_permission", severity="blocking",
            subject="bot_log_channel", permissions=("send_messages",),
            feature="general", identifier="other", channel_id=999,
        )
        with patch.object(dashboard_api, "_dashboard_bot", fake_bot), \
                patch.object(permission_audit, "build_report",
                            return_value=SimpleNamespace(findings=[other_channel])):
            response = self.client.post(
                "/api/guilds/123/permissions/repair",
                json={"channel_id": "500", "subject": "bot_log_channel", "confirm": True},
                headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(dashboard_api.t("dashboard.errors.permission_finding_stale"),
                         response.get_json()["message"])

    def test_permission_repair_queues_exactly_the_findings_permissions(self):
        self.authenticate()
        guild = SimpleNamespace(me=SimpleNamespace(id=1))
        fake_bot = SimpleNamespace(get_guild=lambda gid: guild)
        finding = permission_audit.Finding(
            code="channel_missing_permission", severity="blocking",
            subject="bot_log_channel", permissions=("send_messages",),
            feature="general", identifier="bot-log", channel_id=500,
        )
        with patch.object(dashboard_api, "_dashboard_bot", fake_bot), \
                patch.object(permission_audit, "build_report",
                            return_value=SimpleNamespace(findings=[finding])):
            response = self.client.post(
                "/api/guilds/123/permissions/repair",
                json={"channel_id": "500", "subject": "bot_log_channel", "confirm": True},
                headers={"X-CSRF-Token": "csrf-token"})
        self.assertEqual(200, response.status_code)
        action_id = response.get_json()["data"]["action_id"]
        # `claim_control_action` is what the worker itself reads, so this
        # checks the payload the worker will actually see, not a copy of it.
        claimed = database.claim_control_action()
        self.assertEqual(action_id, claimed["action_id"])
        self.assertEqual("repair_channel_permission", claimed["action_type"])
        self.assertEqual(
            {"channel_id": 500, "permissions": ["send_messages"],
             "setting_key": "bot_log_channel"},
            claimed["payload"])

