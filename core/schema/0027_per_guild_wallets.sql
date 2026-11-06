-- Schema 27 (3.0.0): one wallet per guild.
--
-- `users` was keyed by the member alone, so one balance, XP total, level,
-- cooldown set and Everydle streak served every guild the bot is in: a member
-- of two servers earned twice into one pot, and could farm in one and spend in
-- the other. From here every `users` row belongs to one guild, keyed
-- (guild_id, user_id); a new guild starts empty (todo.md section 3, decided
-- 2026-08-27). The casino's rolling earnings window moves with it, because its
-- limit is already each guild's own setting (decided 2026-11-06).
--
-- **This migration says otherwise to "rewrite no row"**: it writes the new
-- column on every existing row. Nothing else changes: no row is added,
-- removed or merged, and every balance stays where it was, which the tests and
-- the rehearsal prove by count, by sum and per member.
--
-- Provenance, refusing rather than guessing. Every existing row was earned in
-- the one guild this installation served, resolved once, in order:
--   1. `POTATOBOT_LEGACY_GUILD_ID`, which `initialize_database` passes in as
--      the transaction-local setting `potatobot.legacy_guild_id`;
--   2. the single active row in `guilds`;
--   3. the single guild in the 2026-07 `scope_adoptions` marker.
-- If none answers, the whole start fails and the schema stays at 26: there is
-- no installation-default wallet, so guild 0 would park every member's money
-- where no command can reach it.

ALTER TABLE users ADD COLUMN guild_id bigint;
ALTER TABLE casino_earnings_window ADD COLUMN guild_id bigint;

DO $$
DECLARE
    configured text := current_setting('potatobot.legacy_guild_id', true);
    legacy bigint;
    candidates bigint[];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users)
            AND NOT EXISTS (SELECT 1 FROM casino_earnings_window) THEN
        RETURN;
    END IF;
    IF configured ~ '^[0-9]{1,19}$' AND configured::numeric BETWEEN 1 AND 9223372036854775807 THEN
        legacy := configured::bigint;
    ELSE
        SELECT array_agg(guild_id) INTO candidates FROM guilds WHERE active = 1;
        IF cardinality(candidates) = 1 THEN
            legacy := candidates[1];
        ELSE
            SELECT array_agg(DISTINCT guild_id) INTO candidates
                FROM scope_adoptions WHERE adoption_key LIKE 'legacy-users-v1:%';
            IF cardinality(candidates) = 1 THEN
                legacy := candidates[1];
            END IF;
        END IF;
    END IF;
    IF legacy IS NULL THEN
        RAISE EXCEPTION 'schema 27 cannot tell which guild the existing wallets '
            'belong to (no single active guild, no adoption marker); set '
            'POTATOBOT_LEGACY_GUILD_ID to that guild''s id and start again';
    END IF;
    UPDATE users SET guild_id = legacy;
    UPDATE casino_earnings_window SET guild_id = legacy;
END $$;

ALTER TABLE users ALTER COLUMN guild_id SET NOT NULL;
ALTER TABLE users DROP CONSTRAINT users_pkey;
ALTER TABLE users ADD CONSTRAINT users_pkey PRIMARY KEY (guild_id, user_id);

ALTER TABLE casino_earnings_window ALTER COLUMN guild_id SET NOT NULL;
ALTER TABLE casino_earnings_window DROP CONSTRAINT casino_earnings_window_pkey;
ALTER TABLE casino_earnings_window
    ADD CONSTRAINT casino_earnings_window_pkey PRIMARY KEY (guild_id, user_id);

-- The leaderboards rank one guild's wallets, so the guild leads each index.
DROP INDEX idx_users_balance;
DROP INDEX idx_users_streak;
DROP INDEX idx_users_xp;
CREATE INDEX idx_users_balance ON users(guild_id, balance DESC);
CREATE INDEX idx_users_streak ON users(guild_id, streak_count DESC) WHERE streak_count > 0;
CREATE INDEX idx_users_xp ON users(guild_id, xp DESC);
-- A member's rows across guilds: the first-guild check in `_ensure_user`,
-- export, erasure and retention all look a member up without a guild.
CREATE INDEX idx_users_user ON users(user_id);
