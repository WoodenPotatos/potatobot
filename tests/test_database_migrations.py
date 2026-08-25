import glob
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

import database


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_clean_database_reaches_current_schema(self):
        database.initialize_database()

        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            user_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(users)")
            }
            shop_count = conn.execute("SELECT COUNT(*) FROM shop_prices").fetchone()[0]
            tenant_columns = {
                table: {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for table in ("tickets", "warnings", "rented_items")
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            pity_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(gacha_pity)")
            }
            pull_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(gacha_pulls)")
            }
            voucher_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(reward_vouchers)")
            }

        self.assertEqual(version, database.LATEST_SCHEMA_VERSION)
        self.assertTrue(
            {
                "users", "tickets", "rewards", "shop_prices", "guilds",
                "realms", "feature_flags", "guild_data_scopes",
                "user_sharing_preferences", "activity_events", "scoped_accounts",
                "casino_wagers", "reward_claims",
                "shop_item_definitions", "user_inventory", "gacha_banners",
                "gacha_pity", "gacha_pulls", "reward_vouchers",
                "timed_entitlements", "fulfillment_requests",
                "dashboard_documents", "control_actions",
            } <= tables
        )
        self.assertTrue(set(database.USER_COLUMNS) <= user_columns)
        self.assertEqual(shop_count, len(database.SHOP_DEFAULTS))
        self.assertTrue(all("guild_id" in columns for columns in tenant_columns.values()))
        self.assertIn("pulls_toward_four_star", pity_columns)
        self.assertIn("four_star_guarantee", pull_columns)
        self.assertIn("source_type", voucher_columns)
        self.assertTrue(
            {
                "idx_users_xp", "idx_users_balance", "idx_users_streak",
                "idx_casino_wagers_pending", "idx_one_support_ticket_per_member",
            } <= indexes
        )
        self.assertEqual(glob.glob(f"{database.DB_PATH}.backup-*"), [])

    def test_legacy_database_is_backed_up_and_upgraded_in_place(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute(
                "CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER)"
            )
            conn.execute("INSERT INTO users (user_id, balance) VALUES (42, 9876)")
            conn.commit()

        database.initialize_database()
        database.initialize_database()

        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            row = conn.execute(
                "SELECT balance, xp, last_dbdle_killer FROM users WHERE user_id = 42"
            ).fetchone()
            ticket_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
            ).fetchone()

        backups = glob.glob(f"{database.DB_PATH}.backup-v0-*")
        self.assertEqual(version, database.LATEST_SCHEMA_VERSION)
        self.assertEqual(row, (9876, 0, None))
        self.assertIsNotNone(ticket_table)
        self.assertEqual(len(backups), 1)

        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(
                backup.execute("SELECT balance FROM users WHERE user_id = 42").fetchone()[0],
                9876,
            )

    def test_version_one_database_is_preserved_when_scoped_schema_is_added(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute(
                "CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER)"
            )
            conn.execute("INSERT INTO users (user_id, balance) VALUES (7, 4321)")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()

        database.initialize_database()

        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(
                conn.execute("SELECT balance FROM users WHERE user_id = 7").fetchone()[0],
                4321,
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'feature_flags'"
                ).fetchone()
            )
        self.assertEqual(len(glob.glob(f"{database.DB_PATH}.backup-v1-*")), 1)

    def test_newer_database_version_is_rejected(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute(f"PRAGMA user_version = {database.LATEST_SCHEMA_VERSION + 1}")
            conn.commit()

        with self.assertRaises(database.DatabaseOperationError):
            database.initialize_database()

    def test_percentage_vaults_migrate_to_fixed_reserves_once(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute(
                "CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER, "
                "vault_protection REAL)"
            )
            conn.executemany(
                "INSERT INTO users VALUES (?, 1000, ?)",
                [(1, 0.0), (2, 0.25), (3, 0.5), (4, 0.75)],
            )
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        database.initialize_database()
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT user_id, protected_reserve FROM users ORDER BY user_id"
            ).fetchall()
        self.assertEqual(rows, [(1, 0), (2, 25000), (3, 100000), (4, 500000)])

    def test_schema_five_gacha_counters_upgrade_without_losing_pity(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER)")
            conn.execute(
                "CREATE TABLE gacha_pity (guild_id INTEGER, user_id INTEGER, "
                "banner_key TEXT, pulls_since_five_star INTEGER, updated_at TEXT, "
                "PRIMARY KEY (guild_id, user_id, banner_key))"
            )
            conn.execute(
                "CREATE TABLE gacha_pulls (pull_id INTEGER PRIMARY KEY, guild_id INTEGER, "
                "user_id INTEGER, banner_key TEXT, banner_revision INTEGER, rarity INTEGER, "
                "reward_key TEXT, reward_json TEXT, pity_before INTEGER, soft_pity INTEGER, "
                "hard_pity INTEGER, created_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE gacha_banners (guild_id INTEGER, banner_key TEXT, "
                "enabled INTEGER, config_json TEXT, revision INTEGER, updated_by INTEGER, "
                "updated_at TEXT, PRIMARY KEY (guild_id, banner_key))"
            )
            conn.execute(
                "CREATE TABLE reward_vouchers (voucher_id TEXT PRIMARY KEY, "
                "guild_id INTEGER, user_id INTEGER, reward_key TEXT, duration_days INTEGER, "
                "status TEXT DEFAULT 'available', acquired_at TEXT, redeemed_at TEXT, "
                "fulfilled_at TEXT, expires_at TEXT, discord_item_id TEXT)"
            )
            conn.execute(
                "INSERT INTO reward_vouchers "
                "(voucher_id, guild_id, user_id, reward_key, duration_days, acquired_at) "
                "VALUES ('legacy', 10, 20, 'emoji_30d', 30, 'now')"
            )
            old_config = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
            old_config.pop("four_star_guarantee_interval")
            conn.execute(
                "INSERT INTO gacha_banners VALUES (10, 'standard', 1, ?, 3, NULL, 'now')",
                (json.dumps(old_config),),
            )
            conn.execute(
                "INSERT INTO gacha_pity VALUES (10, 20, 'standard', 73, 'now')"
            )
            conn.execute("PRAGMA user_version = 5")
            conn.commit()
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT pulls_since_five_star, pulls_toward_four_star "
                "FROM gacha_pity WHERE guild_id = 10 AND user_id = 20"
            ).fetchone()
            banner_config = json.loads(conn.execute(
                "SELECT config_json FROM gacha_banners WHERE guild_id = 10"
            ).fetchone()[0])
            voucher = conn.execute(
                "SELECT reward_key, source_type FROM reward_vouchers "
                "WHERE voucher_id = 'legacy'"
            ).fetchone()
        self.assertEqual(row, (73, 0))
        self.assertEqual(banner_config["four_star_guarantee_interval"], 10)
        self.assertEqual(voucher, ("emoji_30d", "gacha"))

    def test_schema_six_upgrades_without_touching_rows(self):
        """Every row survives the upgrade; only shape changes."""
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO tickets (channel_id, opener_id, created_at, guild_id) "
                "VALUES (1, 2, 'now', 10)"
            )
            conn.execute("INSERT INTO users (user_id, balance) VALUES (7, 1234)")
            conn.execute(
                "INSERT INTO voice_settings (user_id, channel_name) VALUES (7, 'room')"
            )
            # Pretend this database predates schema 7.
            conn.execute("PRAGMA user_version = 6")
            conn.commit()

        database.initialize_database()

        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            ticket = conn.execute(
                "SELECT opener_id, guild_id, claimer_id FROM tickets WHERE channel_id = 1"
            ).fetchone()
            balance = conn.execute(
                "SELECT balance FROM users WHERE user_id = 7"
            ).fetchone()[0]
            voice = conn.execute(
                "SELECT channel_name, guild_id FROM voice_settings WHERE user_id = 7"
            ).fetchone()
            action_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(control_actions)")
            }
            scoped = {
                table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for table in ("voice_settings", "voice_permissions", "active_channels")
            }

        self.assertEqual(version, database.LATEST_SCHEMA_VERSION)
        self.assertEqual(ticket, (2, 10, None))
        self.assertEqual(balance, 1234)
        # Schema 8 gives voice preferences a non-null provenance; 0 is the
        # installation default, which is what a pre-schema-8 row becomes.
        self.assertEqual(voice, ("room", 0))
        self.assertIn("lease_expires_at", action_columns)
        for table, columns in scoped.items():
            with self.subTest(table=table):
                self.assertIn("guild_id", columns)

    def _build_schema_seven_shapes(self, conn):
        """Recreate the pre-schema-8 table shapes: no guild in any primary key."""
        for table in ("shop_prices", "rewards", "server_config",
                      "voice_settings", "voice_permissions", "active_channels"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript("""
            CREATE TABLE shop_prices (item_id TEXT PRIMARY KEY, price INTEGER);
            CREATE TABLE rewards (
                activity_id TEXT PRIMARY KEY, coin_reward INTEGER, xp_reward INTEGER
            );
            CREATE TABLE server_config (
                config_key TEXT PRIMARY KEY, config_value TEXT
            );
            CREATE TABLE voice_settings (
                user_id INTEGER PRIMARY KEY, channel_name TEXT,
                user_limit INTEGER DEFAULT 0, locked INTEGER DEFAULT 0,
                bitrate INTEGER DEFAULT 64000, guild_id INTEGER
            );
            CREATE TABLE voice_permissions (
                owner_id INTEGER, target_id INTEGER, is_allowed INTEGER,
                guild_id INTEGER, PRIMARY KEY (owner_id, target_id)
            );
            CREATE TABLE active_channels (
                channel_id INTEGER PRIMARY KEY, owner_id INTEGER, guild_id INTEGER
            );
        """)

    def test_schema_eight_scopes_legacy_tables_without_losing_rows(self):
        """The rebuild must preserve every row, at the installation default."""
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self._build_schema_seven_shapes(conn)
            conn.execute("INSERT INTO shop_prices VALUES ('premium', 111)")
            conn.execute("INSERT INTO shop_prices VALUES ('lockpick', 222)")
            conn.execute("INSERT INTO rewards VALUES ('daily_normal', 333, 44)")
            conn.execute("INSERT INTO server_config VALUES ('log_channel', '999')")
            conn.execute(
                "INSERT INTO voice_settings "
                "(user_id, channel_name, user_limit, locked, bitrate, guild_id) "
                "VALUES (7, 'room', 5, 1, 96000, NULL)"
            )
            conn.execute("INSERT INTO voice_permissions VALUES (7, 8, 1, NULL)")
            conn.execute("INSERT INTO active_channels VALUES (500, 7, NULL)")
            conn.execute("PRAGMA user_version = 7")
            conn.commit()

        database.initialize_database()

        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            prices = dict(conn.execute(
                "SELECT item_id, price FROM shop_prices WHERE guild_id = 0"
            ).fetchall())
            reward = conn.execute(
                "SELECT coin_reward, xp_reward FROM rewards "
                "WHERE guild_id = 0 AND activity_id = 'daily_normal'"
            ).fetchone()
            server_row = conn.execute(
                "SELECT guild_id, config_value FROM server_config "
                "WHERE config_key = 'log_channel'"
            ).fetchone()
            voice = conn.execute(
                "SELECT channel_name, user_limit, locked, bitrate, guild_id "
                "FROM voice_settings WHERE user_id = 7"
            ).fetchone()
            permission = conn.execute(
                "SELECT is_allowed, guild_id FROM voice_permissions "
                "WHERE owner_id = 7 AND target_id = 8"
            ).fetchone()
            channel = conn.execute(
                "SELECT owner_id, guild_id FROM active_channels WHERE channel_id = 500"
            ).fetchone()
            keys = {
                table: tuple(
                    row[1] for row in sorted(
                        (r for r in conn.execute(f"PRAGMA table_info({table})") if r[5]),
                        key=lambda r: r[5],
                    )
                )
                for table in ("shop_prices", "rewards", "server_config",
                              "voice_settings", "voice_permissions")
            }

        self.assertEqual(version, database.LATEST_SCHEMA_VERSION)
        # Edited prices survive; the seeding must not overwrite them back to default.
        self.assertEqual(prices["premium"], 111)
        self.assertEqual(prices["lockpick"], 222)
        self.assertEqual(reward, (333, 44))
        self.assertEqual(server_row, (0, "999"))
        self.assertEqual(voice, ("room", 5, 1, 96000, 0))
        self.assertEqual(permission, (1, 0))
        self.assertEqual(channel, (7, 0))
        self.assertEqual(keys["shop_prices"], ("guild_id", "item_id"))
        self.assertEqual(keys["rewards"], ("guild_id", "activity_id"))
        self.assertEqual(keys["server_config"], ("guild_id", "config_key"))
        self.assertEqual(keys["voice_settings"], ("guild_id", "user_id"))
        self.assertEqual(
            keys["voice_permissions"], ("guild_id", "owner_id", "target_id")
        )

    def test_the_rebuild_preserves_a_column_this_version_never_declared(self):
        """A long-lived database carries columns from removed features — the dev
        copy still has active_channels.faction_group. A rebuild that only copied
        the declared columns would be a silent data loss."""
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute("DROP TABLE active_channels")
            conn.execute(
                "CREATE TABLE active_channels (channel_id INTEGER PRIMARY KEY, "
                "owner_id INTEGER, faction_group TEXT)"
            )
            conn.execute("INSERT INTO active_channels VALUES (500, 7, 'reds')")
            conn.execute("PRAGMA user_version = 7")
            conn.commit()

        database.initialize_database()

        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT owner_id, guild_id, faction_group FROM active_channels "
                "WHERE channel_id = 500"
            ).fetchone()
        self.assertEqual(row, (7, 0, "reds"))

    def test_schema_eight_rebuild_is_idempotent(self):
        """Repeated startups must neither re-run the rebuild nor duplicate rows."""
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self._build_schema_seven_shapes(conn)
            conn.execute("INSERT INTO shop_prices VALUES ('premium', 111)")
            conn.execute(
                "INSERT INTO voice_settings "
                "(user_id, channel_name, guild_id) VALUES (7, 'room', NULL)"
            )
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        for _ in range(4):
            database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             database.LATEST_SCHEMA_VERSION)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM shop_prices WHERE item_id = 'premium'"
                ).fetchone()[0], 1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT price FROM shop_prices "
                    "WHERE item_id = 'premium' AND guild_id = 0"
                ).fetchone()[0], 111,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM voice_settings").fetchone()[0], 1,
            )
            # A rebuild that ran twice would leave the scratch table behind.
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%__scoped'"
                ).fetchone()[0], 0,
            )

    def test_migration_is_idempotent(self):
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (9, 500)")
            conn.commit()
        for _ in range(3):
            database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                database.LATEST_SCHEMA_VERSION,
            )
            self.assertEqual(
                conn.execute("SELECT balance FROM users WHERE user_id = 9").fetchone()[0],
                500,
            )
            duplicated = conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('tickets') WHERE name = 'claimer_id'"
            ).fetchone()[0]
        self.assertEqual(duplicated, 1)


if __name__ == "__main__":
    unittest.main()


class Schema10WarningTagTests(unittest.TestCase):
    """Schema 10 adds `warnings.tag` and rewrites no row.

    The failure worth guarding is silent: if the default tag did not absorb a
    NULL, an upgrade would report every existing member's history as zero for
    the tag their warnings had always effectively been under, and every
    threshold would reset with it.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _downgrade_to_9(self):
        """Reshape a current database into the schema-9 warnings table."""
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute("DROP INDEX IF EXISTS idx_warnings_guild_user_tag")
            conn.execute("ALTER TABLE warnings DROP COLUMN tag")
            conn.execute("PRAGMA user_version = 9")
            conn.commit()

    def test_clean_database_has_the_column_and_its_index(self):
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(database.LATEST_SCHEMA_VERSION,
                             conn.execute("PRAGMA user_version").fetchone()[0])
            columns = {row[1] for row in conn.execute("PRAGMA table_info(warnings)")}
            self.assertIn("tag", columns)
            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
            self.assertIn("idx_warnings_guild_user_tag", indexes)

    def test_upgrade_preserves_rows_and_counts_them_under_the_default_tag(self):
        database.initialize_database()
        self._downgrade_to_9()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.executemany(
                "INSERT INTO warnings (user_id, mod_id, reason, date, guild_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [(7, 1, "one", "2026-01-01T00:00:00", 55),
                 (7, 1, "two", "2026-01-02T00:00:00", 55)],
            )
            conn.commit()

        database.initialize_database()

        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(database.LATEST_SCHEMA_VERSION,
                             conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(2, database.get_warning_count(7, 55))
        # An untagged row is a general warning, which is what it always was.
        self.assertEqual(2, database.get_warning_count(7, 55, "general"))
        self.assertEqual(0, database.get_warning_count(7, 55, "spam"))
        self.assertEqual(
            [None, None], [row[4] for row in database.get_warnings(7, 55)])

    def test_re_running_the_migration_changes_nothing(self):
        database.initialize_database()
        self._downgrade_to_9()
        database.initialize_database()
        database.record_warning(7, 1, "one", "2026-01-01T00:00:00", 55, "spam")
        before = database.get_warnings(7, 55)
        database.initialize_database()
        self.assertEqual(before, database.get_warnings(7, 55))

    def test_a_pre_migration_backup_is_written(self):
        database.initialize_database()
        self._downgrade_to_9()
        database.initialize_database()
        self.assertTrue(
            glob.glob(os.path.join(self.temp_dir.name, "economy.db.backup-v9-*")),
            "an upgrade must leave the rollback artefact behind",
        )

    def test_record_warning_counts_include_the_row_it_just_wrote(self):
        """The threshold can ban, so the count must include this warning."""
        database.initialize_database()
        first = database.record_warning(7, 1, "a", "2026-01-01T00:00:00", 55, "spam")
        second = database.record_warning(7, 1, "b", "2026-01-02T00:00:00", 55, "spam")
        other = database.record_warning(7, 1, "c", "2026-01-03T00:00:00", 55, "nsfw")
        self.assertEqual((1, 1), (first["total"], first["tag_count"]))
        self.assertEqual((2, 2), (second["total"], second["tag_count"]))
        # A different tag counts separately but adds to the total.
        self.assertEqual((3, 1), (other["total"], other["tag_count"]))
        # And a guild sees only its own.
        self.assertEqual(0, database.get_warning_count(7, 999, "spam"))


class Schema13EmbedKindTests(unittest.TestCase):
    """Schema 13 widens a CHECK, which SQLite cannot alter.

    So `managed_messages` is rebuilt — create, copy, drop, rename — the same
    shape schema 8 used. The risk of a rebuild is losing rows, so that is what
    these check, against a table populated the way a real one is.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(42, "Test Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def table_sql(self, conn):
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'managed_messages'").fetchone()[0]

    def make_schema_12_table(self, conn):
        """The table as schema 12 created it, populated the way a guild's is."""
        conn.execute("DROP TABLE managed_messages")
        conn.execute("""
            CREATE TABLE managed_messages (
                guild_id INTEGER NOT NULL,
                kind TEXT NOT NULL
                    CHECK (kind IN ('role_menu', 'rules', 'ticket', 'airlock')),
                menu_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                title TEXT,
                body TEXT,
                colour INTEGER,
                options_json TEXT NOT NULL DEFAULT '{}',
                revision INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, kind, menu_key)
            )
        """)
        for kind, key in (("role_menu", "games"), ("rules", "rules"),
                          ("ticket", "ticket"), ("airlock", "airlock")):
            conn.execute(
                "INSERT INTO managed_messages (guild_id, kind, menu_key, "
                "display_name, channel_id, message_id, title, body, colour, "
                "options_json, revision, updated_by, updated_at) VALUES "
                "(42, ?, ?, ?, 1420070400000000001, 1420070400000000002, 'T', "
                "'B', 12345, '{\"a\": 1}', 7, 99, 'when')",
                (kind, key, key.title()))

    def test_a_clean_database_already_accepts_an_embed(self):
        with database.get_connection() as conn:
            self.assertIn("'embed'", self.table_sql(conn))

    def test_an_older_table_is_rebuilt_and_loses_no_row(self):
        with database.get_connection() as conn:
            self.make_schema_12_table(conn)
            before = conn.execute(
                "SELECT guild_id, kind, menu_key, display_name, channel_id, "
                "message_id, title, body, colour, options_json, revision, "
                "updated_by, updated_at FROM managed_messages ORDER BY kind"
            ).fetchall()
            self.assertTrue(database.widen_managed_message_kinds(conn))
            after = conn.execute(
                "SELECT guild_id, kind, menu_key, display_name, channel_id, "
                "message_id, title, body, colour, options_json, revision, "
                "updated_by, updated_at FROM managed_messages ORDER BY kind"
            ).fetchall()
        self.assertEqual(before, after, "every column of every row must survive")
        self.assertEqual(4, len(after))

    def test_a_snowflake_survives_the_copy_exactly(self):
        """A rebuild copies ids through SQLite, and an id is 64-bit."""
        with database.get_connection() as conn:
            self.make_schema_12_table(conn)
            database.widen_managed_message_kinds(conn)
            stored = conn.execute(
                "SELECT message_id FROM managed_messages WHERE kind = 'rules'"
            ).fetchone()[0]
        self.assertEqual(1420070400000000002, stored)
        self.assertGreater(stored, 2 ** 53)

    def test_running_it_again_is_a_no_op(self):
        with database.get_connection() as conn:
            self.make_schema_12_table(conn)
            self.assertTrue(database.widen_managed_message_kinds(conn))
            self.assertFalse(database.widen_managed_message_kinds(conn))
            self.assertFalse(database.widen_managed_message_kinds(conn))

    def test_the_rebuilt_table_takes_an_embed_and_still_refuses_nonsense(self):
        with database.get_connection() as conn:
            self.make_schema_12_table(conn)
            database.widen_managed_message_kinds(conn)
            conn.execute(
                "INSERT INTO managed_messages (guild_id, kind, menu_key, "
                "display_name, options_json, revision, updated_at) "
                "VALUES (42, 'embed', 'notice', 'Notice', '{}', 1, 'when')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO managed_messages (guild_id, kind, menu_key, "
                    "display_name, options_json, revision, updated_at) "
                    "VALUES (42, 'nonsense', 'x', 'X', '{}', 1, 'when')")

    def test_the_primary_key_still_holds(self):
        with database.get_connection() as conn:
            self.make_schema_12_table(conn)
            database.widen_managed_message_kinds(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO managed_messages (guild_id, kind, menu_key, "
                    "display_name, options_json, revision, updated_at) "
                    "VALUES (42, 'rules', 'rules', 'Dup', '{}', 1, 'when')")


class RenamedSettingTests(unittest.TestCase):
    """A setting that changed key has to take its stored row with it.

    Otherwise the guild silently reverts to the default on upgrade: the old key
    stops being read in the same change the new one starts being written, and
    nothing about that failure looks like an error.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        # `guild_settings.guild_id` is a foreign key, so the guild has to exist
        # before a row can name it.
        database.register_guild(42, "Test Guild")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def stored(self, guild_id, key):
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM guild_settings "
                "WHERE guild_id = ? AND setting_key = ?", (guild_id, key)).fetchone()
        return row[0] if row else None

    def write(self, guild_id, key, value):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO guild_settings (guild_id, setting_key, value_json, "
                "revision, updated_by, updated_at) VALUES (?, ?, ?, 1, 1, 't')",
                (guild_id, key, value))

    def test_the_stored_value_moves_to_the_new_key(self):
        self.write(42, "other_games_channel", "1420070400000000001")
        with database.get_connection() as conn:
            self.assertEqual(1, database.rename_setting_rows(conn))
        self.assertEqual("1420070400000000001",
                         self.stored(42, "lfg_default_channel"))
        self.assertIsNone(self.stored(42, "other_games_channel"))

    def test_running_it_again_changes_nothing(self):
        self.write(42, "other_games_channel", "1420070400000000001")
        with database.get_connection() as conn:
            database.rename_setting_rows(conn)
            self.assertEqual(0, database.rename_setting_rows(conn))
        self.assertEqual("1420070400000000001",
                         self.stored(42, "lfg_default_channel"))

    def test_a_value_already_saved_under_the_new_key_wins(self):
        """The old row is stale by definition — it stopped being written."""
        self.write(42, "other_games_channel", "1420070400000000001")
        self.write(42, "lfg_default_channel", "1420070400000000002")
        with database.get_connection() as conn:
            database.rename_setting_rows(conn)
        self.assertEqual("1420070400000000002",
                         self.stored(42, "lfg_default_channel"))
        self.assertIsNone(self.stored(42, "other_games_channel"))

    def test_every_rename_names_a_setting_that_exists(self):
        from settings_registry import SETTING_DEFINITIONS
        for old_key, new_key in database.RENAMED_SETTINGS:
            with self.subTest(new_key=new_key):
                self.assertIn(new_key, SETTING_DEFINITIONS)
                self.assertNotIn(old_key, SETTING_DEFINITIONS,
                                 "the old key must be gone, or both are live")


class Schema12ManagedMessageTests(unittest.TestCase):
    """Schema 12 gives a posted message an identity the dashboard can edit.

    `message_id` appeared nowhere in the schema before this, so every publish was
    fire-and-forget: a draft could be posted a second time but never updated, and
    the bot's own `/update_games` worked only because the operator typed the id.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")
        database.initialize_database()
        database.register_guild(42, "Guild")
        self.menu = {
            "League of Legends": {"id": 1420070400000000001,
                                  "emoji": "<:lol:1420070400000000002>"},
            "Valorant": {"id": 1420070400000000003, "emoji": ""},
        }

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def _downgrade_to_11(self, with_settings=True):
        """Reshape into schema 11: no managed tables, menus as typed settings."""
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute("DROP INDEX IF EXISTS idx_managed_entries_menu")
            conn.execute("DROP TABLE IF EXISTS managed_message_entries")
            conn.execute("DROP TABLE IF EXISTS managed_messages")
            conn.execute("DELETE FROM guild_settings WHERE setting_key LIKE '%_roles'")
            if with_settings:
                conn.execute(
                    "INSERT INTO guild_settings (guild_id, setting_key, "
                    "value_json, revision, updated_by, updated_at) "
                    "VALUES (42, 'game_roles', ?, 1, 1, 't')",
                    (json.dumps(self.menu),))
            conn.execute("PRAGMA user_version = 11")
            conn.commit()

    def test_clean_database_has_both_tables(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(database.LATEST_SCHEMA_VERSION,
                             conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertIn("managed_messages", tables)
            self.assertIn("managed_message_entries", tables)

    def test_the_upgrade_seeds_a_role_menu_from_its_setting(self):
        self._downgrade_to_11()
        database.initialize_database()

        seeded = database.get_managed_message(42, "role_menu", "games")
        self.assertIsNotNone(seeded)
        rebuilt = {entry["label"]: {"id": int(entry["role_id"]),
                                    "emoji": entry["emoji"]}
                   for entry in seeded["entries"]}
        self.assertEqual(self.menu, rebuilt, "every pair must survive intact")

    def test_a_seeded_menu_is_not_claimed_to_be_posted(self):
        """Guessing a message id is not available to us, and posting a second
        copy on upgrade would be worse than asking."""
        self._downgrade_to_11()
        database.initialize_database()
        self.assertFalse(
            database.get_managed_message(42, "role_menu", "games")["posted"])

    def test_a_guild_with_no_menu_setting_gets_no_empty_menu(self):
        self._downgrade_to_11(with_settings=False)
        database.initialize_database()
        self.assertIsNone(database.get_managed_message(42, "role_menu", "games"))

    def test_re_running_seeds_nothing_and_keeps_edits(self):
        self._downgrade_to_11()
        database.initialize_database()
        menu = database.get_managed_message(42, "role_menu", "games")
        database.save_managed_message(
            42, 7, "role_menu", "games", "Renamed", menu["revision"],
            entries=[{"label": "Only one", "role_id": "1420070400000000004"}])

        database.initialize_database()

        after = database.get_managed_message(42, "role_menu", "games")
        self.assertEqual("Renamed", after["display_name"])
        self.assertEqual(1, len(after["entries"]),
                         "seeding must not restore what an operator removed")

    def test_the_settings_rows_are_left_alone_by_the_seed(self):
        """They stop being read in the change that moves the readers over, so an
        upgrade interrupted between the two has lost nothing."""
        self._downgrade_to_11()
        database.initialize_database()
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM guild_settings "
                "WHERE setting_key = 'game_roles'").fetchone()[0])

    def test_a_pre_migration_backup_is_written(self):
        self._downgrade_to_11()
        database.initialize_database()
        self.assertTrue(
            glob.glob(os.path.join(self.temp_dir.name, "economy.db.backup-v11-*")))

    def test_content_edits_never_move_a_posted_message(self):
        """Where a message lives is a fact about Discord, recorded when a post
        succeeds — an edit to the text must not silently claim it moved."""
        database.save_managed_message(42, 7, "role_menu", "solo", "Solo", 0,
                                      entries=[{"label": "a", "role_id": "1"}])
        database.record_managed_post(42, "role_menu", "solo", 111, 222)
        menu = database.get_managed_message(42, "role_menu", "solo")
        database.save_managed_message(
            42, 7, "role_menu", "solo", "Solo", menu["revision"],
            entries=[{"label": "a", "role_id": "1"},
                     {"label": "b", "role_id": "2"}])
        after = database.get_managed_message(42, "role_menu", "solo")
        self.assertEqual("222", after["message_id"])
        self.assertEqual("111", after["channel_id"])
        self.assertEqual(2, len(after["entries"]))
