# Privacy and data lifecycle

PotatoBot stores Discord user and guild IDs, economy/profile/game state,
moderation warnings, ticket ownership, voice preferences, rentals, feature and
settings audit history, and limited activity timestamps. Ticket transcripts are
sent to the configured Discord log channel when staff closes a ticket; generation
is capped at 50,000 messages and five files below Discord's upload limit.

Self-hosters are the data controllers for their installations. They should grant
database and backup access only to operators, define retention periods appropriate
for their community, and disclose enabled logging/reward/moderation features to
members. PotatoBot does not exchange data between installations.

## Export

`/mydata` sends the member a JSON file in DMs containing every row this
installation holds about them, across every guild — the operator is a single data
controller and the economy tables have no guild dimension, so a guild-filtered
export would be a half-truth. Rows that are guild-keyed carry their `guild_id`, so
the member can see what belongs where.

The export deliberately includes rows where the member is not the owner: warnings
they issued as a moderator, tickets they claimed, and other members' voice block
lists naming them. It also includes rows the gameplay readers hide, such as
zero-quantity inventory. Each table is capped at 5,000 rows and any table that hit
the cap is named in `truncated_tables`. Delivery is DM-only: if DMs are closed the
command says so rather than posting the file in a channel.

## Deletion

`/deletemydata` erases the member after one explicit confirmation. Deletion is
**installation-wide**, because wallets are keyed by Discord user id alone and there
is no per-guild copy to erase.

Deletion is an *anonymisation*, not a wipe, and the distinction is deliberate:

- **Deleted outright** — profile and behavioural data, warnings, tickets the member
  opened, voice preferences and permissions, inventory, gacha history, vouchers,
  entitlements, fulfilment requests, sharing preferences and activity events.
- **Retained under a tombstone** — the economy row, its scoped copy, settled wagers
  and reward claims. The tombstone is a fresh negative integer, which cannot
  collide with a Discord snowflake and is unique per erasure. Behavioural columns
  on the retained row (cooldowns, streaks, activity timestamps) are blanked.
- **Dereferenced** — attribution on other members' rows. Nullable columns such as
  `warnings.mod_id` and `tickets.claimer_id` become NULL; columns that cannot be
  nulled, such as `settings_audit.actor_id`, point at the tombstone instead.

Retaining the economy row is what satisfies "never silently alter financial
totals": the installation's coin supply is provably unchanged, and the reward-claim
records that stop a returning member being paid twice survive without the personal
link. Any unsettled wager is refunded to the balance first, because its stake had
already left the balance and deleting the row would destroy the obligation.

Active premium roles and rented assets are withdrawn in Discord before the records
that would have expired them are removed. Audit payloads that named the member —
`remove_warning` embeds the subject id and the staff reason in
`settings_audit.old_value_json` — are rewritten in the same transaction. The
erasure's own audit row records the tombstone and per-table counts, never the
member, because the audit feed is readable by every guild administrator.

The member receives a receipt listing exactly what was deleted and what was kept.

## Retention

`data_retention_days` (Administration → Instance, 0 = retain indefinitely) erases
members who have been gone for longer than the window. A member is only erased when
**both** conditions hold: their recorded activity predates the window, **and** they
are absent from every guild the bot serves. Staleness alone would erase a lurker
who simply never triggered an activity write. Each daily pass erases at most 25
members. Where guilds disagree, the shortest configured window wins, since erasure
is installation-wide and no guild can consent on another's behalf.

## Operators

The host can act on a member's behalf from the dashboard's Privacy card
(Audit page): entering a user id queues the same erasure for the bot to execute.
This is host-only rather than guild-administrator, because erasure spans the whole
installation. Erasure and retention records appear in the same card, and in the
audit feed, so an operator can show what was retained.
