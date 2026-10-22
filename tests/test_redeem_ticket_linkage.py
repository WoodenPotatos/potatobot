"""The redeem ticket <-> dashboard fulfillment link, and the cancel/revoke
actions the Redeems page gained alongside it.

Before this, a redeem ticket and its fulfillment request shared no stored
link at all -- only a human-readable "#request_id" in the ticket's embed that
staff had to read and cross-reference by eye. And the `'cancelled'` status
was a legal value in three CHECK constraints (`reward_vouchers`,
`timed_entitlements`, `fulfillment_requests`) that nothing ever wrote.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import database

GUILD = 1
MEMBER = 7
STAFF = 99


class _FulfillmentTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(GUILD, "Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _grant_voucher(self, voucher_id="v1", reward_key="emoji_30d"):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO reward_vouchers (voucher_id, guild_id, user_id, "
                "reward_key, duration_days, status, acquired_at) "
                "VALUES (?, ?, ?, ?, 30, 'available', '2026-01-01')",
                (voucher_id, GUILD, MEMBER, reward_key),
            )
            conn.commit()

    def _open_request(self, voucher_id="v1"):
        self._grant_voucher(voucher_id)
        database.redeem_voucher(GUILD, MEMBER, voucher_id)
        return [r for r in database.get_fulfillment_requests(GUILD)
                if r["voucher_id"] == voucher_id][0]


class TicketLinkTests(_FulfillmentTestCase):
    def test_a_linked_ticket_is_found_by_channel(self):
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        found = database.get_open_fulfillment_by_channel(555)
        self.assertEqual(request["request_id"], found["request_id"])
        self.assertEqual(GUILD, found["guild_id"])

    def test_an_unlinked_channel_finds_nothing(self):
        self._open_request()
        self.assertIsNone(database.get_open_fulfillment_by_channel(555))

    def test_a_fulfilled_request_is_no_longer_found_by_channel(self):
        """The in-ticket button must stop offering a second completion."""
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        database.fulfill_voucher_request(GUILD, request["request_id"], STAFF, "1")
        self.assertIsNone(database.get_open_fulfillment_by_channel(555))

    def test_linking_a_request_in_another_guild_is_a_no_op(self):
        request = self._open_request()
        database.link_fulfillment_ticket(999, request["request_id"], 555)
        self.assertIsNone(database.get_open_fulfillment_by_channel(555))


class CancelFulfillmentRequestTests(_FulfillmentTestCase):
    def test_cancelling_an_open_request_marks_both_rows_cancelled(self):
        request = self._open_request()
        result = database.cancel_fulfillment_request(GUILD, request["request_id"], STAFF)
        self.assertTrue(result["cancelled"])
        with database.get_connection() as conn:
            req_status, voucher_status = conn.execute(
                "SELECT f.status, v.status FROM fulfillment_requests f "
                "JOIN reward_vouchers v ON v.voucher_id = f.voucher_id "
                "WHERE f.request_id = ?", (request["request_id"],),
            ).fetchone()
        self.assertEqual("cancelled", req_status)
        self.assertEqual("cancelled", voucher_status)

    def test_cancelling_reports_the_linked_ticket(self):
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        result = database.cancel_fulfillment_request(GUILD, request["request_id"], STAFF)
        self.assertEqual(555, result["ticket_channel_id"])

    def test_an_already_fulfilled_request_cannot_be_cancelled(self):
        request = self._open_request()
        database.fulfill_voucher_request(GUILD, request["request_id"], STAFF, "1")
        result = database.cancel_fulfillment_request(GUILD, request["request_id"], STAFF)
        self.assertFalse(result["cancelled"])
        with database.get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM fulfillment_requests WHERE request_id = ?",
                (request["request_id"],),
            ).fetchone()[0]
        self.assertEqual("fulfilled", status, "a rejected cancel must not touch the row")

    def test_a_request_in_another_guild_cannot_be_cancelled(self):
        request = self._open_request()
        result = database.cancel_fulfillment_request(999, request["request_id"], STAFF)
        self.assertFalse(result["cancelled"])


class RevokeTimedEntitlementTests(_FulfillmentTestCase):
    def _active_entitlement(self):
        request = self._open_request()
        database.fulfill_voucher_request(GUILD, request["request_id"], STAFF, "123")
        return database.get_active_entitlements(GUILD, "2000-01-01T00:00:00+00:00")[0]

    def test_revoking_an_active_entitlement_marks_it_cancelled_with_a_timestamp(self):
        entitlement = self._active_entitlement()
        result = database.revoke_timed_entitlement(
            GUILD, entitlement["entitlement_id"], STAFF)
        self.assertTrue(result["revoked"])
        self.assertEqual("emoji", result["entitlement_key"])
        self.assertEqual("123", result["discord_item_id"])
        with database.get_connection() as conn:
            status, revoked_at, expires_at = conn.execute(
                "SELECT status, revoked_at, expires_at FROM timed_entitlements "
                "WHERE entitlement_id = ?", (entitlement["entitlement_id"],),
            ).fetchone()
        self.assertEqual("cancelled", status)
        self.assertIsNotNone(revoked_at)
        self.assertEqual(entitlement["expires_at"], expires_at,
                         "the original grant window must not be rewritten")

    def test_a_revoked_entitlement_no_longer_lists_as_active(self):
        entitlement = self._active_entitlement()
        database.revoke_timed_entitlement(GUILD, entitlement["entitlement_id"], STAFF)
        remaining = database.get_active_entitlements(GUILD, "2000-01-01T00:00:00+00:00")
        self.assertEqual([], remaining)

    def test_revoking_twice_is_refused_the_second_time(self):
        entitlement = self._active_entitlement()
        database.revoke_timed_entitlement(GUILD, entitlement["entitlement_id"], STAFF)
        second = database.revoke_timed_entitlement(
            GUILD, entitlement["entitlement_id"], STAFF)
        self.assertFalse(second["revoked"])

    def test_an_entitlement_in_another_guild_cannot_be_revoked(self):
        entitlement = self._active_entitlement()
        result = database.revoke_timed_entitlement(
            999, entitlement["entitlement_id"], STAFF)
        self.assertFalse(result["revoked"])


class ControlActionWhitelistTests(unittest.TestCase):
    """Its own temp database, like every other class here -- without one this
    silently read and wrote whatever `database.DB_PATH` happened to default
    to, which passed locally only because a stray `economy.db` sat in the
    repo root from earlier ad-hoc runs and masked the missing schema. CI's
    clean checkout had no such file and caught it: `queue_control_action`
    does not wrap its INSERT, so a schema-less database raised a raw
    `sqlite3.OperationalError` instead of the `DatabaseOperationError` this
    test was built to tolerate."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(GUILD, "Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_the_two_new_action_types_are_accepted(self):
        for action_type in ("notify_redeem_fulfilled", "revoke_entitlement"):
            with self.subTest(action_type=action_type):
                action_id = database.queue_control_action(
                    GUILD, STAFF, action_type, {})
                self.assertIsInstance(action_id, int)


class RedeemFulfillViewTests(_FulfillmentTestCase, unittest.IsolatedAsyncioTestCase):
    """The in-ticket "Mark Fulfilled" button and its modal, isolated from the
    feature/staff gate -- same pattern as ShopHideCommandTests in
    tests/test_shop_items.py. `cogs.gacha` is resolved fresh in setUp rather
    than imported at module scope, since tests.test_cog_loading unloads every
    cog and a stale module-level import would make `patch.object` target a
    different object than the one these classes actually call into."""

    def setUp(self):
        super().setUp()
        import cogs.gacha
        self.gacha = cogs.gacha
        self.require_feature_patch = patch.object(
            self.gacha, "require_interaction_feature", AsyncMock(return_value=True))
        self.require_feature_patch.start()
        self.is_staff_patch = patch.object(
            self.gacha, "is_ticket_staff", return_value=True)
        self.is_staff_patch.start()

    def tearDown(self):
        self.require_feature_patch.stop()
        self.is_staff_patch.stop()
        super().tearDown()

    #: Emoji that exist in the stand-in guild; any other id resolves nowhere.
    GUILD_EMOJI = {123456, 999}

    def _interaction(self, channel_id, user_id=STAFF):
        guild = types.SimpleNamespace(
            id=GUILD,
            get_emoji=lambda emoji_id: (types.SimpleNamespace(id=emoji_id)
                                        if emoji_id in self.GUILD_EMOJI else None))
        return types.SimpleNamespace(
            channel_id=channel_id, guild=guild,
            user=types.SimpleNamespace(id=user_id, mention=f"<@{user_id}>"),
            response=types.SimpleNamespace(
                send_message=AsyncMock(), send_modal=AsyncMock(),
                edit_message=AsyncMock()),
        )

    async def test_the_button_refuses_when_nothing_is_open_on_this_channel(self):
        view = self.gacha.RedeemFulfillView()
        interaction = self._interaction(channel_id=555)
        await view.fulfill(interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.send_modal.assert_not_called()

    async def test_the_button_opens_the_modal_when_a_request_is_linked(self):
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        view = self.gacha.RedeemFulfillView()
        interaction = self._interaction(channel_id=555)
        await view.fulfill(interaction)
        interaction.response.send_modal.assert_awaited_once()

    async def test_submitting_the_modal_fulfills_the_linked_request(self):
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        modal = self.gacha.RedeemFulfillModal()
        modal.discord_item_id._value = "123456"
        interaction = self._interaction(channel_id=555)
        await modal.on_submit(interaction)
        interaction.response.edit_message.assert_awaited_once()
        with database.get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM fulfillment_requests WHERE request_id = ?",
                (request["request_id"],)).fetchone()[0]
        self.assertEqual("fulfilled", status)

    async def test_an_id_that_names_no_asset_in_this_guild_is_refused(self):
        """The id is deleted from Discord when the entitlement runs out, so an
        unchecked one let staff schedule the deletion of any asset -- or store
        a non-numeric string that made the entitlement never expire."""
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        for value in ("424242", "not-an-id"):
            with self.subTest(value=value):
                modal = self.gacha.RedeemFulfillModal()
                modal.discord_item_id._value = value
                interaction = self._interaction(channel_id=555)
                await modal.on_submit(interaction)
                interaction.response.send_message.assert_awaited_once()
                interaction.response.edit_message.assert_not_called()
        with database.get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM fulfillment_requests WHERE request_id = ?",
                (request["request_id"],)).fetchone()[0]
        self.assertEqual("open", status)

    async def test_submitting_the_modal_on_an_already_settled_request_is_refused(self):
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        database.fulfill_voucher_request(GUILD, request["request_id"], STAFF, "1")
        modal = self.gacha.RedeemFulfillModal()
        modal.discord_item_id._value = "999"
        interaction = self._interaction(channel_id=555)
        await modal.on_submit(interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.edit_message.assert_not_called()

    async def test_a_non_staff_member_is_refused_by_the_button(self):
        request = self._open_request()
        database.link_fulfillment_ticket(GUILD, request["request_id"], 555)
        view = self.gacha.RedeemFulfillView()
        interaction = self._interaction(channel_id=555)
        # Nests inside the class's own patch and unwinds back to it on exit.
        with patch.object(self.gacha, "is_ticket_staff", return_value=False):
            await view.fulfill(interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.send_modal.assert_not_called()


class ControlActionDispatcherTests(unittest.IsolatedAsyncioTestCase):
    """The two newer `control_actions` branches of the outbox worker --
    `cogs.gacha.revoke_entitlement` is imported fresh at call time inside
    `execute_entitlement_revocation`, so patching it by string path here is
    not the stale-module trap tests/test_voicemod.py had to work around."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "dispatch.db")
        database.initialize_database()
        database.register_guild(GUILD, "Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    async def test_the_notice_names_the_fulfilling_staff_member_and_item(self):
        from core import control_actions
        staff = types.SimpleNamespace(mention="<@99>")
        channel = types.SimpleNamespace(
            send=AsyncMock(),
            guild=types.SimpleNamespace(get_member=lambda uid: staff),
        )
        action = {"actor_id": 99,
                  "payload": {"outcome": "fulfilled", "discord_item_id": "123"}}
        error = await control_actions.execute_redeem_fulfilled_notice(channel, action)
        self.assertIsNone(error)
        sent_text = channel.send.await_args.args[0]
        self.assertIn("<@99>", sent_text)
        self.assertIn("123", sent_text)

    async def test_the_notice_handles_a_cancelled_outcome(self):
        from core import control_actions
        staff = types.SimpleNamespace(mention="<@99>")
        channel = types.SimpleNamespace(
            send=AsyncMock(),
            guild=types.SimpleNamespace(get_member=lambda uid: staff),
        )
        action = {"actor_id": 99, "payload": {"outcome": "cancelled"}}
        await control_actions.execute_redeem_fulfilled_notice(channel, action)
        self.assertIn("<@99>", channel.send.await_args.args[0])

    async def test_the_notice_falls_back_when_the_staff_member_is_uncached(self):
        from core import control_actions
        channel = types.SimpleNamespace(
            send=AsyncMock(),
            guild=types.SimpleNamespace(get_member=lambda uid: None),
        )
        action = {"actor_id": 99,
                  "payload": {"outcome": "fulfilled", "discord_item_id": "1"}}
        error = await control_actions.execute_redeem_fulfilled_notice(channel, action)
        self.assertIsNone(error)
        channel.send.assert_awaited_once()

    async def test_entitlement_revocation_reuses_revoke_entitlement(self):
        from core import control_actions
        guild = types.SimpleNamespace(id=GUILD)
        bot = types.SimpleNamespace(get_guild=lambda gid: guild)
        action = {
            "guild_id": GUILD,
            "payload": {"entitlement_id": 5, "user_id": MEMBER,
                        "entitlement_key": "emoji", "discord_item_id": "777"},
        }
        with patch("cogs.gacha.revoke_entitlement",
                   AsyncMock(return_value=True)) as mocked:
            error = await control_actions.execute_entitlement_revocation(bot, action)
        self.assertIsNone(error)
        mocked.assert_awaited_once()
        passed_guild, passed_entitlement = mocked.await_args.args
        self.assertIs(guild, passed_guild)
        self.assertEqual("emoji", passed_entitlement["entitlement_key"])
        self.assertEqual("777", passed_entitlement["discord_item_id"])

    async def test_entitlement_revocation_reports_a_discord_failure(self):
        from core import control_actions
        guild = types.SimpleNamespace(id=GUILD)
        bot = types.SimpleNamespace(get_guild=lambda gid: guild)
        action = {"guild_id": GUILD, "payload": {
            "entitlement_id": 5, "user_id": MEMBER,
            "entitlement_key": "emoji", "discord_item_id": "777"}}
        with patch("cogs.gacha.revoke_entitlement", AsyncMock(return_value=False)):
            error = await control_actions.execute_entitlement_revocation(bot, action)
        self.assertEqual("discord_http_error", error)

    async def test_entitlement_revocation_is_a_no_op_if_the_guild_is_gone(self):
        from core import control_actions
        bot = types.SimpleNamespace(get_guild=lambda gid: None)
        action = {"guild_id": GUILD, "payload": {}}
        error = await control_actions.execute_entitlement_revocation(bot, action)
        self.assertIsNone(error)


class RedeemsPageActionsTests(unittest.TestCase):
    """The dashboard's cancel/revoke buttons, driven through a real browser
    environment -- a renderer is only exercised by a row, so the fixture
    carries one open request and one active entitlement rather than empty
    lists, the same trap `boot_dashboard.js`'s own fixtures were rewritten to
    avoid."""

    def test_the_buttons_send_the_right_delete_requests(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        if not (Path(ROOT) / "node_modules" / "jsdom").is_dir():
            self.skipTest("jsdom is not installed; run `npm install`")
        script = Path(ROOT) / "tests" / "js" / "redeem_actions.js"
        result = subprocess.run([node, str(script), ROOT],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, f"{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
