-- The SQLite schema at version 26, exactly as 2.20.12a's initialize_database()
-- created it (generated 2026-11-01 from a worktree of main). The copy to
-- PostgreSQL reads only this schema, so its tests build their source from it.
-- Never edit by hand: SQLite's schema is frozen at 26.

CREATE TABLE active_channels (channel_id INTEGER PRIMARY KEY, owner_id INTEGER, guild_id INTEGER NOT NULL DEFAULT 0);

CREATE TABLE activity_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            origin_guild_id INTEGER,
            category TEXT NOT NULL,
            event_type TEXT NOT NULL,
            amount INTEGER,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        );

CREATE TABLE casino_earnings_window (
            user_id INTEGER PRIMARY KEY,
            window_started_at TEXT NOT NULL,
            window_total INTEGER NOT NULL DEFAULT 0
        );

CREATE TABLE casino_wagers (
            wager_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            stake INTEGER NOT NULL CHECK (stake > 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'settled', 'refunded')),
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_json TEXT
        );

CREATE TABLE control_actions (
            action_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error_code TEXT
        , lease_expires_at TEXT);

CREATE TABLE dashboard_access_invalidations (
            invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER,
            created_at TEXT NOT NULL
        );

CREATE TABLE dashboard_documents (
            document_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            document_type TEXT NOT NULL CHECK (document_type IN ('embed', 'rules', 'panel')),
            name TEXT NOT NULL,
            content_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (guild_id, document_type, name)
        );

CREATE TABLE feature_flags (
            guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
            feature_key TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, feature_key)
        );

CREATE TABLE fulfillment_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id TEXT NOT NULL UNIQUE REFERENCES reward_vouchers(voucher_id),
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            asset_type TEXT NOT NULL CHECK (asset_type IN ('emoji', 'sticker', 'sound')),
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'fulfilled', 'cancelled')),
            created_at TEXT NOT NULL,
            completed_at TEXT,
            completed_by INTEGER
        , ticket_channel_id INTEGER);

CREATE TABLE gacha_banners (
            guild_id INTEGER NOT NULL,
            banner_key TEXT NOT NULL,
            -- Operator-facing label. NULL on a banner created before schema 9,
            -- which then renders as its key.
            display_name TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            config_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            -- Both NULL means "no schedule": pullability then depends only on
            -- `enabled`, exactly as before schema 21. A bound present narrows
            -- that further; `enabled` still applies on top as a manual override.
            starts_at TEXT,
            ends_at TEXT,
            PRIMARY KEY (guild_id, banner_key)
        );

CREATE TABLE gacha_pity (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            banner_key TEXT NOT NULL,
            pulls_since_five_star INTEGER NOT NULL DEFAULT 0,
            pulls_toward_four_star INTEGER NOT NULL DEFAULT 0,
            guaranteed_featured_five INTEGER NOT NULL DEFAULT 0,
            guaranteed_featured_four INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, banner_key)
        );

CREATE TABLE gacha_pulls (
            pull_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            banner_key TEXT NOT NULL,
            banner_revision INTEGER NOT NULL,
            rarity INTEGER NOT NULL CHECK (rarity IN (3, 4, 5)),
            reward_key TEXT NOT NULL,
            reward_json TEXT NOT NULL,
            pity_before INTEGER NOT NULL,
            soft_pity INTEGER NOT NULL CHECK (soft_pity IN (0, 1)),
            hard_pity INTEGER NOT NULL CHECK (hard_pity IN (0, 1)),
            four_star_guarantee INTEGER NOT NULL DEFAULT 0 CHECK (four_star_guarantee IN (0, 1)),
            featured INTEGER NOT NULL DEFAULT 0 CHECK (featured IN (0, 1)),
            featured_guaranteed INTEGER NOT NULL DEFAULT 0 CHECK (featured_guaranteed IN (0, 1)),
            created_at TEXT NOT NULL
        );

CREATE TABLE game_patch_state (
            game_key TEXT PRIMARY KEY,
            latest_version TEXT,
            latest_url TEXT,
            latest_title TEXT,
            checked_at TEXT
        );

CREATE TABLE guild_data_scopes (
            guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
            category TEXT NOT NULL
                CHECK (category IN ('economy', 'profile', 'game_stats', 'moderation')),
            scope_type TEXT NOT NULL DEFAULT 'guild'
                CHECK (scope_type IN ('guild', 'realm', 'instance')),
            realm_id INTEGER REFERENCES realms(realm_id),
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, category),
            CHECK (
                (scope_type = 'realm' AND realm_id IS NOT NULL) OR
                (scope_type != 'realm' AND realm_id IS NULL)
            )
        );

CREATE TABLE guild_patch_announcements (
            guild_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            last_announced_version TEXT,
            PRIMARY KEY (guild_id, game_key)
        );

CREATE TABLE guild_settings (
            guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
            setting_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, setting_key)
        );

CREATE TABLE guilds (
            guild_id INTEGER PRIMARY KEY,
            display_name TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

CREATE TABLE instance_settings (
            setting_key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER,
            updated_at TEXT NOT NULL
        );

CREATE TABLE lfg_posts (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            host_id INTEGER NOT NULL,
            -- Exactly one of these is set: a game is either one of the guild's
            -- game roles or a line of text the host typed.
            game_role_id INTEGER,
            game_text TEXT,
            needed INTEGER NOT NULL DEFAULT 0,
            -- The party, as an ordered JSON list. A second table would be more
            -- normalised and every read wants the whole list anyway, in order,
            -- which is what the embed prints.
            joined_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        );

CREATE TABLE managed_message_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            menu_key TEXT NOT NULL,
            position INTEGER NOT NULL,
            label TEXT NOT NULL,
            role_id INTEGER,
            emoji TEXT NOT NULL DEFAULT '',
            UNIQUE (guild_id, kind, menu_key, label)
        );

CREATE TABLE managed_messages (
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL
                CHECK (kind IN ('role_menu', 'rules', 'ticket', 'airlock',
                               'embed')),
            menu_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            -- NULL until posted, and NULL again once the message is gone.
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
        );

CREATE TABLE minigame_state (
            guild_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            last_user_id INTEGER,
            streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, game_key)
        );

CREATE TABLE minigame_used_words (
            guild_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            word TEXT NOT NULL,
            used_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, game_key, word)
        );

CREATE TABLE realm_guilds (
            realm_id INTEGER NOT NULL REFERENCES realms(realm_id),
            guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'left')),
            approved_by INTEGER,
            joined_at TEXT,
            PRIMARY KEY (realm_id, guild_id)
        );

CREATE TABLE realms (
            realm_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_by INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'archived')),
            created_at TEXT NOT NULL
        );

CREATE TABLE rented_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_type TEXT,
            discord_item_id TEXT, expires_at TEXT
        , guild_id INTEGER);

CREATE TABLE reward_claims (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reward_key TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, reward_key)
        );

CREATE TABLE reward_vouchers (
            voucher_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reward_key TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'gacha'
                CHECK (source_type IN ('gacha', 'shop')),
            duration_days INTEGER NOT NULL CHECK (duration_days > 0),
            -- What the voucher is *for*, written when it is granted. NULL means
            -- "derive it from `reward_key`", which is how every voucher granted
            -- before this column still redeems.
            subject TEXT,
            status TEXT NOT NULL DEFAULT 'available'
                CHECK (status IN ('available', 'pending', 'active', 'fulfilled', 'expired', 'cancelled')),
            acquired_at TEXT NOT NULL,
            redeemed_at TEXT,
            fulfilled_at TEXT,
            expires_at TEXT,
            discord_item_id TEXT
        );

CREATE TABLE rewards (guild_id INTEGER NOT NULL DEFAULT 0, activity_id TEXT NOT NULL, coin_reward INTEGER, xp_reward INTEGER, PRIMARY KEY (guild_id, activity_id));

CREATE TABLE scope_adoptions (
            adoption_key TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            user_count INTEGER NOT NULL,
            adopted_at TEXT NOT NULL
        );

CREATE TABLE scoped_accounts (
            scope_type TEXT NOT NULL
                CHECK (scope_type IN ('guild', 'realm', 'instance')),
            scope_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            balance INTEGER NOT NULL DEFAULT 100 CHECK (balance >= 0),
            xp INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
            level INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, bj_wins INTEGER DEFAULT 0, bj_losses INTEGER DEFAULT 0, last_daily TEXT, last_job TEXT, rob_bonus REAL DEFAULT 0.0, rob_defense REAL DEFAULT 1.0, vault_protection REAL DEFAULT 0.0, protected_reserve INTEGER NOT NULL DEFAULT 0, bodyguard_until TEXT, last_rob TEXT, last_loldle_easy TEXT, last_loldle_medium TEXT, last_loldle_hard TEXT, last_valdle TEXT, last_genshindle TEXT, last_dbdle_killer TEXT, last_dbdle_survivor TEXT, last_dbdle_perk TEXT, streak_count INTEGER DEFAULT 0, last_streak_update TEXT, last_active TEXT DEFAULT NULL, inactive_warned INTEGER DEFAULT 0, rules_read_time INTEGER DEFAULT NULL,
            PRIMARY KEY (scope_type, scope_id, user_id)
        );

CREATE TABLE server_config (guild_id INTEGER NOT NULL DEFAULT 0, config_key TEXT NOT NULL, config_value TEXT, PRIMARY KEY (guild_id, config_key));

CREATE TABLE settings_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_key TEXT NOT NULL,
            old_value_json TEXT,
            new_value_json TEXT,
            created_at TEXT NOT NULL
        );

CREATE TABLE shop_item_definitions (
            guild_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            template_type TEXT NOT NULL,
            category TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            price INTEGER NOT NULL CHECK (price >= 0),
            config_json TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, item_key)
        );

CREATE TABLE shop_item_localizations (
            guild_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            language TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            PRIMARY KEY (guild_id, item_key, language),
            FOREIGN KEY (guild_id, item_key)
                REFERENCES shop_item_definitions(guild_id, item_key) ON DELETE CASCADE
        );

CREATE TABLE shop_prices (guild_id INTEGER NOT NULL DEFAULT 0, item_id TEXT NOT NULL, price INTEGER, PRIMARY KEY (guild_id, item_id));

CREATE TABLE tickets (
            channel_id INTEGER PRIMARY KEY, opener_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        , guild_id INTEGER, ticket_type TEXT, claimer_id INTEGER);

CREATE TABLE timed_entitlements (
            entitlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            entitlement_key TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            source_voucher_id TEXT UNIQUE REFERENCES reward_vouchers(voucher_id),
            discord_item_id TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'expired', 'cancelled')),
            -- Schema 22. NULL means "derive it from the voucher join" -- see
            -- the ALTER TABLE in _create_control_plane_v5_schema for why
            -- these carry no CHECK.
            source_type TEXT,
            granted_by INTEGER
        , revoked_at TEXT, discord_missing_since TEXT);

CREATE TABLE user_identities (
            user_id INTEGER PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

CREATE TABLE user_inventory (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, item_key)
        );

CREATE TABLE user_sharing_preferences (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
            category TEXT NOT NULL
                CHECK (category IN ('economy', 'profile', 'game_stats')),
            opted_out INTEGER NOT NULL DEFAULT 0 CHECK (opted_out IN (0, 1)),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, guild_id, category)
        );

CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 100, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1, bj_wins INTEGER DEFAULT 0, bj_losses INTEGER DEFAULT 0, last_daily TEXT, last_job TEXT, rob_bonus REAL DEFAULT 0.0, rob_defense REAL DEFAULT 1.0, vault_protection REAL DEFAULT 0.0, protected_reserve INTEGER NOT NULL DEFAULT 0, bodyguard_until TEXT, last_rob TEXT, last_loldle_easy TEXT, last_loldle_medium TEXT, last_loldle_hard TEXT, last_valdle TEXT, last_genshindle TEXT, last_dbdle_killer TEXT, last_dbdle_survivor TEXT, last_dbdle_perk TEXT, streak_count INTEGER DEFAULT 0, last_streak_update TEXT, last_active TEXT DEFAULT NULL, inactive_warned INTEGER DEFAULT 0, rules_read_time INTEGER DEFAULT NULL);

CREATE TABLE voice_permissions (guild_id INTEGER NOT NULL DEFAULT 0, owner_id INTEGER NOT NULL, target_id INTEGER NOT NULL, is_allowed INTEGER, PRIMARY KEY (guild_id, owner_id, target_id));

CREATE TABLE voice_settings (guild_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER NOT NULL, channel_name TEXT, user_limit INTEGER DEFAULT 0, locked INTEGER DEFAULT 0, bitrate INTEGER DEFAULT 64000, PRIMARY KEY (guild_id, user_id));

CREATE TABLE warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            mod_id INTEGER, reason TEXT, date TEXT
        , guild_id INTEGER, tag TEXT);

CREATE TABLE work_responses (
            response_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            -- Which outcome of `/work` this line belongs to. The tiers are
            -- mechanics, not text, so they stay English identifiers.
            tier TEXT NOT NULL CHECK (tier IN ('normal', 'free', 'high')),
            weight INTEGER NOT NULL CHECK (weight > 0),
            message TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            revision INTEGER NOT NULL DEFAULT 1,
            updated_by INTEGER,
            updated_at TEXT NOT NULL
        );

CREATE INDEX idx_activity_user_guild
            ON activity_events(user_id, origin_guild_id, created_at);

CREATE INDEX idx_casino_wagers_pending
            ON casino_wagers(status, created_at);

CREATE INDEX idx_casino_wagers_user
            ON casino_wagers(guild_id, user_id, created_at);

CREATE INDEX idx_control_actions_pending
            ON control_actions(status, created_at);

CREATE INDEX idx_gacha_pulls_user
            ON gacha_pulls(guild_id, user_id, created_at);

CREATE INDEX idx_managed_entries_menu
            ON managed_message_entries(guild_id, kind, menu_key, position);

CREATE UNIQUE INDEX idx_one_support_ticket_per_member
            ON tickets(guild_id, opener_id)
            WHERE ticket_type = 'support';

CREATE INDEX idx_reward_vouchers_user
            ON reward_vouchers(guild_id, user_id, acquired_at DESC);

CREATE INDEX idx_users_balance ON users(balance DESC);

CREATE INDEX idx_users_streak ON users(streak_count DESC) WHERE streak_count > 0;

CREATE INDEX idx_users_xp ON users(xp DESC);

CREATE INDEX idx_warnings_guild_user_tag ON warnings(guild_id, user_id, tag);

CREATE INDEX idx_work_responses_guild
            ON work_responses(guild_id, tier);
