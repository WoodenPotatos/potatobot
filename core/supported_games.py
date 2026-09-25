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
