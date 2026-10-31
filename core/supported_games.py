"""The one ordered list of real-world games this installation has any
special awareness of.

`cogs/patchbot.py` owns the per-game patch-notes adapters; this module owns
only the key list, in `core/` because both `cogs/patchbot.py` (a bot-side
cog) and `dashboard_api.py` (which can run as a fully separate process in
the two-process split and must never import a cog) need to read it.
`tests/test_supported_games.py` pins this tuple against
`cogs.patchbot.SUPPORTED_GAMES` so the two cannot silently drift apart.

Display names live in `dashboard.game_names.<key>` in the locale catalogs,
not here: `/api/locale` serves only the top-level `dashboard` namespace to
the browser, so a name reachable by both the bot's Discord embeds and the
dashboard client has to live under that namespace, in one place, rather than
being duplicated per surface.
"""

SUPPORTED_GAME_KEYS = (
    "lol",
    "valorant",
    "minecraft",
    "phasmophobia",
    "dbd",
    "genshin",
    "cs2",
    "r6",
    "hsr",
    "wuwa",
    "zzz",
)


# Readers of the `games` setting (schema 26), here because both cogs need the
# same answer and neither may import the other. A stored row is already
# validated, but these still tolerate a malformed one: they run inside
# `/search` and the patch-notes loop, where a raise is a silent failure.

def lfg_role_for_channel(games, channel_id: int) -> int | None:
    """The role `/search` pings in this channel, or None if it is no game's."""
    for row in (games or {}).values():
        if (isinstance(row, dict) and row.get("lfg")
                and row.get("channel") == channel_id and row.get("role")):
            return row["role"]
    return None


def patch_target(games, game_key: str) -> tuple[int | None, int | None]:
    """`(channel, role)` for a supported game's patch notes.

    The channel is the row's own patch channel, else the game channel — the
    patch channel exists to override, so an unset one follows the game.
    """
    row = (games or {}).get(game_key)
    if not isinstance(row, dict):
        return None, None
    return row.get("patch_channel") or row.get("channel"), row.get("role")
